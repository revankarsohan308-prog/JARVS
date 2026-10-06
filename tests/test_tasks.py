"""Tasks: CRUD, isolation, recurrence, chat-driven creation, confirmation."""

from __future__ import annotations

from tests.conftest import auth_headers, local_date


def test_create_list_complete_delete_via_api(client, admin_session):
    h = auth_headers(admin_session["token"])

    created = client.post(
        "/api/v1/tasks", headers=h,
        json={"title": "Submit BBA assignment", "priority": "high", "due_at": "2026-10-14"},
    )
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["priority"] == "high"
    assert local_date(task["due_at"]) == "2026-10-14"

    listed = client.get("/api/v1/tasks", headers=h).json()["tasks"]
    assert len(listed) == 1

    done = client.post(f"/api/v1/tasks/{task['id']}/complete", headers=h)
    assert done.json()["task"]["status"] == "done"

    # Completed tasks disappear from the pending default view.
    assert client.get("/api/v1/tasks", headers=h).json()["tasks"] == []
    all_tasks = client.get(
        "/api/v1/tasks", headers=h, params={"include_completed": True}
    ).json()["tasks"]
    assert len(all_tasks) == 1

    deleted = client.delete(f"/api/v1/tasks/{task['id']}", headers=h)
    assert deleted.status_code == 200
    assert client.delete(f"/api/v1/tasks/{task['id']}", headers=h).status_code == 404


def test_student_cannot_delete_tasks(client, student_session):
    h = auth_headers(student_session["token"])
    created = client.post("/api/v1/tasks", headers=h, json={"title": "not deletable"}).json()
    resp = client.delete(f"/api/v1/tasks/{created['task']['id']}", headers=h)
    assert resp.status_code == 403
    assert "task:delete" in resp.json()["detail"]


def test_tasks_are_isolated_between_users(client, admin_session, student_session):
    admin_h = auth_headers(admin_session["token"])
    student_h = auth_headers(student_session["token"])
    client.post("/api/v1/tasks", headers=admin_h, json={"title": "admin task"})
    client.post("/api/v1/tasks", headers=student_h, json={"title": "student task"})

    student_titles = [t["title"] for t in client.get("/api/v1/tasks", headers=student_h).json()["tasks"]]
    assert student_titles == ["student task"]

    # Accessing the other user's task id must 404, not 403 (no existence leak).
    admin_task = client.get("/api/v1/tasks", headers=admin_h).json()["tasks"][0]
    assert client.get(f"/api/v1/tasks/{admin_task['id']}", headers=student_h).status_code == 404
    assert client.post(
        f"/api/v1/tasks/{admin_task['id']}/complete", headers=student_h
    ).status_code == 404


def test_bad_due_date_is_rejected_not_silently_dropped(client, student_session):
    h = auth_headers(student_session["token"])
    resp = client.post(
        "/api/v1/tasks", headers=h, json={"title": "x", "due_at": "next tuesday-ish"}
    )
    assert resp.status_code == 422
    assert "couldn't read" in resp.json()["detail"]


def test_recurrence_spawns_next_occurrence(client, student_session):
    h = auth_headers(student_session["token"])
    created = client.post(
        "/api/v1/tasks", headers=h,
        json={"title": "daily revision", "recurrence": "weekly", "due_at": "2026-10-10"},
    ).json()["task"]
    client.post(f"/api/v1/tasks/{created['id']}/complete", headers=h)

    pending = client.get("/api/v1/tasks", headers=h).json()["tasks"]
    assert len(pending) == 1
    assert local_date(pending[0]["due_at"]) == "2026-10-17"
    assert pending[0]["recurrence"] == "weekly"


def test_chat_creates_and_lists_tasks(client, student_session):
    h = auth_headers(student_session["token"])
    resp = client.post(
        "/api/v1/brain/chat",
        headers=h,
        json={"message": "remind me to submit library form on 2026-10-20"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "Created task #1" in body["text"]
    assert body["tools"][0]["verified"] is True

    listing = client.post("/api/v1/brain/chat", headers=h, json={"message": "my tasks"})
    assert "submit library form" in listing.json()["text"]


def test_chat_delete_requires_human_confirmation(client, admin_session):
    """Even a SUPER_ADMIN must confirm a destructive tool (spec §15.1)."""
    h = auth_headers(admin_session["token"])
    client.post("/api/v1/brain/chat", headers=h, json={"message": "remind me to throw out old notes"})

    asked = client.post("/api/v1/brain/chat", headers=h, json={"message": "delete task 1"})
    body = asked.json()
    assert body["needs_confirmation"] is True
    assert body["confirmation_id"]

    # The task still exists — nothing ran without the human.
    tasks = client.get("/api/v1/tasks", headers=h).json()["tasks"]
    assert len(tasks) == 1

    confirmed = client.post(
        f"/api/v1/brain/confirmations/{body['confirmation_id']}",
        headers=h,
        json={"confirmation_id": body["confirmation_id"], "approve": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["ok"] is True
    assert confirmed.json()["verified"] is True
    assert client.get("/api/v1/tasks", headers=h).json()["tasks"] == []


def test_denied_confirmation_runs_nothing(client, admin_session):
    h = auth_headers(admin_session["token"])
    client.post("/api/v1/brain/chat", headers=h, json={"message": "remind me to keep this"})
    asked = client.post("/api/v1/brain/chat", headers=h, json={"message": "delete task 1"})
    cid = asked.json()["confirmation_id"]

    denied = client.post(
        f"/api/v1/brain/confirmations/{cid}",
        headers=h,
        json={"confirmation_id": cid, "approve": False},
    )
    assert denied.json()["resolution"] == "denied"
    assert len(client.get("/api/v1/tasks", headers=h).json()["tasks"]) == 1


def test_confirmation_is_owner_scoped(client, admin_session, student_session):
    admin_h = auth_headers(admin_session["token"])
    student_h = auth_headers(student_session["token"])
    client.post("/api/v1/brain/chat", headers=admin_h, json={"message": "remind me to private thing"})
    asked = client.post("/api/v1/brain/chat", headers=admin_h, json={"message": "delete task 1"})
    cid = asked.json()["confirmation_id"]

    steal = client.post(
        f"/api/v1/brain/confirmations/{cid}",
        headers=student_h,
        json={"confirmation_id": cid, "approve": True},
    )
    assert steal.status_code == 404
    assert len(client.get("/api/v1/tasks", headers=admin_h).json()["tasks"]) == 1
