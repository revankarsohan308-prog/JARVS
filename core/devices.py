"""J.A.R.V.S LINK device registry — pairing, authorization and lifecycle.

Security rules (spec: Device Security / §19 and the LINK pairing flow):
- A device token is generated exactly once at pairing time and shown to the
  owner; only its SHA-256 digest is stored (same rule as session tokens).
- A pairing code is short-lived (default 10 min) and stored as a digest, so a
  revoked/expired code can never be replayed.
- Everything is owner-scoped: authenticated REST calls must pass the device's
  owner, and queries always filter by ``owner_user_id``. One user can never
  see, revoke, or receive another user's device.
- All pairing/lifecycle events are written to the append-only audit log.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.voice.config import voice_config
from database.models import Device
from security.audit import audit
from timeutil import utcnow

# Codes avoid lookalike characters (no 0/O/1/I) so they type cleanly on the
# ESP32 OLED flow.
_PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_PAIRING_LENGTH = 6


class DeviceError(Exception):
    """Raised for honest, user-safe device failures."""


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_device_id() -> str:
    return "jarvs-link-" + secrets.token_hex(4)


def create_pairing_code(db: Session, user_id: int) -> tuple[str, int]:
    """Generate a fresh 6-char pairing code; returns (code, expires_at_epoch).

    Any previous unpaired code for this user is invalidated first.
    """
    now = utcnow()
    expiry = now + timedelta(minutes=voice_config.pairing_ttl_minutes)
    # Invalidate any earlier codes for this user that never turned into a pair.
    for dev in db.scalars(
        select(Device).where(
            Device.owner_user_id == user_id,
            Device.pairing_code_hash.is_not(None),
        )
    ):
        dev.pairing_code_hash = None
        dev.pairing_expires_at = None
    code = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(_PAIRING_LENGTH))
    device = Device(
        device_id=new_device_id(),
        owner_user_id=user_id,
        name="J.A.R.V.S LINK (pending)",
        kind="jarvs-link",
        firmware="unknown",
        # Placeholder digest only; the real device token is written at pair time.
        token_hash=_digest(secrets.token_urlsafe(32)),
        pairing_code_hash=_digest(code),
        pairing_expires_at=expiry,
    )
    db.add(device)
    db.commit()
    audit(
        db,
        "device_pairing_code_issued",
        f"user:{user_id} device:{device.device_id}",
        actor=f"user:{user_id}",
        component="devices",
    )
    return code, int(expiry.timestamp())


def pair_device(db: Session, user_id: int, code: str) -> tuple[Device, str]:
    """Exchange the owner-entered code for a one-time device token.

    Returns (device, token) — the token is shown exactly once.
    """
    code = code.strip().upper()
    now = utcnow()
    device = db.scalar(
        select(Device).where(
            Device.owner_user_id == user_id,
            Device.pairing_code_hash.is_not(None),
        )
    )
    if device is None or not _constant_time_eq(device.pairing_code_hash or "", _digest(code)):
        raise DeviceError("Pairing code is invalid.")
    if device.pairing_expires_at is None or now > device.pairing_expires_at:
        raise DeviceError("Pairing code has expired. Generate a new one.")
    token = secrets.token_urlsafe(32)
    device.token_hash = _digest(token)
    device.pairing_code_hash = None
    device.pairing_expires_at = None
    device.name = "J.A.R.V.S LINK"
    device.updated_at = utcnow()
    db.commit()
    audit(
        db,
        "device_paired",
        f"user:{user_id} device:{device.device_id}",
        actor=f"user:{user_id}",
        component="devices",
    )
    return device, token


def authenticate_device(db: Session, device_id: str, token: str) -> Device | None:
    """Validate a device token (constant-time on hashes). None = unauthorized."""
    device = db.scalar(select(Device).where(Device.device_id == device_id))
    if device is None:
        return None
    if not _constant_time_eq(device.token_hash, _digest(token)):
        return None
    return device


def list_devices(db: Session, user_id: int) -> list[Device]:
    return list(
        db.scalars(
            select(Device)
            .where(Device.owner_user_id == user_id)
            .order_by(Device.created_at)
        )
    )


def get_owned_device(db: Session, user_id: int, device_id: str) -> Device:
    device = db.scalar(
        select(Device).where(
            Device.device_id == device_id,
            Device.owner_user_id == user_id,
        )
    )
    if device is None:
        raise DeviceError("Device not found.")
    return device


def revoke_device(db: Session, user_id: int, device_id: str) -> None:
    device = get_owned_device(db, user_id, device_id)
    db.delete(device)
    db.commit()
    audit(
        db,
        "device_revoked",
        f"user:{user_id} device:{device_id}",
        actor=f"user:{user_id}",
        component="devices",
    )


def update_device_telemetry(
    db: Session,
    device_id: str,
    *,
    state: str | None = None,
    battery_pct: int | None = None,
    signal_dbm: int | None = None,
    firmware: str | None = None,
    last_seen: bool = True,
) -> Device | None:
    """Apply telemetry pushed over the wire from the device (voice server)."""
    device = db.scalar(select(Device).where(Device.device_id == device_id))
    if device is None:
        return None
    if state is not None:
        device.state = state
    if battery_pct is not None:
        device.battery_pct = max(0, min(100, battery_pct))
    if signal_dbm is not None:
        device.signal_dbm = signal_dbm
    if firmware:
        device.firmware = firmware
    if last_seen:
        device.last_seen_at = utcnow()
    device.updated_at = utcnow()
    db.commit()
    return device