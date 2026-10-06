"""Long-term memory: user-controlled, secret-rejecting, per-user isolated.

Rules (spec §5 / §16.5):
- Writes are explicit: memory is stored when the user asks (or confirms).
- Secrets (passwords, API keys, tokens, OTPs) are refused and audited.
- Every query is scoped by user_id — a shared device never mixes memories.
- Deletion is real: once delete() returns True the row is gone and can no
  longer appear in search results.
"""

from __future__ import annotations

import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Memory
from security.audit import audit
from timeutil import utcnow

CATEGORIES = (
    "PREFERENCES",
    "IMPORTANT_FACTS",
    "PROJECTS",
    "TASKS",
    "CONVERSATION_SUMMARY",
)

_SECRET_RE = re.compile(
    r"\b(password|passwd|secret|token|api[_-]?key|otp|pin)\b\s*[=::]?\s*\S+"
    r"|\bsk-[A-Za-z0-9]{16,}\b"
    r"|\bbearer\s+[A-Za-z0-9._-]{16,}\b",
    re.IGNORECASE,
)


class MemoryRejected(Exception):
    """Raised when a write is refused; message is safe to show the user."""


def store(
    db: Session,
    user_id: int,
    key: str,
    content: str,
    *,
    category: str = "IMPORTANT_FACTS",
    source: str = "user",
    actor: str = "system",
) -> Memory:
    key = (key or "").strip()
    content = (content or "").strip()
    if not key or not content:
        raise MemoryRejected("Memory needs both a key and content.")

    from database.models import User as UserModel

    owner = db.get(UserModel, user_id)
    if owner is not None and not owner.long_term_memory_enabled:
        audit(
            db,
            "memory_rejected_disabled",
            f"key={key!r} reason=long_term_memory_disabled",
            actor=actor,
            component="memory",
        )
        raise MemoryRejected(
            "Long-term memory is turned off in your Privacy settings, so I "
            "didn't store anything. You can re-enable it under Privacy."
        )

    if _SECRET_RE.search(content) or _SECRET_RE.search(key):
        audit(
            db,
            "memory_rejected_sensitive",
            f"key={key!r} reason=secret_pattern",
            actor=actor,
            component="memory",
        )
        raise MemoryRejected(
            "I can't store that — it looks like a password, API key or token. "
            "Keep secrets in your password manager instead."
        )
    if category not in CATEGORIES:
        category = "IMPORTANT_FACTS"

    existing = db.scalar(
        select(Memory).where(
            Memory.user_id == user_id, Memory.category == category, Memory.key == key
        )
    )
    if existing:
        existing.content = content
        existing.updated_at = utcnow()
        existing.source = source
        db.commit()
        audit(db, "memory_updated", f"key={key!r} category={category}", actor=actor, component="memory")
        return existing

    row = Memory(user_id=user_id, category=category, key=key, content=content, source=source)
    db.add(row)
    db.commit()
    audit(db, "memory_stored", f"key={key!r} category={category}", actor=actor, component="memory")
    return row


def search(db: Session, user_id: int, query: str, limit: int = 10) -> list[Memory]:
    """Ranked substring search over this user's memories only."""
    query = (query or "").strip()
    if not query:
        return []
    pattern = f"%{query}%"
    rows = db.scalars(
        select(Memory)
        .where(Memory.user_id == user_id, or_(Memory.key.like(pattern), Memory.content.like(pattern)))
        .order_by(Memory.updated_at.desc())
        .limit(limit)
    ).all()
    # Exact key matches first, then prefix, then substring.
    lowered = query.lower()
    return sorted(
        rows,
        key=lambda m: (
            0 if m.key.lower() == lowered else 1 if m.key.lower().startswith(lowered) else 2,
            -m.updated_at.timestamp(),
        ),
    )


def list_memories(
    db: Session, user_id: int, category: str | None = None, limit: int = 200
) -> list[Memory]:
    stmt = select(Memory).where(Memory.user_id == user_id)
    if category:
        stmt = stmt.where(Memory.category == category)
    return db.scalars(stmt.order_by(Memory.updated_at.desc()).limit(limit)).all()


def update(
    db: Session,
    user_id: int,
    memory_id: int,
    *,
    content: str | None = None,
    key: str | None = None,
    category: str | None = None,
    actor: str = "system",
) -> Memory | None:
    row = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
    if row is None:
        return None
    if content is not None:
        if _SECRET_RE.search(content):
            audit(db, "memory_rejected_sensitive", f"id={memory_id} reason=secret_pattern_on_update",
                  actor=actor, component="memory")
            raise MemoryRejected("That update looks like it contains a secret; not stored.")
        row.content = content
    if key is not None and key.strip():
        row.key = key.strip()
    if category is not None and category in CATEGORIES:
        row.category = category
    db.commit()
    audit(db, "memory_updated", f"id={memory_id} key={row.key!r}", actor=actor, component="memory")
    return row


def delete(db: Session, user_id: int, memory_id: int, actor: str = "system") -> bool:
    row = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
    if row is None:
        return False
    # Audit records that a deletion occurred and which key — never the content.
    key, category = row.key, row.category
    db.delete(row)
    db.commit()
    audit(db, "memory_deleted", f"id={memory_id} key={key!r} category={category}",
          actor=actor, component="memory")
    return True


def stats(db: Session, user_id: int) -> dict[str, object]:
    total = db.scalar(
        select(func.count(Memory.id)).where(Memory.user_id == user_id)
    ) or 0
    by_category = dict(
        db.execute(
            select(Memory.category, func.count(Memory.id))
            .where(Memory.user_id == user_id)
            .group_by(Memory.category)
        ).all()
    )
    return {"total": total, "by_category": by_category}
