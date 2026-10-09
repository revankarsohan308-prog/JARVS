"""Module 8 tests: notification centre — dedup, severity, lifecycle, honesty.

The in-app channel is the only one that actually delivers; every external
channel must report NOT_CONFIGURED rather than pretending to send.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from core.notifications import service as notifications
from database.models import Task, User
from tests.conftest import auth_headers
from timeutil import utcnow


def _user(db, username: str) -> User:
    return db.scalar(select(User).where(User.username == username))


def test_notify_creates_and_counts_unread(client, db, student_session):
    user = _user(db, "sohan")
    row, reason = notifications.notify(
        db, user.id, category="MISSION", severity="MEDIUM", title="Mission failed", body="x"
    )
    assert reason == "created"
    assert row.status == "UNREAD"
    assert row.channel == "IN_APP"
    assert row.delivered_at is not None
    assert notifications.unread_count(db, user.id) == 1

    h = auth_headers(student_session["token"])
    listed = client.get("/api/v1/notifications", headers=h).json()
    assert listed["unread"] == 1
    assert listed["notifications"][0]["title"] == "Mission failed"


def test_duplicates_within_cooldown_are_merged_not_flooded(db, student_session):
    user = _user(db, "sohan")
    first, _ = notifications.notify(
        db, user.id, category="HEALTH", severity="HIGH", title="db down",
        dedup_key="health:db:HIGH",
    )
    second, reason = notifications.notify(
        db, user.id, category="HEALTH", severity="HIGH", title="db down",
        dedup_key="health:db:HIGH",
    )
    assert reason == "deduped"
    assert second.id == first.id
    assert second.occurrences == 2
    assert len(list(notifications.list_for_user(db, user.id))) == 1


def test_duplicate_after_cooldown_creates_new_notification(db, student_session):
    user = _user(db, "sohan")
    first, _ = notifications.notify(
        db, user.id, category="HEALTH", severity="HIGH", title="db down",
        dedup_key="health:db:HIGH",
    )
    first.updated_at = utcnow() - timedelta(hours=2)
    db.commit()
    _, reason = notifications.notify(
        db, user.id, category="HEALTH", severity="HIGH", title="db down",
        dedup_key="health:db:HIGH",
    )
    assert reason == "created"
    assert len(list(notifications.list_for_user(db, user.id))) == 2


def test_preferences_suppress_low_severity_and_muted_categories(db, student_session):
    user = _user(db, "sohan")
    notifications.set_preferences(db, user.id, min_severity="HIGH", muted_categories=["MISSION"])

    _, reason = notifications.notify(
        db, user.id, category="HEALTH", severity="LOW", title="noise"
    )
    assert reason == "suppressed_preference"
    _, reason = notifications.notify(
        db, user.id, category="MISSION", severity="CRITICAL", title="muted mission"
    )
    assert reason == "suppressed_preference"
    row, reason = notifications.notify(
        db, user.id, category="HEALTH", severity="HIGH", title="important"
    )
    assert reason == "created"
    assert row is not None


def test_read_acknowledge_and_escalate_lifecycle(client, db, student_session):
    user = _user(db, "sohan")
    row, _ = notifications.notify(
        db, user.id, category="SECURITY", severity="MEDIUM", title="suspicious login"
    )
    h = auth_headers(student_session["token"])

    read = client.post(f"/api/v1/notifications/{row.id}/read", headers=h).json()
    assert read["notification"]["status"] == "READ"
    assert read["notification"]["read_at"] is not None

    acked = client.post(f"/api/v1/notifications/{row.id}/ack", headers=h).json()
    assert acked["notification"]["status"] == "ACKNOWLEDGED"
    assert acked["notification"]["acknowledged_at"] is not None

    esc = client.post(f"/api/v1/notifications/{row.id}/escalate", headers=h).json()
    assert esc["notification"]["status"] == "ESCALATED"
    assert esc["notification"]["severity"] == "HIGH"
    assert esc["notification"]["escalated_at"] is not None


def test_notifications_are_owner_scoped(client, db, admin_session, student_session):
    sohan = _user(db, "sohan")
    row, _ = notifications.notify(
        db, sohan.id, category="SYSTEM", severity="LOW", title="for sohan"
    )
    admin_h = auth_headers(admin_session["token"])
    assert client.post(f"/api/v1/notifications/{row.id}/read", headers=admin_h).status_code == 404

    # The admin's own list never shows the student's notification.
    admin_list = client.get("/api/v1/notifications", headers=admin_h).json()["notifications"]
    assert all(n["id"] != row.id for n in admin_list)


def test_external_channels_are_reported_not_configured(client, student_session):
    h = auth_headers(student_session["token"])
    channels = client.get("/api/v1/notifications/channels", headers=h).json()["channels"]
    assert channels["IN_APP"]["status"] == "CONFIGURED"
    for external in ("EMAIL", "SMS", "WEBHOOK", "PUSH"):
        assert channels[external]["status"] == "NOT_CONFIGURED"


def test_escalation_sweep_is_bounded(db, student_session):
    user = _user(db, "sohan")
    row, _ = notifications.notify(
        db, user.id, category="HEALTH", severity="HIGH", title="still down"
    )
    # Age it past the escalation window.
    row.updated_at = utcnow() - timedelta(hours=3)
    db.commit()

    moved = notifications.escalate_stale(db)
    assert moved == 1
    assert notifications.get_owned(db, user.id, row.id).status == "ESCALATED"


def test_health_transition_notifies_admins(db, admin_session):
    from core.health.checks import CheckResult
    from core.health.service import record_result

    admin = _user(db, "admin")
    record_result(db, "unit_test_component", CheckResult(status="OFFLINE", detail="probe failed"))
    rows = [n for n in notifications.list_for_user(db, admin.id) if n.category == "HEALTH"]
    assert rows, "an unhealthy transition must notify administrators"
    assert rows[0].severity in ("MEDIUM", "HIGH")


def test_mission_awaiting_approval_notifies_owner(client, db, student_session):
    user = _user(db, "sohan")
    task = Task(user_id=user.id, title="delete me")
    db.add(task)
    db.commit()

    from core.missions import service as missions

    mission = missions.create_mission(db, user.id, f"delete task {task.id}", actor="test")
    assert mission.state == "WAITING_FOR_APPROVAL"
    rows = [n for n in notifications.list_for_user(db, user.id) if n.category == "MISSION"]
    assert any("approval" in n.title.lower() for n in rows)
