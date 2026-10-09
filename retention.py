"""Retention engine (spec §16.7 / §16.19 / §16.20).

Design rules:
- Periods come from config.settings — never hard-coded here.
- Idempotent: running twice on unchanged data deletes nothing the second time.
- Never stores deleted content in any log; only counts.
- Security/legal hold: rows referenced by an OPEN security event are skipped.
- Truthful: returns per-category counts of what was actually deleted.
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from config.settings import settings
from database.models import (
    AuditLog,
    ConversationTurn,
    Memory,
    PendingConfirmation,
    SecurityEvent,
    SessionToken,
    Task,
)
from timeutil import utcnow


def retention_policy() -> dict[str, int]:
    """Effective day-counts for every auto-deleting category."""
    return {
        "conversation_turns": settings.retention_conversations_days,
        "completed_tasks": settings.retention_task_history_days,
        "security_events": settings.retention_security_events_days,
        "critical_security_incidents": settings.retention_critical_incident_days,
        "audit_logs": settings.retention_audit_days,
        "temporary_files": settings.retention_temp_days,
        "long_term_memories": 0,  # 0 = keep until the user deletes it
    }


def apply_retention(db: Session, now=None) -> dict[str, int]:
    """Delete expired rows. Returns counts actually deleted (idempotent)."""
    now = now or utcnow()
    deleted: dict[str, int] = {}

    deleted["conversation_turns"] = _purge_expired(
        db, ConversationTurn, now - timedelta(days=settings.retention_conversations_days),
        getattr(ConversationTurn, "created_at"),
    )

    # Tasks: only completed ones age out, and only after the history period.
    cutoff = now - timedelta(days=settings.retention_task_history_days)
    stale = db.scalars(
        select(Task).where(
            Task.status.in_(("done", "cancelled")),
            Task.completed_at.isnot(None),
            Task.completed_at < cutoff,
        )
    ).all()
    for row in stale:
        db.delete(row)
    deleted["completed_tasks"] = len(stale)

    # Security events: evidence from OPEN events/incidents is under an
    # automatic security hold (§16.20) and never ages out.
    held_count = (
        db.scalar(
            select(func.count(SecurityEvent.id)).where(SecurityEvent.status == "OPEN")
        )
        or 0
    )
    cutoff = now - timedelta(days=settings.retention_security_events_days)
    held_cutoff = now - timedelta(days=settings.retention_critical_incident_days)
    stale_events = db.scalars(
        select(SecurityEvent).where(
            SecurityEvent.status != "OPEN",
            or_(
                and_(SecurityEvent.severity.in_(("HIGH", "CRITICAL")), SecurityEvent.created_at < held_cutoff),
                and_(SecurityEvent.severity.notin_(("HIGH", "CRITICAL")), SecurityEvent.created_at < cutoff),
            ),
        )
    ).all()
    for row in stale_events:
        db.delete(row)
    deleted["security_events"] = len(stale_events)
    deleted["security_events_on_hold"] = held_count

    # Audit logs age out per policy, but the newest 24h are always kept so the
    # audit trail can never be emptied by a mis-set policy.
    # cutoff = min(retention cutoff, 24h ago): rows newer than either survive.
    audit_floor = now - timedelta(days=settings.retention_audit_days)
    audit_keep = now - timedelta(days=1)
    effective_cutoff = min(audit_floor, audit_keep)
    stale_audit = db.scalars(select(AuditLog).where(AuditLog.created_at < effective_cutoff)).all()
    for row in stale_audit:
        db.delete(row)
    deleted["audit_logs"] = len(stale_audit)

    # Expired/revoked sessions and resolved confirmations.
    stale_sessions = db.scalars(
        select(SessionToken).where(
            or_(
                SessionToken.expires_at < now - timedelta(days=1),
                SessionToken.revoked.is_(True),
            )
        )
    ).all()
    for row in stale_sessions:
        db.delete(row)
    deleted["sessions"] = len(stale_sessions)

    resolved_cutoff = now - timedelta(days=settings.retention_temp_days)
    stale_conf = db.scalars(
        select(PendingConfirmation).where(
            PendingConfirmation.resolved.is_(True),
            PendingConfirmation.created_at < resolved_cutoff,
        )
    ).all()
    for row in stale_conf:
        db.delete(row)
    deleted["resolved_confirmations"] = len(stale_conf)

    # Memories with an explicit expiry past their window (Module 4). Memories
    # with the default KEEP policy never age out here.
    from core.memory import service as memory_service

    deleted["expired_memories"] = memory_service.purge_expired(db, now)

    deleted["temporary_files"] = _purge_temp_files(now)
    db.commit()
    return deleted


def _purge_expired(db: Session, model, cutoff, timestamp_col) -> int:
    rows = db.scalars(select(model).where(timestamp_col < cutoff)).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def _purge_temp_files(now) -> int:
    """Delete cache files older than the temp retention period (§16.3)."""

    tmp = settings.tmp_dir
    if not tmp.is_dir():
        return 0
    cutoff = time.time() - settings.retention_temp_days * 86400
    removed = 0
    for entry in tmp.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def privacy_audit_report(db: Session) -> dict:
    """Human-readable privacy report for administrators (spec §16.15)."""
    counts = {
        "conversation_turns": _count(db, ConversationTurn),
        "memories": _count(db, Memory),
        "tasks": _count(db, Task),
        "security_events": _count(db, SecurityEvent),
        "audit_logs": _count(db, AuditLog),
        "active_sessions": db.scalar(
            select(func.count(SessionToken.id)).where(SessionToken.revoked.is_(False))
        )
        or 0,
    }
    return {
        "what_is_stored": counts,
        "why": {
            "conversation_turns": "Context for ongoing conversations.",
            "memories": "Facts you asked ALYO to remember.",
            "tasks": "Your tasks and reminders.",
            "security_events": "Threat/bug detection and incident response.",
            "audit_logs": "Accountability for privileged actions.",
            "active_sessions": "Authentication for your devices.",
        },
        "retention_days": retention_policy(),
        "who_can_access": "Owners of their own rows; administrators only for "
                          "security/audit categories per RBAC.",
        "how_to_delete": "Privacy endpoints: /api/v1/privacy/conversations, "
                         "/api/v1/memory/{id}, /api/v1/tasks/{id}, "
                         "/api/v1/privacy/account-deletion.",
        "last_cleanup": _last_cleanup(db),
        "generated_at": utcnow().isoformat() + "Z",
    }


def _last_cleanup(db: Session) -> str | None:
    row = db.scalar(
        select(AuditLog)
        .where(AuditLog.event == "retention_cleanup")
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    return (row.created_at.isoformat() + "Z") if row else None


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count(model.id))) or 0
