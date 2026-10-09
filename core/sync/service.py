"""Cross-device sync (Reliability Addendum Module 5).

A per-user, versioned change log with real optimistic-concurrency conflict
detection. Design rules:

- Every entity has a monotonically increasing ``version``. A push carries the
  ``base_version`` the client edited from; a mismatch is a conflict.
- Conflicts are surfaced, never silently last-write-wins. Sensitive entity
  types (memory, security, privacy, credentials) *always* require an explicit
  resolution — a client cannot opt into overwriting them.
- Offline queues are safe to retry: a repeated ``client_change_id`` is
  idempotent and returns the original outcome (no double apply).
- Secrets are refused before they ever enter the sync log.
- Pull is cursor-based on the append-only change log (``since`` = last change
  id the client has), so devices can resynchronise after being offline.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import SyncChange, SyncRecord
from security.audit import audit
from timeutil import utcnow

# Entity types whose concurrent edits must never be auto-overwritten.
SENSITIVE_ENTITIES = {"MEMORY", "SECURITY", "PRIVACY", "CREDENTIALS"}

_SECRET_RE = re.compile(
    r"\b(password|passwd|secret|token|api[_-]?key|otp|pin)\b\s*[=::]?\s*\S+"
    r"|\bsk-[A-Za-z0-9]{16,}\b"
    r"|\bbearer\s+[A-Za-z0-9._-]{16,}\b",
    re.IGNORECASE,
)


def is_sensitive(entity_type: str) -> bool:
    return (entity_type or "").upper() in SENSITIVE_ENTITIES


def _serialize_change(change: SyncChange) -> dict:
    return {
        "cursor": change.id,
        "entity_type": change.entity_type,
        "entity_key": change.entity_key,
        "op": change.op,
        "version": change.version,
        "device_id": change.device_id,
        "payload": json.loads(change.payload_json or "{}"),
        "created_at": change.created_at.isoformat() + "Z",
    }


def _serialize_record(record: SyncRecord) -> dict:
    return {
        "entity_type": record.entity_type,
        "entity_key": record.entity_key,
        "version": record.version,
        "op": record.op,
        "sensitive": record.sensitive,
        "conflict_state": record.conflict_state,
        "pending_payload": (
            json.loads(record.pending_payload_json)
            if record.pending_payload_json
            else None
        ),
        "payload": json.loads(record.payload_json or "{}"),
        "updated_at": record.updated_at.isoformat() + "Z",
    }


def current_cursor(db: Session, user_id: int) -> int:
    return (
        db.scalar(select(func.max(SyncChange.id)).where(SyncChange.user_id == user_id)) or 0
    )


def state(db: Session, user_id: int) -> dict:
    records = db.scalar(
        select(func.count(SyncRecord.id)).where(SyncRecord.user_id == user_id)
    ) or 0
    conflicts = db.scalar(
        select(func.count(SyncRecord.id)).where(
            SyncRecord.user_id == user_id, SyncRecord.conflict_state == "CONFLICT"
        )
    ) or 0
    return {"cursor": current_cursor(db, user_id), "records": records, "conflicts": conflicts}


def pull(db: Session, user_id: int, *, since: int = 0, limit: int = 200) -> dict:
    """Return changes after ``since`` for this user (oldest first)."""
    limit = min(max(limit, 1), 500)
    rows = list(
        db.scalars(
            select(SyncChange)
            .where(SyncChange.user_id == user_id, SyncChange.id > since)
            .order_by(SyncChange.id.asc())
            .limit(limit)
        )
    )
    cursor = rows[-1].id if rows else since
    return {
        "changes": [_serialize_change(c) for c in rows],
        "cursor": cursor,
        "has_more": len(rows) == limit,
    }


def _append_change(
    db: Session,
    user_id: int,
    *,
    entity_type: str,
    entity_key: str,
    op: str,
    version: int,
    device_id: str,
    client_change_id: str | None,
    payload: dict,
) -> SyncChange:
    change = SyncChange(
        user_id=user_id,
        entity_type=entity_type,
        entity_key=entity_key,
        op=op,
        version=version,
        device_id=device_id,
        client_change_id=client_change_id,
        payload_json=json.dumps(payload),
    )
    db.add(change)
    return change


def push(
    db: Session,
    user_id: int,
    device_id: str,
    changes: list[dict],
    *,
    on_conflict: str = "reject",
    actor: str = "system",
) -> list[dict]:
    """Apply a batch of client changes. Returns one result per change.

    Result status is one of: APPLIED, APPLIED_LWW, CONFLICT, REJECTED,
    REJECTED_SECRET, DUPLICATE.
    """
    results: list[dict] = []
    on_conflict = (on_conflict or "reject").lower()

    for raw in changes:
        entity_type = (raw.get("entity_type") or "").upper()
        entity_key = (raw.get("entity_key") or "").strip()
        op = (raw.get("op") or "UPSERT").upper()
        payload = raw.get("payload") or {}
        base_version = raw.get("base_version")
        client_change_id = raw.get("client_change_id") or None

        if not entity_type or not entity_key:
            results.append({"status": "REJECTED", "reason": "entity_type and entity_key are required"})
            continue
        if op not in ("UPSERT", "DELETE"):
            results.append({"status": "REJECTED", "reason": f"unknown op {op!r}"})
            continue

        # Idempotent retry from an offline queue.
        if client_change_id:
            prior = db.scalar(
                select(SyncChange).where(
                    SyncChange.user_id == user_id,
                    SyncChange.client_change_id == client_change_id,
                )
            )
            if prior is not None:
                results.append(
                    {
                        "entity_type": entity_type,
                        "entity_key": entity_key,
                        "status": "DUPLICATE",
                        "version": prior.version,
                    }
                )
                continue

        if _SECRET_RE.search(json.dumps(payload)):
            audit(db, "sync_secret_rejected", f"entity={entity_type}:{entity_key}",
                  actor=actor, component="sync")
            results.append(
                {"entity_type": entity_type, "entity_key": entity_key,
                 "status": "REJECTED_SECRET"}
            )
            continue

        record = db.scalar(
            select(SyncRecord).where(
                SyncRecord.user_id == user_id,
                SyncRecord.entity_type == entity_type,
                SyncRecord.entity_key == entity_key,
            )
        )
        sensitive = is_sensitive(entity_type)

        if record is None:
            existing_version = 0
        else:
            existing_version = record.version or 0

        # A client based its edit on a version that is not current -> conflict.
        if record is not None and base_version is not None and int(base_version) != existing_version:
            if not sensitive and on_conflict == "lww":
                _apply(record, op, payload, device_id, actor, user_id, entity_type, entity_key,
                       client_change_id, db)
                results.append(
                    {"entity_type": entity_type, "entity_key": entity_key,
                     "status": "APPLIED_LWW", "version": record.version}
                )
            else:
                record.conflict_state = "CONFLICT"
                record.pending_payload_json = json.dumps(payload)
                record.pending_device_id = device_id
                db.commit()
                audit(
                    db,
                    "sync_conflict",
                    f"entity={entity_type}:{entity_key} base={base_version} current={existing_version} "
                    f"sensitive={sensitive}",
                    actor=actor,
                    component="sync",
                )
                results.append(
                    {"entity_type": entity_type, "entity_key": entity_key,
                     "status": "CONFLICT", "current_version": existing_version}
                )
            continue

        if record is None:
            record = SyncRecord(
                user_id=user_id,
                entity_type=entity_type,
                entity_key=entity_key,
                sensitive=sensitive,
            )
            db.add(record)
            db.flush()
        _apply(record, op, payload, device_id, actor, user_id, entity_type, entity_key,
               client_change_id, db)
        results.append(
            {"entity_type": entity_type, "entity_key": entity_key,
             "status": "APPLIED", "version": record.version}
        )

    db.commit()
    return results


def _apply(
    record: SyncRecord,
    op: str,
    payload: dict,
    device_id: str,
    actor: str,
    user_id: int,
    entity_type: str,
    entity_key: str,
    client_change_id: str | None,
    db: Session,
) -> None:
    record.version = (record.version or 0) + 1
    record.op = op
    record.payload_json = json.dumps(payload)
    record.device_id = device_id
    record.sensitive = is_sensitive(entity_type)
    record.conflict_state = "NONE"
    record.pending_payload_json = None
    record.pending_device_id = ""
    record.updated_at = utcnow()
    _append_change(
        db,
        user_id,
        entity_type=entity_type,
        entity_key=entity_key,
        op=op,
        version=record.version,
        device_id=device_id,
        client_change_id=client_change_id,
        payload=payload,
    )


def conflicts(db: Session, user_id: int) -> list[SyncRecord]:
    return list(
        db.scalars(
            select(SyncRecord)
            .where(SyncRecord.user_id == user_id, SyncRecord.conflict_state == "CONFLICT")
            .order_by(SyncRecord.updated_at.desc())
        )
    )


def resolve(
    db: Session,
    user_id: int,
    entity_type: str,
    entity_key: str,
    *,
    keep: str,
    actor: str = "system",
) -> SyncRecord | None:
    """Resolve a conflict. ``keep`` is ``local`` (the pending client edit) or
    ``remote`` (the server value). Both are explicit and audited."""
    keep = (keep or "").lower()
    if keep not in ("local", "remote"):
        raise ValueError("keep must be 'local' or 'remote'")

    record = db.scalar(
        select(SyncRecord).where(
            SyncRecord.user_id == user_id,
            SyncRecord.entity_type == entity_type.upper(),
            SyncRecord.entity_key == entity_key,
        )
    )
    if record is None or record.conflict_state != "CONFLICT":
        return None

    if keep == "local":
        pending = json.loads(record.pending_payload_json or "{}")
        _apply(
            record,
            "UPSERT",
            pending,
            record.pending_device_id,
            actor,
            user_id,
            record.entity_type,
            record.entity_key,
            None,
            db,
        )
    else:
        record.conflict_state = "NONE"
        record.pending_payload_json = None
        record.pending_device_id = ""
        record.updated_at = utcnow()
    db.commit()
    audit(db, "sync_conflict_resolved", f"entity={entity_type}:{entity_key} keep={keep}",
          actor=actor, component="sync")
    return record


serialize_record = _serialize_record
