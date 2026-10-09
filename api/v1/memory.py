"""Memory routes: owner-scoped CRUD, search, quality review, export.

Module 4 adds inspectable memory quality: conflicts, duplicates, revisions,
corrections and user-approved merges (spec §16.4 / §16.11).
"""

from __future__ import annotations

from datetime import datetime
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
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    importance: int = Field(default=3, ge=1, le=5)
    source_ref: str = Field(default="", max_length=255)
    retention_policy: str = Field(default="KEEP", max_length=24)
    expires_at: datetime | None = None


class MemoryPatch(BaseModel):
    key: str | None = Field(default=None, max_length=128)
    content: str | None = Field(default=None, max_length=8000)
    category: str | None = Field(default=None, max_length=48)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    importance: int | None = Field(default=None, ge=1, le=5)
    retention_policy: str | None = Field(default=None, max_length=24)
    expires_at: datetime | None = None


class CorrectIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class RevertIn(BaseModel):
    revision_id: int


class ResolveConflictIn(BaseModel):
    keep: str = Field(default="current", pattern="^(current|previous)$")


class MergeIn(BaseModel):
    target_id: int
    source_ids: list[int] = Field(min_length=1)
    merged_content: str | None = Field(default=None, max_length=8000)


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
            db,
            user.id,
            body.key,
            body.content,
            category=body.category,
            confidence=body.confidence,
            importance=body.importance,
            source_ref=body.source_ref,
            retention_policy=body.retention_policy,
            expires_at=body.expires_at,
            actor=f"user:{user.username}",
        )
    except MemoryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = _serialize(row)
    return {
        "ok": True,
        "memory": payload,
        # Surface a contradiction instead of hiding it.
        "conflict": payload["verification_state"] == "CONFLICT",
    }


@router.get("/search")
def search_memory(user: CurrentUser, db: DbDep, q: str = Query(min_length=1, max_length=256)):
    rows = memory.search(db, user.id, q, touch=True)
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


@router.get("/conflicts")
def list_conflicts(user: CurrentUser, db: DbDep):
    """Memories whose value contradicted a prior value and await a decision."""
    return {"conflicts": [_serialize(r) for r in memory.conflicts(db, user.id)]}


@router.get("/duplicates")
def list_duplicates(
    user: CurrentUser,
    db: DbDep,
    threshold: float = Query(default=0.85, ge=0.5, le=1.0),
):
    """Near-duplicate groups (read-only) the user may merge."""
    return {"groups": memory.find_duplicates(db, user.id, threshold=threshold)}


@router.post("/merge")
def merge_memories(body: MergeIn, user: MemoryWriter, db: DbDep):
    try:
        row = memory.merge(
            db,
            user.id,
            body.target_id,
            body.source_ids,
            body.merged_content,
            actor=f"user:{user.username}",
        )
    except MemoryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Target memory not found.")
    return {"ok": True, "memory": _serialize(row)}


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
            confidence=body.confidence,
            importance=body.importance,
            retention_policy=body.retention_policy,
            expires_at=body.expires_at,
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


@router.get("/{memory_id}/revisions")
def memory_revisions(memory_id: int, user: CurrentUser, db: DbDep):
    rows = memory.revisions(db, user.id, memory_id)
    return {
        "memory_id": memory_id,
        "revisions": [
            {
                "id": r.id,
                "content": r.content,
                "confidence": r.confidence,
                "importance": r.importance,
                "verification_state": r.verification_state,
                "reason": r.reason,
                "created_at": r.created_at.isoformat() + "Z",
            }
            for r in rows
        ],
    }


@router.post("/{memory_id}/correct")
def correct_memory(memory_id: int, body: CorrectIn, user: MemoryWriter, db: DbDep):
    try:
        row = memory.correct(db, user.id, memory_id, body.content, actor=f"user:{user.username}")
    except MemoryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"ok": True, "memory": _serialize(row)}


@router.post("/{memory_id}/revert")
def revert_memory(memory_id: int, body: RevertIn, user: MemoryWriter, db: DbDep):
    row = memory.revert(db, user.id, memory_id, body.revision_id, actor=f"user:{user.username}")
    if row is None:
        raise HTTPException(status_code=404, detail="Memory or revision not found.")
    return {"ok": True, "memory": _serialize(row)}


@router.post("/{memory_id}/resolve-conflict")
def resolve_conflict(memory_id: int, body: ResolveConflictIn, user: MemoryWriter, db: DbDep):
    row = memory.resolve_conflict(
        db, user.id, memory_id, keep=body.keep, actor=f"user:{user.username}"
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"ok": True, "memory": _serialize(row)}


@router.post("/{memory_id}/access")
def record_access(memory_id: int, user: CurrentUser, db: DbDep):
    row = memory.record_access(db, user.id, memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"ok": True, "access_count": row.access_count}


def _serialize(row) -> dict:
    return {
        "id": row.id,
        "category": row.category,
        "key": row.key,
        "content": row.content,
        "source": row.source,
        "confidence": row.confidence,
        "importance": row.importance,
        "verification_state": row.verification_state,
        "source_ref": row.source_ref,
        "retention_policy": row.retention_policy,
        "expires_at": row.expires_at.isoformat() + "Z" if row.expires_at else None,
        "access_count": row.access_count,
        "last_accessed_at": row.last_accessed_at.isoformat() + "Z" if row.last_accessed_at else None,
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }
