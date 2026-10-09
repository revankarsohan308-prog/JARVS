"""Long-term memory: user-controlled, secret-rejecting, per-user isolated.

Rules (spec §5 / §16.5; Reliability Addendum Module 4):
- Writes are explicit: memory is stored when the user asks (or confirms).
- Secrets (passwords, API keys, tokens, OTPs) are refused and audited.
- Every query is scoped by user_id — a shared device never mixes memories.
- Deletion is real: once delete() returns True the row is gone and can no
  longer appear in search results.
- Quality is explicit: every memory carries confidence, importance, a
  verification state, provenance and a retention policy.
- Contradictions are never silent: overwriting a differing value preserves the
  old value as a revision and flags the memory as CONFLICT for review.
- Duplicate detection is available; merging is user-approved and reversible
  because each source value is preserved as a revision first.
"""

from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Memory, MemoryRevision
from security.audit import audit
from timeutil import utcnow

CATEGORIES = (
    "PREFERENCES",
    "IMPORTANT_FACTS",
    "PROJECTS",
    "TASKS",
    "CONVERSATION_SUMMARY",
)

RETENTION_POLICIES = ("KEEP", "WINDOW")

VERIFICATION_STATES = ("UNVERIFIED", "USER_VERIFIED", "CONFLICT", "EXPIRED")

_SECRET_RE = re.compile(
    r"\b(password|passwd|secret|token|api[_-]?key|otp|pin)\b\s*[=::]?\s*\S+"
    r"|\bsk-[A-Za-z0-9]{16,}\b"
    r"|\bbearer\s+[A-Za-z0-9._-]{16,}\b",
    re.IGNORECASE,
)


class MemoryRejected(Exception):
    """Raised when a write is refused; message is safe to show the user."""


class MemoryConflict(MemoryRejected):
    """Raised when a write contradicts an existing memory and needs a decision."""


def _snapshot(db: Session, row: Memory, reason: str) -> MemoryRevision:
    revision = MemoryRevision(
        memory_id=row.id,
        user_id=row.user_id,
        category=row.category,
        key=row.key,
        content=row.content,
        source=row.source,
        confidence=row.confidence,
        importance=row.importance,
        verification_state=row.verification_state,
        reason=reason,
    )
    db.add(revision)
    return revision


