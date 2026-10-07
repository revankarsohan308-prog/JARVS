"""J.A.R.V.S LINK device routes: pairing code, pair, list, revoke, telemetry.

The pairing flow mirrors the LINK onboarding: the owner asks for a code
(shown on the OLED), the firmware prints it, the owner types it here with a
WANTED device name, and the backend returns a one-time device token. That
token is what the ESP32 presents when it opens the voice WebSocket — the
token is never stored server-side, only its digest.
"""

from __future__ import annotations

import json
import os
import urllib.request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbDep
from backend.voice.config import voice_config
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


def fetch_voice_status() -> dict:
    """Live LINK-plane status proxied from the voice server.

    Honest by construction: OFFLINE when the voice server is unreachable, never
    a simulated state. Only device IDs and STT/TTS capability labels cross this
    boundary — no tokens, no audio.
    """
    url = os.environ.get("ALYO_VOICE_SERVER_URL") or f"http://127.0.0.1:{voice_config.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {
            "voice_server": "ONLINE" if payload.get("status") == "ONLINE" else "UNKNOWN",
            "stt": payload.get("stt"),
            "tts": payload.get("tts"),
            "connected_devices": sorted(payload.get("connected_devices") or []),
        }
    except Exception:
        return {"voice_server": "OFFLINE", "stt": None, "tts": None, "connected_devices": []}


@router.get("/voice-status")
def voice_status(user: CurrentUser, db: DbDep):
    """Live voice-plane view, filtered to the caller's own devices (isolation)."""
    status = fetch_voice_status()
    mine = {d.device_id for d in list_devices(db, user.id)}
    status["connected_devices"] = [did for did in status["connected_devices"] if did in mine]
    return status


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