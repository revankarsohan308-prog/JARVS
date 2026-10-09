"""Cross-device sync routes (Reliability Addendum Module 5).

Owner-scoped: the change log and conflict queue are per authenticated user.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbDep
from core.sync import service as sync

router = APIRouter(prefix="/sync", tags=["sync"])


class ChangeIn(BaseModel):
    entity_type: str = Field(min_length=1, max_length=32)
    entity_key: str = Field(min_length=1, max_length=255)
    op: str = Field(default="UPSERT", max_length=16)
    base_version: int | None = Field(default=None, ge=0)
    payload: dict = Field(default_factory=dict)
    client_change_id: str | None = Field(default=None, max_length=64)


class PushIn(BaseModel):
    device_id: str = Field(default="", max_length=64)
    on_conflict: str = Field(default="reject", max_length=16)
    changes: list[ChangeIn] = Field(default_factory=list, max_length=500)


class ResolveIn(BaseModel):
    entity_type: str = Field(min_length=1, max_length=32)
    entity_key: str = Field(min_length=1, max_length=255)
    keep: str = Field(pattern="^(local|remote)$")


@router.get("/state")
def sync_state(user: CurrentUser, db: DbDep):
    return sync.state(db, user.id)


@router.get("/pull")
def pull(user: CurrentUser, db: DbDep, since: int = Query(default=0, ge=0),
         limit: int = Query(default=200, ge=1, le=500)):
    return sync.pull(db, user.id, since=since, limit=limit)


@router.post("/push")
def push(body: PushIn, user: CurrentUser, db: DbDep):
    results = sync.push(
        db,
        user.id,
        body.device_id,
        [change.model_dump() for change in body.changes],
        on_conflict=body.on_conflict,
        actor=f"user:{user.username}",
    )
    return {"results": results, "cursor": sync.current_cursor(db, user.id)}


@router.get("/conflicts")
def list_conflicts(user: CurrentUser, db: DbDep):
    return {"conflicts": [sync.serialize_record(r) for r in sync.conflicts(db, user.id)]}


@router.post("/conflicts/resolve")
def resolve_conflict(body: ResolveIn, user: CurrentUser, db: DbDep):
    try:
        record = sync.resolve(
            db,
            user.id,
            body.entity_type,
            body.entity_key,
            keep=body.keep,
            actor=f"user:{user.username}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="No such conflict.")
    return {"ok": True, "record": sync.serialize_record(record)}
