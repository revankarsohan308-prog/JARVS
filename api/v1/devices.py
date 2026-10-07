"""J.A.R.V.S LINK device routes: pairing code, pair, list, revoke, telemetry.

The pairing flow mirrors the LINK onboarding: the owner asks for a code
(shown on the OLED), the firmware prints it, the owner types it here with a
WANTED device name, and the backend returns a one-time device token. That
token is what the ESP32 presents when it opens the voice WebSocket — the
token is never stored server-side, only its digest.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbDep
from core.devices import (
    DeviceError,
    create_pairing_code,
    list_devices,
    pair_device,
    revoke_device,
    update_device_telemetry,
)
from database.models import Device
from timeutil import utcnow

router = APIRouter(prefix="/devices", tags=["devices"])


def _to_public(device: Device, *, token: str | None = None) -> dict:
    """Never leak the token hash; the token is shown exactly once, at pair time."""
    return {
        "device_id": device.device_id,
        "name": device.name,
        "kind": device.kind,
        "firmware": device.firmware,
        "state": device.state,
        "battery_pct": device.battery_pct,
        "signal_dbm": device.signal_dbm,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "paired": device.pairing_code_hash is None,
        "created_at": device.created_at.isoformat(),
        "token": token,  # only present on the single pair response
    }


class PairingIn(BaseModel):
    name: str = Field(default="J.A.R.V.S LINK", min_length=1, max_length=128)


class PairIn(BaseModel):
    code: str = Field(min_length=6, max_length=6)
    name: str = Field(default="J.A.R.V.S LINK", min_length=1, max_length=128)


class TelemetryIn(BaseModel):
    state: str = Field(default="", max_length=24)
    battery_pct: int | None = Field(default=None, ge=0, le=100)
    signal_dbm: int | None = Field(default=None)
    firmware: str = Field(default="", max_length=64)


@router.get("")
def list_my_devices(user: CurrentUser, db: DbDep):
    return {"devices": [_to_public(d) for d in list_devices(db, user.id)]}


@router.post("/pairing")
def start_pairing(body: PairingIn, user: CurrentUser, db: DbDep):
    code, expires_at = create_pairing_code(db, user.id)
    return {"code": code, "expires_at": expires_at, "expires_in_seconds": expires_at - int(utcnow().timestamp())}


@router.post("/pair")
def pair(body: PairIn, user: CurrentUser, db: DbDep):
    try:
        device, token = pair_device(db, user.id, body.code)
    except DeviceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    device.name = body.name
    db.add(device)
    db.commit()
    return _to_public(device, token=token)


@router.delete("/{device_id}")
def revoke(device_id: str, user: CurrentUser, db: DbDep):
    try:
        revoke_device(db, user.id, device_id)
    except DeviceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/{device_id}/telemetry")
def telemetry(device_id: str, body: TelemetryIn, user: CurrentUser, db: DbDep):
    """The desktop/voice console can refresh live device status. Owner-scoped."""
    updated = update_device_telemetry(
        db,
        device_id,
        state=body.state or None,
        battery_pct=body.battery_pct,
        signal_dbm=body.signal_dbm,
        firmware=body.firmware or None,
    )
    if updated is None or updated.owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="Device not found.")
    return _to_public(updated)