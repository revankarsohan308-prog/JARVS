"""Notification centre (Reliability Addendum Module 8).

Honest by construction:
- Severity is one of INFO/LOW/MEDIUM/HIGH/CRITICAL.
- Repeats are de-duplicated within a cooldown window (no notification storms);
  the existing row's ``occurrences`` counter increments instead.
- Every delivery/read/acknowledgement is recorded with a real timestamp.
- Only the in-app channel is CONFIGURED. Email/SMS/webhook channels report
  NOT_CONFIGURED with the reason — they never claim to have delivered.
- Escalation is bounded: an unacknowledged HIGH/CRITICAL item is re-raised once
  per ``notification_escalation_minutes``, never in a tight loop.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import settings
from database.models import Notification, NotificationPreference, User
from security.audit import audit
from timeutil import utcnow

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
CATEGORIES = ("HEALTH", "MISSION", "SECURITY", "RELEASE", "SYSTEM")

# Longer cooldowns for lower severities: a flapping INFO should not nag.
_COOLDOWN_BY_SEVERITY = {
    "CRITICAL": 300,
    "HIGH": 900,
    "MEDIUM": 3600,
    "LOW": 21600,
    "INFO": 86400,
}

EXTERNAL_CHANNELS = ("EMAIL", "SMS", "WEBHOOK", "PUSH")


def _rank(severity: str) -> int:
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return 0


def normalize_severity(severity: str) -> str:
    value = (severity or "").upper()
    return value if value in SEVERITIES else "INFO"


def normalize_category(category: str) -> str:
    value = (category or "").upper()
    return value if value in CATEGORIES else "SYSTEM"


def channel_status() -> dict[str, dict[str, str]]:
    status = {"IN_APP": {"status": "CONFIGURED", "detail": "Durable in-app notification centre."}}
    for channel in EXTERNAL_CHANNELS:
        status[channel] = {
            "status": "NOT_CONFIGURED",
            "detail": (
                f"No {channel.lower()} provider credentials are configured, so "
                "nothing is sent externally. This is reported, never assumed."
            ),
        }
    return status


# ----------------------------------------------------------------------
# Preferences
# ----------------------------------------------------------------------

def get_preferences(db: Session, user_id: int) -> NotificationPreference:
    row = db.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    if row is None:
        row = NotificationPreference(user_id=user_id)
        db.add(row)
        db.commit()
    return row


def set_preferences(
    db: Session, user_id: int, *, min_severity: str, muted_categories: list[str]
) -> NotificationPreference:
    row = get_preferences(db, user_id)
    row.min_severity = normalize_severity(min_severity)
    row.muted_categories_json = json.dumps(
        sorted({normalize_category(c) for c in muted_categories})
    )
    db.commit()
    audit(db, "notification_preferences_updated", f"min_severity={row.min_severity}",
          actor=f"user_id:{user_id}", component="notifications")
    return row


# ----------------------------------------------------------------------
# Creation
# ----------------------------------------------------------------------

def notify(
    db: Session,
    user_id: int,
    *,
    category: str,
    severity: str,
    title: str,
    body: str = "",
    dedup_key: str | None = None,
    correlation_id: str | None = None,
    actor: str = "system",
) -> tuple[Notification | None, str]:
    """Create or de-duplicate a notification. Returns (row, reason)."""
    if not settings.notifications_enabled:
        return None, "disabled"
    category = normalize_category(category)
    severity = normalize_severity(severity)

    prefs = get_preferences(db, user_id)
    muted = set(json.loads(prefs.muted_categories_json or "[]"))
    if category in muted or _rank(severity) < _rank(prefs.min_severity):
        return None, "suppressed_preference"

    now = utcnow()
    key = (dedup_key or f"{category}:{title}")[:160]
    if dedup_key:
        existing = db.scalar(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.dedup_key == key,
                Notification.status.in_(("UNREAD", "ACKNOWLEDGED", "ESCALATED")),
            )
            .order_by(Notification.updated_at.desc())
            .limit(1)
        )
        if existing is not None:
            cooldown = _COOLDOWN_BY_SEVERITY.get(severity, settings.notification_cooldown_seconds)
            if (now - existing.updated_at) < timedelta(seconds=cooldown):
                existing.occurrences += 1
                existing.body = body or existing.body
                existing.updated_at = now
                db.commit()
                return existing, "deduped"

    row = Notification(
        user_id=user_id,
        category=category,
        severity=severity,
        title=title[:255],
        body=body[:4000],
        dedup_key=key,
        correlation_id=correlation_id,
        delivered_at=now,
    )
    db.add(row)
    db.commit()
    if _rank(severity) >= _rank("HIGH"):
        audit(db, "notification_raised",
              f"user_id={user_id} category={category} severity={severity} title={title[:80]}",
              actor=actor, component="notifications", correlation_id=correlation_id)
    return row, "created"


def notify_roles(
    db: Session,
    roles: tuple[str, ...] | list[str],
    **kwargs,
) -> int:
    """Fan a notification out to every active user holding one of ``roles``."""
    users = db.scalars(
        select(User).where(User.role.in_(tuple(roles)), User.is_active.is_(True))
    ).all()
    created = 0
    for user in users:
        _, reason = notify(db, user.id, **kwargs)
        if reason == "created":
            created += 1
    return created


def notify_admins(db: Session, **kwargs) -> int:
    return notify_roles(db, ("SUPER_ADMIN", "COLLEGE_ADMIN"), **kwargs)


# ----------------------------------------------------------------------
# Reads and lifecycle
# ----------------------------------------------------------------------

def list_for_user(
    db: Session, user_id: int, *, status: str | None = None, limit: int = 100
) -> list[Notification]:
    stmt = select(Notification).where(Notification.user_id == user_id)
    if status:
        stmt = stmt.where(Notification.status == status.upper())
    return list(db.scalars(stmt.order_by(Notification.created_at.desc()).limit(min(limit, 500))))


def unread_count(db: Session, user_id: int) -> int:
    return (
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.status.in_(("UNREAD", "ESCALATED")),
            )
        )
        or 0
    )


def get_owned(db: Session, user_id: int, notification_id: str) -> Notification | None:
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user_id:
        return None
    return row


def mark_read(db: Session, user_id: int, notification_id: str) -> Notification | None:
    row = get_owned(db, user_id, notification_id)
    if row is None:
        return None
    if row.read_at is None:
        row.read_at = utcnow()
    if row.status == "UNREAD":
        row.status = "READ"
    db.commit()
    return row


def acknowledge(db: Session, user_id: int, notification_id: str) -> Notification | None:
    row = get_owned(db, user_id, notification_id)
    if row is None:
        return None
    now = utcnow()
    row.read_at = row.read_at or now
    row.acknowledged_at = now
    row.status = "ACKNOWLEDGED"
    db.commit()
    audit(db, "notification_acknowledged", f"id={notification_id}", actor=f"user_id:{user_id}",
          component="notifications")
    return row


def escalate(db: Session, user_id: int, notification_id: str) -> Notification | None:
    row = get_owned(db, user_id, notification_id)
    if row is None:
        return None
    if _rank(row.severity) < _rank("HIGH"):
        row.severity = "HIGH"
    row.status = "ESCALATED"
    row.escalated_at = utcnow()
    db.commit()
    audit(db, "notification_escalated", f"id={notification_id}", actor=f"user_id:{user_id}",
          component="notifications")
    return row


def escalate_stale(db: Session, now=None) -> int:
    """Bounded automatic escalation sweep for unacknowledged HIGH/CRITICAL."""
    now = now or utcnow()
    threshold = now - timedelta(minutes=settings.notification_escalation_minutes)
    rows = db.scalars(
        select(Notification).where(
            Notification.severity.in_(("HIGH", "CRITICAL")),
            Notification.status.in_(("UNREAD", "READ")),
            Notification.updated_at < threshold,
        )
    ).all()
    escalated = 0
    for row in rows:
        last_marker = row.escalated_at or row.updated_at
        # Only re-raise once per escalation window.
        if (now - last_marker) < timedelta(minutes=settings.notification_escalation_minutes):
            continue
        row.status = "ESCALATED"
        row.escalated_at = now
        row.occurrences += 1
        escalated += 1
    if rows:
        db.commit()
    return escalated


def _serialize(row: Notification) -> dict:
    return {
        "id": row.id,
        "category": row.category,
        "severity": row.severity,
        "title": row.title,
        "body": row.body,
        "status": row.status,
        "channel": row.channel,
        "occurrences": row.occurrences,
        "correlation_id": row.correlation_id,
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
        "delivered_at": row.delivered_at.isoformat() + "Z" if row.delivered_at else None,
        "read_at": row.read_at.isoformat() + "Z" if row.read_at else None,
        "acknowledged_at": row.acknowledged_at.isoformat() + "Z" if row.acknowledged_at else None,
        "escalated_at": row.escalated_at.isoformat() + "Z" if row.escalated_at else None,
    }


serialize = _serialize
