"""J.A.R.V.S LINK device lifecycle — pairing, auth, ownership isolation.

The critical rule (spec, User A/B isolation): one user can never see or
revoke another user's device, and a revoked device can never connect again.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from core.devices import (
    DeviceError,
    authenticate_device,
    list_devices,
    pair_device,
)
from database.models import Device
from tests.conftest import auth_headers, login, register

_PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


@pytest.fixture()
def owner(client, db):
    register(client, "owner")
    return login(client, "owner")


def _pair_through_api(client, token, code: str | None = None) -> dict:
    code = code or _fresh_code(client, token)
    resp = client.post(
        "/api/v1/devices/pair",
        json={"code": code, "name": "WatchKitchen LINK"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _fresh_code(client, token: str) -> str:
    resp = client.post("/api/v1/devices/pairing", json={"name": "WatchKitchen LINK"}, headers=auth_headers(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["code"]) == 6
    assert all(c in _PAIRING_ALPHABET for c in body["code"])
    return body["code"]


def test_pairing_code_and_token_are_issued_once(client, db, owner):
    token = owner["token"]
    code = _fresh_code(client, token)
    paired = _pair_through_api(client, token, code)
    assert paired["device_id"].startswith("jarvs-link-")
    assert paired["token"]  # shown exactly this once
    assert paired["paired"] is True
    # Re-using the same code must fail — it was consumed.
    resp = client.post("/api/v1/devices/pair", json={"code": code}, headers=auth_headers(token))
    assert resp.status_code == 400
    assert "invalid" in resp.json()["detail"].lower()


def test_authenticate_accepts_only_the_real_token(client, db, owner):
    token = owner["token"]
    code = _fresh_code(client, token)
    resp = client.post("/api/v1/devices/pair", json={"code": code}, headers=auth_headers(token))
    pair_body = resp.json()
    device_id, device_token = pair_body["device_id"], pair_body["token"]

    from database.session import SessionLocal

    with SessionLocal() as session:
        device = authenticate_device(session, device_id, device_token)
        assert device is not None
        assert authenticate_device(session, device_id, "wrong-token") is None
        assert authenticate_device(session, "jarvs-link-nope", device_token) is None

    # The token is never stored — only its digest.
    row = db.scalar(select(Device).where(Device.device_id == device_id))
    assert row.token_hash != device_token
    assert len(row.token_hash) == 64  # sha256 hex


def test_expired_pairing_code_is_rejected(client, db, owner):
    token = owner["token"]
    code = _fresh_code(client, token)
    device = db.scalar(select(Device).where(Device.pairing_code_hash.is_not(None)))
    from timeutil import utcnow
    from datetime import timedelta

    device.pairing_expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    with pytest.raises(DeviceError, match="expired"):
        pair_device(db, db.scalar(select(Device)).owner_user_id, code)


def test_devices_are_owner_isolated(client, db, owner):
    # owner pairs a device...
    token = owner["token"]
    paired = _pair_through_api(client, token)

    # ...a second user must not see it, and revoking it must fail.
    resp = client.post("/api/v1/auth/register", json={"username": "other", "password": "correct-horse-1"})
    assert resp.status_code == 201
    other = login(client, "other")

    resp = client.get("/api/v1/devices", headers=auth_headers(other["token"]))
    assert resp.json()["devices"] == []

    resp = client.delete(f"/api/v1/devices/{paired['device_id']}", headers=auth_headers(other["token"]))
    assert resp.status_code == 404

    # ...and the device still connects for its real owner.
    from database.session import SessionLocal

    with SessionLocal() as session:
        assert authenticate_device(session, paired["device_id"], paired["token"]) is not None


def test_revoke_blocks_future_connections(client, db, owner):
    from database.session import SessionLocal

    token = owner["token"]
    paired = _pair_through_api(client, token)

    with SessionLocal() as session:
        assert authenticate_device(session, paired["device_id"], paired["token"]) is not None

    resp = client.delete(f"/api/v1/devices/{paired['device_id']}", headers=auth_headers(token))
    assert resp.status_code == 200

    with SessionLocal() as session:
        assert authenticate_device(session, paired["device_id"], paired["token"]) is None
    resp = client.get("/api/v1/devices", headers=auth_headers(token))
    assert resp.json()["devices"] == []


def test_telemetry_is_owner_scoped_and_visible(client, db, owner):
    token = owner["token"]
    paired = _pair_through_api(client, token)
    resp = client.post(
        f"/api/v1/devices/{paired['device_id']}/telemetry",
        json={"state": "listening", "battery_pct": 87, "signal_dbm": -51, "firmware": "0.1.0"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "listening"
    assert body["battery_pct"] == 87
    assert body["signal_dbm"] == -51

    rows = list_devices(db, db.scalar(select(Device)).owner_user_id)
    assert rows[0].last_seen_at is not None