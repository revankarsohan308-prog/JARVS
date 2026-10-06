"""Append-only audit logging.

Every privileged decision lands here. Rows are never updated or deleted by
application code — the SQLAlchemy event hooks below reject mutation so a bug
or a careless handler cannot rewrite history. Content is sanitized so
passwords, tokens and API keys never reach the table.
"""

from __future__ import annotations

import re

from sqlalchemy import event
from sqlalchemy.orm import Session

from database.models import AuditLog

# Anything matching these patterns is redacted before persisting.
_SECRET_PATTERNS = [
    re.compile(r"(password|passwd|pin|secret|token|api[_-]?key|authorization)\s*[=:]\s*\S+", re.I),
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),  # long hex blobs (hashes/tokens)
]


def sanitize(detail: str) -> str:
    text = detail or ""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:4000]


def audit(
    db: Session,
    event_name: str,
    detail: str = "",
    *,
    actor: str = "system",
    component: str = "core",
) -> AuditLog:
    """Record an audited event. Commits immediately so it survives later failures."""
    entry = AuditLog(
        event=event_name,
        actor=actor,
        detail=sanitize(detail),
        component=component,
    )
    db.add(entry)
    db.commit()
    return entry


def recent_events(db: Session, limit: int = 100) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .order_by(AuditLog.id.desc())
        .limit(min(limit, 500))
        .all()
    )


@event.listens_for(AuditLog, "before_update")
def _reject_update(_mapper, _connection, _target):  # pragma: no cover - defensive hook
    raise RuntimeError("Audit logs are append-only; updates are forbidden.")


@event.listens_for(AuditLog, "before_delete")
def _reject_delete(_mapper, _connection, _target):  # pragma: no cover - defensive hook
    raise RuntimeError("Audit logs are append-only; deletion is forbidden.")
