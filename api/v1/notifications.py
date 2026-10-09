"""Notification centre routes (Reliability Addendum Module 8).

Owner-scoped: a user only ever sees and mutates their own notifications.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbDep
from core.notifications import service as notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


class PreferencesIn(BaseModel):
    min_severity: str = Field(default="INFO", max_length=16)
    muted_categories: list[str] = Field(default_factory=list)


@router.get("")
def list_notifications(
    user: CurrentUser,
    db: DbDep,
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=100, ge=1, le=200),
):
    rows = notifications.list_for_user(db, user.id, status=status, limit=limit)
    return {
        "notifications": [notifications.serialize(r) for r in rows],
        "unread": notifications.unread_count(db, user.id),
    }


@router.get("/unread-count")
def unread_count(user: CurrentUser, db: DbDep):
    return {"unread": notifications.unread_count(db, user.id)}


@router.get("/channels")
def channels(user: CurrentUser):
    """Which channels can actually deliver. External ones are NOT_CONFIGURED."""
    return {"channels": notifications.channel_status()}


@router.get("/preferences")
def get_preferences(user: CurrentUser, db: DbDep):
    row = notifications.get_preferences(db, user.id)
    return {
        "min_severity": row.min_severity,
        "muted_categories": json.loads(row.muted_categories_json or "[]"),
    }


@router.put("/preferences")
def set_preferences(body: PreferencesIn, user: CurrentUser, db: DbDep):
    row = notifications.set_preferences(
        db, user.id, min_severity=body.min_severity, muted_categories=body.muted_categories
    )
    return {
        "ok": True,
        "min_severity": row.min_severity,
        "muted_categories": json.loads(row.muted_categories_json or "[]"),
    }


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, user: CurrentUser, db: DbDep):
    row = notifications.mark_read(db, user.id, notification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    return {"ok": True, "notification": notifications.serialize(row)}


@router.post("/{notification_id}/ack")
def acknowledge(notification_id: str, user: CurrentUser, db: DbDep):
    row = notifications.acknowledge(db, user.id, notification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    return {"ok": True, "notification": notifications.serialize(row)}


@router.post("/{notification_id}/escalate")
def escalate(notification_id: str, user: CurrentUser, db: DbDep):
    row = notifications.escalate(db, user.id, notification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    return {"ok": True, "notification": notifications.serialize(row)}
