"""Module 5 tests: versioned sync, conflict detection, honesty under retries.

The contract: concurrent edits are never silently overwritten. Sensitive
entities always require an explicit resolution; offline retries are idempotent.
"""

from __future__ import annotations

from sqlalchemy import select

from core.sync import service as sync
from database.models import SyncRecord, User
from tests.conftest import auth_headers


def _user(db, username: str) -> User:
    return db.scalar(select(User).where(User.username == username))


def _push(db, user_id, changes, *, device="dev-1", on_conflict="reject"):
    return sync.push(db, user_id, device, changes, on_conflict=on_conflict)


def test_push_creates_version_and_change_log(db, student_session):
    user = _user(db, "sohan")
    results = _push(db, user.id, [
        {"entity_type": "TASK", "entity_key": "t1", "base_version": 0, "payload": {"title": "buy"}}
    ])
    assert results[0]["status"] == "APPLIED"
    assert results[0]["version"] == 1
    assert sync.current_cursor(db, user.id) == 1

    pulled = sync.pull(db, user.id, since=0)
    assert len(pulled["changes"]) == 1
    assert pulled["changes"][0]["entity_key"] == "t1"
    assert pulled["cursor"] == 1


def test_pull_is_cursor_based(db, student_session):
    user = _user(db, "sohan")
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "a", "base_version": 0, "payload": {}}])
    first_cursor = sync.current_cursor(db, user.id)
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "b", "base_version": 0, "payload": {}}])

    later = sync.pull(db, user.id, since=first_cursor)
    keys = [c["entity_key"] for c in later["changes"]]
    assert keys == ["b"]


def test_version_mismatch_is_a_conflict_not_an_overwrite(client, db, student_session):
    user = _user(db, "sohan")
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "t1", "base_version": 0, "payload": {"v": 1}}])
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "t1", "base_version": 1, "payload": {"v": 2}}],
          device="dev-2")

    # A stale third edit must not clobber dev-2's version.
    results = _push(db, user.id, [
        {"entity_type": "TASK", "entity_key": "t1", "base_version": 1, "payload": {"v": 999}}
    ], device="dev-3")
    assert results[0]["status"] == "CONFLICT"
    assert results[0]["current_version"] == 2

    record = db.scalar(
        select(SyncRecord).where(SyncRecord.entity_key == "t1", SyncRecord.user_id == user.id)
    )
    assert record.version == 2
    assert record.conflict_state == "CONFLICT"

    h = auth_headers(student_session["token"])
    conflicts = client.get("/api/v1/sync/conflicts", headers=h).json()["conflicts"]
    assert conflicts and conflicts[0]["pending_payload"] == {"v": 999}


def test_conflict_resolution_keep_local_and_remote(client, db, student_session):
    user = _user(db, "sohan")
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "t1", "base_version": 0, "payload": {"v": 1}}])
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "t1", "base_version": 1, "payload": {"v": 2}}])
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "t1", "base_version": 1, "payload": {"v": 999}}])

    h = auth_headers(student_session["token"])
    resolved = client.post(
        "/api/v1/sync/conflicts/resolve",
        headers=h,
        json={"entity_type": "TASK", "entity_key": "t1", "keep": "local"},
    ).json()
    assert resolved["record"]["payload"] == {"v": 999}
    assert resolved["record"]["version"] == 3
    assert resolved["record"]["conflict_state"] == "NONE"

    # A second conflict resolved by keeping the server value.
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "t1", "base_version": 3, "payload": {"v": 4}}])
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "t1", "base_version": 3, "payload": {"v": 5}}])
    remote = client.post(
        "/api/v1/sync/conflicts/resolve",
        headers=h,
        json={"entity_type": "TASK", "entity_key": "t1", "keep": "remote"},
    ).json()
    assert remote["record"]["payload"] == {"v": 4}
    assert remote["record"]["conflict_state"] == "NONE"


def test_sensitive_entities_never_allow_lww(db, student_session):
    user = _user(db, "sohan")
    _push(db, user.id, [{"entity_type": "MEMORY", "entity_key": "note", "base_version": 0, "payload": {"v": 1}}])
    # Even explicit last-write-wins is refused for a sensitive entity.
    results = _push(
        db, user.id,
        [{"entity_type": "MEMORY", "entity_key": "note", "base_version": 0, "payload": {"v": 2}}],
        on_conflict="lww",
    )
    assert results[0]["status"] == "CONFLICT"


def test_non_sensitive_lww_is_explicit_and_opt_in(db, student_session):
    user = _user(db, "sohan")
    _push(db, user.id, [{"entity_type": "TASK", "entity_key": "t1", "base_version": 0, "payload": {"v": 1}}])
    results = _push(
        db, user.id,
        [{"entity_type": "TASK", "entity_key": "t1", "base_version": 0, "payload": {"v": 2}}],
        on_conflict="lww",
    )
    assert results[0]["status"] == "APPLIED_LWW"
    record = db.scalar(select(SyncRecord).where(SyncRecord.entity_key == "t1"))
    assert record.payload_json == '{"v": 2}'


def test_offline_retry_is_idempotent(db, student_session):
    user = _user(db, "sohan")
    change = {
        "entity_type": "TASK", "entity_key": "t1", "base_version": 0,
        "payload": {"v": 1}, "client_change_id": "cc-1",
    }
    first = _push(db, user.id, [change])
    assert first[0]["status"] == "APPLIED"
    cursor_after_first = sync.current_cursor(db, user.id)

    replay = _push(db, user.id, [change])  # device retries the queued change
    assert replay[0]["status"] == "DUPLICATE"
    assert sync.current_cursor(db, user.id) == cursor_after_first  # no double apply


def test_syncing_secrets_is_refused(db, student_session):
    user = _user(db, "sohan")
    results = _push(db, user.id, [
        {"entity_type": "TASK", "entity_key": "t1", "base_version": 0,
         "payload": {"password": "hunter2secret"}}
    ])
    assert results[0]["status"] == "REJECTED_SECRET"
    assert db.scalar(select(SyncRecord).where(SyncRecord.user_id == user.id)) is None


def test_sync_is_user_isolated(client, db, admin_session, student_session):
    sohan = _user(db, "sohan")
    admin = _user(db, "admin")
    _push(db, sohan.id, [{"entity_type": "TASK", "entity_key": "private", "base_version": 0, "payload": {"x": 1}}])

    assert sync.state(db, admin.id)["records"] == 0
    admin_h = auth_headers(admin_session["token"])
    assert client.get("/api/v1/sync/pull", headers=admin_h, params={"since": 0}).json()[
        "changes"
    ] == []


def test_sync_state_and_push_api(client, student_session):
    h = auth_headers(student_session["token"])
    before = client.get("/api/v1/sync/state", headers=h).json()
    assert before["cursor"] == 0

    resp = client.post(
        "/api/v1/sync/push",
        headers=h,
        json={
            "device_id": "phone",
            "changes": [
                {"entity_type": "SETTING", "entity_key": "theme", "base_version": 0,
                 "payload": {"value": "dark"}}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "APPLIED"

    after = client.get("/api/v1/sync/state", headers=h).json()
    assert after["records"] == 1
    assert after["cursor"] == 1

    pulled = client.get("/api/v1/sync/pull", headers=h, params={"since": 0}).json()
    assert pulled["changes"][0]["payload"] == {"value": "dark"}
