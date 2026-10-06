"""Privacy & Data Control Center routes (spec §16.4 / §16.11 / §16.17).

Retention periods are read from the centralized DATA_RETENTION_POLICY in
config.settings — never hard-coded per route. Deletion endpoints return a
truthful status (§16.14): COMPLETED only after the rows are actually gone.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from api.deps import CurrentUser, DbDep, require_permission
from config.settings import settings
from core.permissions.rbac import Permission
from database.models import AuditLog, ConversationTurn, Memory, SecurityEvent, Task, User
from security.audit import audit

router = APIRouter(prefix="/privacy", tags=["privacy"])

AdminUser = Annotated[User, Depends(require_permission(Permission.USER_MANAGE))]


class RetentionPolicyIn(BaseModel):
    retention_conversations_days: int = Field(ge=0, le=3650)
    retention_security_events_days: int = Field(ge=0, le=3650)
    retention_critical_incident_days: int = Field(ge=0, le=3650)
    retention_audit_days: int = Field(ge=0, le=3650)
    retention_temp_days: int = Field(ge=0, le=365)
    retention_task_history_days: int = Field(ge=0, le=3650)


class HistoryToggle(BaseModel):
    enabled: bool


class MemoryToggle(BaseModel):
    enabled: bool


@router.get("/retention")
def retention_policy(user: CurrentUser, db: DbDep):
    """The effective retention table plus how much data is currently stored."""
    counts = {
        "conversation_turns": db.scalar(select(func.count(ConversationTurn.id))) or 0,
        "memories": db.scalar(select(func.count(Memory.id))) or 0,
        "tasks": db.scalar(select(func.count(Task.id))) or 0,
        "security_events": db.scalar(select(func.count(SecurityEvent.id))) or 0,
        "audit_logs": db.scalar(select(func.count(AuditLog.id))) or 0,
    }
    return {
        "policy": data_retention_policy(),
        "stored_records": counts,
        "configurable_by": (
            "Administrator only. Values are validated (0-3650 days) and every "
            "change is authenticated, authorized and audited."
        ),
    }


def data_retention_policy() -> dict[str, dict[str, object]]:
    """Central retention table (spec §16.19). Single source of truth."""
    return {
        "conversation_history": {
            "retention_days": settings.retention_conversations_days,
            "user_can_delete": True,
            "auto_delete": True,
            "note": "Clear from Privacy at any time.",
        },
        "long_term_memories": {
            "retention_days": None,
            "user_can_delete": True,
            "auto_delete": False,
            "note": "Kept until you delete it.",
        },
        "tasks_and_reminders": {
            "retention_days": settings.retention_task_history_days,
            "user_can_delete": True,
            "auto_delete": True,
            "note": "Counted after completion.",
        },
        "security_events": {
            "retention_days": settings.retention_security_events_days,
            "user_can_delete": False,
            "auto_delete": True,
            "note": "Administrator-controlled.",
        },
        "critical_security_incidents": {
            "retention_days": settings.retention_critical_incident_days,
            "user_can_delete": False,
            "auto_delete": True,
            "note": "Evidence preserved for the security period.",
        },
        "audit_logs": {
            "retention_days": settings.retention_audit_days,
            "user_can_delete": False,
            "auto_delete": True,
            "note": "Append-only; retained for accountability.",
        },
        "voice_recordings": {
            "retention_days": 0 if not settings.store_voice_recordings else 30,
            "user_can_delete": True,
            "auto_delete": True,
            "note": "Not stored by default (0 days).",
        },
        "temporary_files": {
            "retention_days": settings.retention_temp_days,
            "user_can_delete": False,
            "auto_delete": True,
            "note": "Cache cleanup.",
        },
    }


@router.put("/retention")
def update_retention(body: RetentionPolicyIn, user: AdminUser, db: DbDep):
    """Authenticated, authorized, audited retention change (§16.20)."""
    settings.retention_conversations_days = body.retention_conversations_days
    settings.retention_security_events_days = body.retention_security_events_days
    settings.retention_critical_incident_days = body.retention_critical_incident_days
    settings.retention_audit_days = body.retention_audit_days
    settings.retention_temp_days = body.retention_temp_days
    settings.retention_task_history_days = body.retention_task_history_days
    audit(
        db,
        "retention_policy_changed",
        (
            f"conversations={body.retention_conversations_days} "
            f"security_events={body.retention_security_events_days} "
            f"incidents={body.retention_critical_incident_days} "
            f"audit={body.retention_audit_days} temp={body.retention_temp_days} "
            f"tasks={body.retention_task_history_days}"
        ),
        actor=f"user:{user.username}",
        component="privacy",
    )
    return {"ok": True, "policy": data_retention_policy(), "status": "DELETION_POLICY_UPDATED"}


@router.get("/data")
def stored_data(user: CurrentUser, db: DbDep):
    """What ALYO stores for THIS user (§16.4 view)."""
    own_turns = (
        db.scalar(select(func.count(ConversationTurn.id)).where(ConversationTurn.user_id == user.id))
        or 0
    )
    own_memories = (
        db.scalar(select(func.count(Memory.id)).where(Memory.user_id == user.id)) or 0
    )
    own_tasks = db.scalar(select(func.count(Task.id)).where(Task.user_id == user.id)) or 0
    return {
        "user": user.username,
        "categories": [
            {"category": "Conversation history", "records": own_turns, "retention_days": settings.retention_conversations_days},
            {"category": "Long-term memories", "records": own_memories, "retention_days": None},
            {"category": "Tasks & reminders", "records": own_tasks, "retention_days": settings.retention_task_history_days},
            {"category": "Audit log entries mentioning you", "records": None, "retention_days": settings.retention_audit_days},
            {"category": "Voice recordings", "records": 0, "retention_days": 0, "note": "Voice capture is not enabled."},
        ],
        "never_stored": ["Passwords (only PBKDF2 hashes)", "API keys", "Payment card data"],
    }


@router.get("/export")
def export_data(user: CurrentUser, db: DbDep):
    """Full personal export in readable JSON (§16.11). No secrets included."""
    turns = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.user_id == user.id)
        .order_by(ConversationTurn.id)
    ).all()
    memories = db.scalars(select(Memory).where(Memory.user_id == user.id)).all()
    tasks = db.scalars(select(Task).where(Task.user_id == user.id)).all()
    audit(
        db, "data_exported",
        f"turns={len(turns)} memories={len(memories)} tasks={len(tasks)}",
        actor=f"user:{user.username}", component="privacy",
    )
    return {
        "exported_for": user.username,
        "exported_at": _now(),
        "excluded": ["password hashes", "session tokens", "api keys"],
        "profile": {
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role,
            "created_at": user.created_at.isoformat() + "Z",
        },
        "conversations": [
            {"role": t.role, "content": t.content, "created_at": t.created_at.isoformat() + "Z"}
            for t in turns
        ],
        "memories": [
            {"category": m.category, "key": m.key, "content": m.content, "updated_at": m.updated_at.isoformat() + "Z"}
            for m in memories
        ],
        "tasks": [
            {
                "title": t.title, "notes": t.notes, "priority": t.priority,
                "status": t.status,
                "due_at": t.due_at.isoformat() + "Z" if t.due_at else None,
                "created_at": t.created_at.isoformat() + "Z",
            }
            for t in tasks
        ],
    }


@router.delete("/conversations")
def delete_conversations(user: CurrentUser, db: DbDep):
    """Truthful deletion: reports COMPLETED only when rows are really gone."""
    ids = db.scalars(
        select(ConversationTurn.id).where(ConversationTurn.user_id == user.id)
    ).all()
    for row_id in ids:
        row = db.get(ConversationTurn, row_id)
        if row is not None:
            db.delete(row)
    db.commit()
    remaining = (
        db.scalar(select(func.count(ConversationTurn.id)).where(ConversationTurn.user_id == user.id))
        or 0
    )
    # Deletion log records that it happened — never the content itself.
    audit(
        db, "conversations_deleted",
        f"requested={len(ids)} remaining={remaining}",
        actor=f"user:{user.username}", component="privacy",
    )
    return {
        "status": "DELETION_COMPLETED" if remaining == 0 else "DELETION_PARTIALLY_COMPLETED",
        "deleted": len(ids) - remaining,
        "requested": len(ids),
    }


class AccountDeleteIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    confirm: bool


@router.post("/account-deletion")
def request_account_deletion(
    body: AccountDeleteIn, user: CurrentUser, db: DbDep
):
    """Authenticated, confirmed account deletion (§16.8).

    Sessions are revoked first, then owned data is removed. The audit trail
    keeps only the minimum (that deletion happened, for which user id).
    """
    from core.identity.service import verify_password

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required: set confirm=true after reviewing what will be deleted.",
        )
    if not verify_password(body.password, user.password_hash):
        audit(db, "account_deletion_rejected", "password mismatch",
              actor=f"user:{user.username}", component="privacy")
        raise HTTPException(status_code=401, detail="Password incorrect; nothing was deleted.")

    from core.identity.service import revoke_all_sessions

    revoked = revoke_all_sessions(db, user.id, actor=f"user:{user.username}")

    deleted = {}
    for model in (ConversationTurn, Memory, Task):
        rows = db.scalars(select(model).where(model.user_id == user.id)).all()
        deleted[model.__tablename__] = len(rows)
        for row in rows:
            db.delete(row)
    user.is_active = False
    user.email = None
    db.commit()

    remaining = sum(
        db.scalar(select(func.count(m.id)).where(m.user_id == user.id)) or 0
        for m in (ConversationTurn, Memory, Task)
    )
    audit(
        db, "account_deletion_completed",
        f"user_id={user.id} sessions_revoked={revoked} rows_deleted={deleted} remaining={remaining}",
        actor=f"user:{user.username}", component="privacy",
    )
    return {
        "status": "DELETION_COMPLETED" if remaining == 0 else "DELETION_PARTIALLY_COMPLETED",
        "sessions_revoked": revoked,
        "deleted": deleted,
        "note": (
            "Your account is disabled and your conversations, memories and tasks "
            "were removed. Audit entries record only that the deletion happened."
        ),
    }


@router.put("/conversation-history")
def toggle_history(body: HistoryToggle, user: CurrentUser, db: DbDep):
    user.conversation_history_enabled = body.enabled
    db.commit()
    audit(db, "conversation_history_toggled",
          f"enabled={body.enabled}", actor=f"user:{user.username}", component="privacy")
    return {"ok": True, "conversation_history_enabled": body.enabled}


@router.put("/long-term-memory")
def toggle_memory(body: MemoryToggle, user: CurrentUser, db: DbDep):
    user.long_term_memory_enabled = body.enabled
    db.commit()
    audit(db, "long_term_memory_toggled",
          f"enabled={body.enabled}", actor=f"user:{user.username}", component="privacy")
    return {"ok": True, "long_term_memory_enabled": body.enabled}


def _now() -> str:
    from timeutil import utcnow

    return utcnow().isoformat() + "Z"
