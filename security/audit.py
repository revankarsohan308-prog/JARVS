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
    correlation_id: str | None = None,
) -> AuditLog:
    """Record an audited event. Commits immediately so it survives later failures.

    ``correlation_id`` ties every row belonging to one logical action together
    (a mission, a request) so the full trail can be reconstructed later.
    """
    entry = AuditLog(
        event=event_name,
        actor=actor,
        detail=sanitize(detail),
        component=component,
        correlation_id=correlation_id,
    )
    db.add(entry)
    db.commit()
    return entry


def recent_events(
    db: Session, limit: int = 100, correlation_id: str | None = None
) -> list[AuditLog]:
    query = db.query(AuditLog)
    if correlation_id:
        query = query.filter(AuditLog.correlation_id == correlation_id)
    return query.order_by(AuditLog.id.desc()).limit(min(limit, 500)).all()


def trail(db: Session, correlation_id: str) -> list[AuditLog]:
    """Full chronological trail for one correlation id (oldest first)."""
    return (
        db.query(AuditLog)
        .filter(AuditLog.correlation_id == correlation_id)
        .order_by(AuditLog.id.asc())
        .all()
    )


@event.listens_for(AuditLog, "before_update")
def _reject_update(_mapper, _connection, _target):  # pragma: no cover - defensive hook
    raise RuntimeError("Audit logs are append-only; updates are forbidden.")


@event.listens_for(AuditLog, "before_delete")
def _reject_delete(_mapper, _connection, _target):  # pragma: no cover - defensive hook
    raise RuntimeError("Audit logs are append-only; deletion is forbidden.")