def store(
    db: Session,
    user_id: int,
    key: str,
    content: str,
    *,
    category: str = "IMPORTANT_FACTS",
    source: str = "user",
    confidence: float = 1.0,
    importance: int = 3,
    verification_state: str = "UNVERIFIED",
    source_ref: str = "",
    retention_policy: str = "KEEP",
    expires_at: datetime | None = None,
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
    if retention_policy not in RETENTION_POLICIES:
        retention_policy = "KEEP"
    confidence = min(1.0, max(0.0, float(confidence)))
    importance = min(5, max(1, int(importance)))

    existing = db.scalar(
        select(Memory).where(
            Memory.user_id == user_id, Memory.category == category, Memory.key == key
        )
    )
    if existing:
        changed = existing.content != content
        if changed:
            # Preserve the old value before replacing it: no silent overwrite.
            _snapshot(db, existing, "overwrite")
        existing.content = content
        existing.updated_at = utcnow()
        existing.source = source
        existing.source_ref = source_ref
        existing.retention_policy = retention_policy
        existing.expires_at = expires_at
        existing.importance = importance
        if changed:
            # A contradiction is surfaced, not hidden, until the user decides.
            existing.verification_state = "CONFLICT"
            existing.confidence = min(existing.confidence, 0.5)
        else:
            existing.confidence = confidence
            if verification_state != "UNVERIFIED":
                existing.verification_state = verification_state
        db.commit()
        if changed:
            audit(db, "memory_conflict_detected", f"key={key!r} category={category}",
                  actor=actor, component="memory")
            audit(db, "memory_updated", f"key={key!r} category={category} prior_preserved=true",
                  actor=actor, component="memory")
        else:
            audit(db, "memory_updated", f"key={key!r} category={category}",
                  actor=actor, component="memory")
        return existing

    row = Memory(
        user_id=user_id,
        category=category,
        key=key,
        content=content,
        source=source,
        confidence=confidence,
        importance=importance,
        verification_state=verification_state,
        source_ref=source_ref,
        retention_policy=retention_policy,
        expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    audit(db, "memory_stored", f"key={key!r} category={category}", actor=actor, component="memory")
    return row


def search(
    db: Session, user_id: int, query: str, limit: int = 10, *, touch: bool = False
) -> list[Memory]:
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
    ranked = sorted(
        rows,
        key=lambda m: (
            0 if m.key.lower() == lowered else 1 if m.key.lower().startswith(lowered) else 2,
            -m.updated_at.timestamp(),
        ),
    )
    if touch and ranked:
        now = utcnow()
        for row in ranked:
            row.access_count += 1
            row.last_accessed_at = now
        db.commit()
    return ranked


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
    confidence: float | None = None,
    importance: int | None = None,
    retention_policy: str | None = None,
    expires_at: datetime | None = None,
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
        # Preserve the prior value so an edit is never a silent overwrite.
        _snapshot(db, row, "overwrite")
        row.content = content
        row.verification_state = "USER_VERIFIED"
        row.confidence = 1.0
    if key is not None and key.strip():
        row.key = key.strip()
    if category is not None and category in CATEGORIES:
        row.category = category
    if confidence is not None:
        row.confidence = min(1.0, max(0.0, float(confidence)))
    if importance is not None:
        row.importance = min(5, max(1, int(importance)))
    if retention_policy is not None and retention_policy in RETENTION_POLICIES:
        row.retention_policy = retention_policy
    if expires_at is not None:
        row.expires_at = expires_at
    db.commit()
    audit(db, "memory_updated", f"id={memory_id} key={row.key!r}", actor=actor, component="memory")
    return row


def correct(
    db: Session, user_id: int, memory_id: int, content: str, *, actor: str = "system"
) -> Memory | None:
    """Explicit user correction: records the old value, marks the new verified."""
    row = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
    if row is None:
        return None
    if _SECRET_RE.search(content):
        raise MemoryRejected("That correction looks like it contains a secret; not stored.")
    _snapshot(db, row, "correction")
    row.content = content
    row.verification_state = "USER_VERIFIED"
    row.confidence = 1.0
    row.updated_at = utcnow()
    db.commit()
    audit(db, "memory_corrected", f"id={memory_id} key={row.key!r}", actor=actor, component="memory")
    return row


def revert(db: Session, user_id: int, memory_id: int, revision_id: int, *, actor: str = "system") -> Memory | None:
    """Restore a previous revision (itself preserved as a new revision)."""
    row = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
    if row is None:
        return None
    revision = db.scalar(
        select(MemoryRevision).where(
            MemoryRevision.id == revision_id, MemoryRevision.memory_id == memory_id
        )
    )
    if revision is None:
        return None
    _snapshot(db, row, "revert")
    row.content = revision.content
    row.confidence = revision.confidence
    row.importance = revision.importance
    row.verification_state = "USER_VERIFIED"
    row.updated_at = utcnow()
    db.commit()
    audit(db, "memory_reverted", f"id={memory_id} revision={revision_id}", actor=actor, component="memory")
    return row


def revisions(db: Session, user_id: int, memory_id: int) -> list[MemoryRevision]:
    return list(
        db.scalars(
            select(MemoryRevision)
            .where(MemoryRevision.memory_id == memory_id, MemoryRevision.user_id == user_id)
            .order_by(MemoryRevision.id.desc())
        )
    )


def conflicts(db: Session, user_id: int) -> list[Memory]:
    return list(
        db.scalars(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.verification_state == "CONFLICT")
            .order_by(Memory.updated_at.desc())
        )
    )


def resolve_conflict(
    db: Session, user_id: int, memory_id: int, *, keep: str, actor: str = "system"
) -> Memory | None:
    """Resolve a flagged contradiction.

    ``keep='current'`` accepts the newest value; ``keep='previous'`` restores the
    most recent superseded value. Both mark the memory USER_VERIFIED.
    """
    row = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
    if row is None:
        return None
    if keep == "previous":
        last = db.scalar(
            select(MemoryRevision)
            .where(MemoryRevision.memory_id == memory_id, MemoryRevision.user_id == user_id)
            .order_by(MemoryRevision.id.desc())
            .limit(1)
        )
        if last is not None:
            _snapshot(db, row, "revert")
            row.content = last.content
            row.confidence = last.confidence
    row.verification_state = "USER_VERIFIED"
    row.confidence = max(row.confidence, 0.8)
    row.updated_at = utcnow()
    db.commit()
    audit(db, "memory_conflict_resolved", f"id={memory_id} keep={keep}", actor=actor, component="memory")
    return row


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def find_duplicates(
    db: Session, user_id: int, threshold: float = 0.85, scan_limit: int = 500
) -> list[dict]:
    """Near-duplicate memories by content similarity (read-only, per user)."""
    rows = list(
        db.scalars(
            select(Memory).where(Memory.user_id == user_id).order_by(Memory.id).limit(scan_limit)
        )
    )
    normalized = [(row, _normalize(row.content)) for row in rows]
    groups: list[dict] = []
    used: set[int] = set()
    for i in range(len(normalized)):
        a_row, a_text = normalized[i]
        if a_row.id in used or not a_text:
            continue
        cluster = [a_row]
        scores = [1.0]
        for j in range(i + 1, len(normalized)):
            b_row, b_text = normalized[j]
            if b_row.id in used or not b_text:
                continue
            ratio = SequenceMatcher(None, a_text, b_text).ratio()
            if ratio >= threshold:
                cluster.append(b_row)
                scores.append(ratio)
        if len(cluster) > 1:
            for row in cluster:
                used.add(row.id)
            groups.append(
                {
                    "similarity": round(min(scores[1:]), 3) if len(scores) > 1 else 1.0,
                    "memories": [
                        {"id": r.id, "key": r.key, "content": r.content, "category": r.category}
                        for r in cluster
                    ],
                }
            )
    return groups


def merge(
    db: Session,
    user_id: int,
    target_id: int,
    source_ids: list[int],
    merged_content: str | None = None,
    *,
    actor: str = "system",
) -> Memory | None:
    """User-approved merge. Every source value is preserved as a revision."""
    target = db.scalar(select(Memory).where(Memory.id == target_id, Memory.user_id == user_id))
    if target is None:
        return None
    source_ids = [sid for sid in source_ids if sid != target_id]
    sources = list(
        db.scalars(
            select(Memory).where(Memory.user_id == user_id, Memory.id.in_(source_ids))
        )
    )
    if not sources:
        raise MemoryRejected("No matching source memories to merge.")
    _snapshot(db, target, "merge")
    contents = [target.content] + [s.content for s in sources]
    if merged_content:
        target.content = merged_content
    else:
        # Join distinct values rather than dropping any.
        seen: list[str] = []
        for value in contents:
            if value not in seen:
                seen.append(value)
        target.content = "; ".join(seen)
    target.importance = max([target.importance] + [s.importance for s in sources])
    target.confidence = max(target.confidence, 0.8)
    target.verification_state = "USER_VERIFIED"
    target.updated_at = utcnow()
    for s in sources:
        _snapshot(db, s, "merge")
        db.delete(s)
    db.commit()
    audit(
        db,
        "memory_merged",
        f"target={target_id} sources={sorted(source_ids)}",
        actor=actor,
        component="memory",
    )
    return target


def purge_expired(db: Session, now: datetime | None = None) -> int:
    """Delete memories past their retention expiry. Returns count deleted."""
    now = now or utcnow()
    rows = list(
        db.scalars(
            select(Memory).where(Memory.expires_at.isnot(None), Memory.expires_at < now)
        )
    )
    for row in rows:
        row.verification_state = "EXPIRED"
    db.commit()
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


def record_access(db: Session, user_id: int, memory_id: int) -> Memory | None:
    row = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
    if row is None:
        return None
    row.access_count += 1
    row.last_accessed_at = utcnow()
    db.commit()
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
    by_verification = dict(
        db.execute(
            select(Memory.verification_state, func.count(Memory.id))
            .where(Memory.user_id == user_id)
            .group_by(Memory.verification_state)
        ).all()
    )
    conflicts_count = by_verification.get("CONFLICT", 0)
    return {
        "total": total,
        "by_category": by_category,
        "by_verification": by_verification,
        "conflicts": conflicts_count,
        "revisions": db.scalar(
            select(func.count(MemoryRevision.id)).where(MemoryRevision.user_id == user_id)
        )
        or 0,
    }
