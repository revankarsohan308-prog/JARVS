"""Memory routes: owner-scoped CRUD, search, export (spec §16.4 / §16.11)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbDep, require_permission
from core.memory import service as memory
from core.memory.service import MemoryRejected
from core.permissions.rbac import Permission
from database.models import User
from security.audit import audit

router = APIRouter(prefix="/memory", tags=["memory"])

MemoryWriter = Annotated[User, Depends(require_permission(Permission.MEMORY_WRITE))]


class MemoryIn(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=8000)
    category: str = Field(default="IMPORTANT_FACTS", max_length=48)


class MemoryPatch(BaseModel):
    key: str | None = Field(default=None, max_length=128)
    content: str | None = Field(default=None, max_length=8000)
    category: str | None = Field(default=None, max_length=48)


@router.get("")
def list_memories(
    user: CurrentUser,
    db: DbDep,
    category: str | None = Query(default=None, max_length=48),
):
    rows = memory.list_memories(db, user.id, category=category)
    return {"memories": [_serialize(r) for r in rows], "stats": memory.stats(db, user.id)}


@router.post("", status_code=201)
def store_memory(body: MemoryIn, user: MemoryWriter, db: DbDep):
    try:
        row = memory.store(
            db, user.id, body.key, body.content, category=body.category,
            actor=f"user:{user.username}",
        )
    except MemoryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "memory": _serialize(row)}


@router.get("/search")
def search_memory(user: CurrentUser, db: DbDep, q: str = Query(min_length=1, max_length=256)):
    rows = memory.search(db, user.id, q)
    return {"results": [_serialize(r) for r in rows]}


@router.get("/export")
def export_memory(user: CurrentUser, db: DbDep):
    """User data export. Passwords/tokens are never stored, so none can leak."""
    rows = memory.list_memories(db, user.id)
    payload = {
        "exported_for": user.username,
        "category": "long_term_memory",
        "count": len(rows),
        "memories": [_serialize(r) for r in rows],
    }
    audit(db, "memory_exported", f"count={len(rows)}", actor=f"user:{user.username}", component="memory")
    return payload


@router.patch("/{memory_id}")
def update_memory(memory_id: int, body: MemoryPatch, user: MemoryWriter, db: DbDep):
    try:
        row = memory.update(
            db,
            user.id,
            memory_id,
            content=body.content,
            key=body.key,
            category=body.category,
            actor=f"user:{user.username}",
        )
    except MemoryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"ok": True, "memory": _serialize(row)}


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, user: MemoryWriter, db: DbDep):
    removed = memory.delete(db, user.id, memory_id, actor=f"user:{user.username}")
    if not removed:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"ok": True, "deleted": memory_id, "detail": "Deletion completed."}


def _serialize(row) -> dict:
    return {
        "id": row.id,
        "category": row.category,
        "key": row.key,
        "content": row.content,
        "source": row.source,
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }
