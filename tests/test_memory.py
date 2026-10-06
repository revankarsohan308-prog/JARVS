"""Memory: storage, search, isolation between users, secret rejection."""

from __future__ import annotations

from tests.conftest import auth_headers


def test_store_search_and_recall(client, student_session):
    h = auth_headers(student_session["token"])
    resp = client.post(
        "/api/v1/memory",
        headers=h,
        json={"key": "course", "content": "BBA first year", "category": "IMPORTANT_FACTS"},
    )
    assert resp.status_code == 201

    found = client.get("/api/v1/memory/search", headers=h, params={"q": "course"})
    assert found.status_code == 200
    results = found.json()["results"]
    assert any(m["key"] == "course" for m in results)


def test_secret_content_rejected_and_audited(client, student_session, db):
    h = auth_headers(student_session["token"])
    resp = client.post(
        "/api/v1/memory",
        headers=h,
        json={"key": "wifi", "content": "password hunter2secret"},
    )
    assert resp.status_code == 422
    assert "password" in resp.json()["detail"].lower()

    from sqlalchemy import select

    from database.models import AuditLog, Memory

    assert db.scalar(select(Memory)) is None
    entry = db.scalar(select(AuditLog).where(AuditLog.event == "memory_rejected_sensitive"))
    assert entry is not None


def test_users_cannot_see_each_others_memories(client, admin_session, student_session):
    admin_h = auth_headers(admin_session["token"])
    student_h = auth_headers(student_session["token"])

    client.post(
        "/api/v1/memory", headers=admin_h,
        json={"key": "admin_secret", "content": "college budget review"},
    )
    client.post(
        "/api/v1/memory", headers=student_h,
        json={"key": "student_note", "content": "assignment due friday"},
    )

    student_list = client.get("/api/v1/memory", headers=student_h).json()["memories"]
    keys = [m["key"] for m in student_list]
    assert "student_note" in keys
    assert "admin_secret" not in keys

    # Searching another user's term must not leak it.
    found = client.get(
        "/api/v1/memory/search", headers=student_h, params={"q": "admin_secret"}
    ).json()["results"]
    assert all(m["key"] != "admin_secret" for m in found)


def test_delete_memory_removes_it_for_real(client, student_session):
    h = auth_headers(student_session["token"])
    created = client.post(
        "/api/v1/memory", headers=h, json={"key": "temp", "content": "delete me"}
    ).json()["memory"]
    resp = client.delete(f"/api/v1/memory/{created['id']}", headers=h)
    assert resp.status_code == 200
    assert resp.json()["detail"] == "Deletion completed."

    remaining = [m["key"] for m in client.get("/api/v1/memory", headers=h).json()["memories"]]
    assert "temp" not in remaining
    # A second delete is honestly reported as missing, not as success.
    again = client.delete(f"/api/v1/memory/{created['id']}", headers=h)
    assert again.status_code == 404


def test_cannot_delete_another_users_memory(client, admin_session, student_session):
    admin_h = auth_headers(admin_session["token"])
    student_h = auth_headers(student_session["token"])
    created = client.post(
        "/api/v1/memory", headers=admin_h,
        json={"key": "mine", "content": "admin only"},
    ).json()["memory"]
    resp = client.delete(f"/api/v1/memory/{created['id']}", headers=student_h)
    assert resp.status_code == 404  # not found in *your* scope = no leak

    remaining = [m["key"] for m in client.get("/api/v1/memory", headers=admin_h).json()["memories"]]
    assert "mine" in remaining


def test_memory_disabled_by_user_refuses_writes(client, student_session):
    h = auth_headers(student_session["token"])
    toggle = client.put(
        "/api/v1/privacy/long-term-memory", headers=h, json={"enabled": False}
    )
    assert toggle.status_code == 200

    resp = client.post(
        "/api/v1/memory", headers=h, json={"key": "k", "content": "v"}
    )
    assert resp.status_code == 422
    assert "turned off" in resp.json()["detail"]

    # Re-enable and the same write succeeds — the refusal was real, not cosmetic.
    client.put("/api/v1/privacy/long-term-memory", headers=h, json={"enabled": True})
    ok = client.post("/api/v1/memory", headers=h, json={"key": "k", "content": "v"})
    assert ok.status_code == 201


def test_update_overwrite_keeps_single_row(client, student_session):
    h = auth_headers(student_session["token"])
    client.post("/api/v1/memory", headers=h, json={"key": "mood", "content": "tired"})
    client.post("/api/v1/memory", headers=h, json={"key": "mood", "content": "better now"})
    items = [m for m in client.get("/api/v1/memory", headers=h).json()["memories"] if m["key"] == "mood"]
    assert len(items) == 1
    assert items[0]["content"] == "better now"


def test_export_contains_no_secrets(client, student_session, db):
    h = auth_headers(student_session["token"])
    export = client.get("/api/v1/privacy/export", headers=h)
    assert export.status_code == 200
    body = export.json()
    assert body["exported_for"] == "sohan"
    from sqlalchemy import select

    from database.models import User

    user = db.scalar(select(User).where(User.username == "sohan"))
    assert user.password_hash not in str(body)
    assert "excluded" in body
