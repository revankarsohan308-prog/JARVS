"""Task routes: owner-scoped CRUD, permission-gated writes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbDep, require_permission
from core.permissions.rbac import Permission
from core.tasks import service as tasks
from core.tasks.service import TaskError
from database.models import User
from timeutil import parse_datetime

router = APIRouter(prefix="/tasks", tags=["tasks"])

TaskWriter = Annotated[User, Depends(require_permission(Permission.TASK_WRITE))]
TaskDeleter = Annotated[User, Depends(require_permission(Permission.TASK_DELETE))]


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    notes: str = Field(default="", max_length=4000)
    priority: str = Field(default="normal", max_length=16)
    due_at: str | None = Field(default=None, max_length=40)
    recurrence: str = Field(default="", max_length=16)
    reminder_at: str | None = Field(default=None, max_length=40)


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=4000)
    priority: str | None = Field(default=None, max_length=16)
    status: str | None = Field(default=None, max_length=16)
    due_at: str | None = Field(default=None, max_length=40)
    recurrence: str | None = Field(default=None, max_length=16)


@router.get("")
def list_tasks(
    user: CurrentUser,
    db: DbDep,
    include_completed: bool = False,
    day: str | None = Query(default=None, max_length=10),
):
    day_dt = None
    if day:
        day_dt = parse_datetime(day)
        if day_dt is None:
            raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD.")
    rows = tasks.list_tasks(db, user.id, include_completed=include_completed, day=day_dt)
    return {"tasks": [_serialize(t) for t in rows]}


@router.post("", status_code=201)
def create_task(body: TaskIn, user: TaskWriter, db: DbDep):
    try:
        task = tasks.create(
            db,
            user.id,
            body.title,
            notes=body.notes,
            priority=body.priority,
            due_at=body.due_at,
            recurrence=body.recurrence,
            reminder_at=body.reminder_at,
            actor=f"user:{user.username}",
        )
    except TaskError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "task": _serialize(task)}


@router.get("/{task_id}")
def get_task(task_id: int, user: CurrentUser, db: DbDep):
    task = tasks.get(db, user.id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"task": _serialize(task)}


@router.patch("/{task_id}")
def update_task(task_id: int, body: TaskPatch, user: TaskWriter, db: DbDep):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        task = tasks.update(db, user.id, task_id, actor=f"user:{user.username}", **fields)
    except TaskError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"ok": True, "task": _serialize(task)}


@router.post("/{task_id}/complete")
def complete_task(task_id: int, user: TaskWriter, db: DbDep):
    task = tasks.complete(db, user.id, task_id, actor=f"user:{user.username}")
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"ok": True, "task": _serialize(task)}


@router.delete("/{task_id}")
def delete_task(task_id: int, user: TaskDeleter, db: DbDep):
    removed = tasks.delete(db, user.id, task_id, actor=f"user:{user.username}")
    if not removed:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"ok": True, "deleted": task_id, "detail": "Deletion completed."}


@router.post("/clear-completed")
def clear_completed(user: TaskDeleter, db: DbDep):
    count = tasks.clear_completed(db, user.id, actor=f"user:{user.username}")
    return {"ok": True, "deleted": count}


def _serialize(t) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "notes": t.notes,
        "priority": t.priority,
        "status": t.status,
        "due_at": t.due_at.isoformat() + "Z" if t.due_at else None,
        "recurrence": t.recurrence,
        "reminder_at": t.reminder_at.isoformat() + "Z" if t.reminder_at else None,
        "created_at": t.created_at.isoformat() + "Z",
        "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
    }
