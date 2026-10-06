"""Task engine: real CRUD with priority, due dates, recurrence and reminders.

All rows are owner-scoped. Reminder dueness is computed by the scheduler in
core/scheduler.py from the stored timestamps — nothing is faked.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Task
from security.audit import audit
from timeutil import parse_datetime, utcnow

PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("pending", "done", "cancelled")
RECURRENCE = ("", "daily", "weekly", "monthly")


class TaskError(Exception):
    """User-facing task failure."""


def create(
    db: Session,
    user_id: int,
    title: str,
    *,
    notes: str = "",
    priority: str = "normal",
    due_at: datetime | str | None = None,
    recurrence: str = "",
    reminder_at: datetime | str | None = None,
    actor: str = "system",
) -> Task:
    title = (title or "").strip()
    if not title:
        raise TaskError("A task needs a title.")
    if priority not in PRIORITIES:
        priority = "normal"
    if recurrence not in RECURRENCE:
        recurrence = ""
    if isinstance(due_at, str):
        due_at = parse_datetime(due_at)
        if due_at is None:
            raise TaskError("I couldn't read that due date. Use a format like 2026-10-14 or a full ISO timestamp.")
    if isinstance(reminder_at, str):
        reminder_at = parse_datetime(reminder_at)

    task = Task(
        user_id=user_id,
        title=title[:255],
        notes=notes[:4000],
        priority=priority,
        due_at=due_at,
        recurrence=recurrence,
        reminder_at=reminder_at,
    )
    db.add(task)
    db.commit()
    audit(db, "task_created", f"id={task.id} title={task.title!r} due={task.due_at}",
          actor=actor, component="tasks")
    return task


def list_tasks(
    db: Session,
    user_id: int,
    *,
    include_completed: bool = False,
    day: datetime | None = None,
    limit: int = 200,
) -> list[Task]:
    stmt = select(Task).where(Task.user_id == user_id)
    if not include_completed:
        stmt = stmt.where(Task.status == "pending")
    if day is not None:
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
        stmt = stmt.where(Task.due_at >= start, Task.due_at < end)
    return db.scalars(stmt.order_by(Task.due_at.is_(None), Task.due_at, Task.id).limit(limit)).all()


def get(db: Session, user_id: int, task_id: int) -> Task | None:
    return db.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))


def update(db: Session, user_id: int, task_id: int, actor: str = "system", **fields) -> Task | None:
    task = get(db, user_id, task_id)
    if task is None:
        return None
    if "title" in fields and fields["title"]:
        task.title = str(fields["title"]).strip()[:255]
    if "notes" in fields and fields["notes"] is not None:
        task.notes = str(fields["notes"])[:4000]
    if "priority" in fields:
        if fields["priority"] not in PRIORITIES:
            raise TaskError(f"Priority must be one of: {', '.join(PRIORITIES)}.")
        task.priority = fields["priority"]
    if "status" in fields:
        if fields["status"] not in STATUSES:
            raise TaskError(f"Status must be one of: {', '.join(STATUSES)}.")
        task.status = fields["status"]
        task.completed_at = utcnow() if fields["status"] == "done" else None
    if "due_at" in fields:
        due = fields["due_at"]
        if isinstance(due, str):
            due = parse_datetime(due)
            if due is None and fields["due_at"]:
                raise TaskError("I couldn't read that due date.")
        task.due_at = due
    if "recurrence" in fields:
        if fields["recurrence"] not in RECURRENCE:
            raise TaskError("Recurrence must be one of: daily, weekly, monthly (or empty).")
        task.recurrence = fields["recurrence"]
    db.commit()
    audit(db, "task_updated", f"id={task.id} fields={sorted(k for k in fields if fields[k] is not None)}",
          actor=actor, component="tasks")
    return task


def complete(db: Session, user_id: int, task_id: int, actor: str = "system") -> Task | None:
    task = get(db, user_id, task_id)
    if task is None:
        return None
    if task.status == "done":
        return task
    task.status = "done"
    task.completed_at = utcnow()
    spawned: Task | None = None
    if task.recurrence:
        delta = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1), "monthly": timedelta(days=30)}[
            task.recurrence
        ]
        base = task.due_at or task.completed_at
        spawned = Task(
            user_id=user_id,
            title=task.title,
            notes=task.notes,
            priority=task.priority,
            due_at=base + delta,
            recurrence=task.recurrence,
            reminder_at=(task.reminder_at + delta) if task.reminder_at else None,
        )
        db.add(spawned)
    db.commit()
    audit(db, "task_completed", f"id={task.id} recurrence={task.recurrence or 'none'}",
          actor=actor, component="tasks")
    if spawned:
        audit(db, "task_created", f"id={spawned.id} title={spawned.title!r} (recurring next occurrence)",
              actor=actor, component="tasks")
    return task


def delete(db: Session, user_id: int, task_id: int, actor: str = "system") -> bool:
    task = get(db, user_id, task_id)
    if task is None:
        return False
    title = task.title
    db.delete(task)
    db.commit()
    audit(db, "task_deleted", f"id={task_id} title={title!r}", actor=actor, component="tasks")
    return True


def due_reminders(db: Session, now: datetime | None = None) -> list[Task]:
    now = now or utcnow()
    return db.scalars(
        select(Task).where(Task.status == "pending", Task.reminder_at.isnot(None), Task.reminder_at <= now)
    ).all()


def clear_completed(db: Session, user_id: int, actor: str = "system") -> int:
    rows = db.scalars(
        select(Task).where(Task.user_id == user_id, Task.status == "done")
    ).all()
    count = len(rows)
    for row in rows:
        db.delete(row)
    db.commit()
    if count:
        audit(db, "task_history_cleared", f"count={count}", actor=actor, component="tasks")
    return count
