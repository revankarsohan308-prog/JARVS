"""System health, metrics, boot report, retention engine, privacy reports."""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import auth_headers


def test_health_reports_real_database_state(client):
    resp = client.get("/api/v1/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ONLINE"
    assert body["checks"]["database"]["status"] == "ONLINE"
    assert body["checks"]["ai_provider"]["available"] is True
    assert body["checks"]["ai_provider"]["mode"] == "local-rules"


def test_metrics_require_auth(client):
    assert client.get("/api/v1/system/metrics").status_code == 401


def test_metrics_return_real_values(client, student_session):
    body = client.get(
        "/api/v1/system/metrics", headers=auth_headers(student_session["token"])
    ).json()
    assert 0 <= body["cpu_percent"] <= 100
    assert body["ram_total_gb"] > 0
    assert body["disk_total_gb"] > 0
    assert isinstance(body["alerts"], list)


def test_boot_report_lists_systems(client, student_session):
    body = client.get("/api/v1/system/boot").json()
    assert body["wake_word"]  # wake word comes from config, not hard-coded
    assert body["systems"]["database"]["ok"] is True
    assert "tools" in body["systems"]


def test_retention_policy_exposes_configurable_table(client, admin_session):
    body = client.get(
        "/api/v1/privacy/retention", headers=auth_headers(admin_session["token"])
    ).json()
    policy = body["policy"]
    assert policy["conversation_history"]["retention_days"] == 90
    assert policy["audit_logs"]["retention_days"] == 365
    assert policy["long_term_memories"]["retention_days"] is None  # until deleted
    assert policy["voice_recordings"]["retention_days"] == 0  # not stored
    assert "stored_records" in body


def test_retention_update_requires_admin_and_is_audited(client, admin_session, student_session, db):
    student_h = auth_headers(student_session["token"])
    payload = {"retention_conversations_days": 30, "retention_security_events_days": 90,
               "retention_critical_incident_days": 180, "retention_audit_days": 120,
               "retention_temp_days": 3, "retention_task_history_days": 14}
    denied = client.put("/api/v1/privacy/retention", headers=student_h, json=payload)
    assert denied.status_code == 403

    ok = client.put(
        "/api/v1/privacy/retention", headers=auth_headers(admin_session["token"]),
        json=payload,
    )
    assert ok.status_code == 200
    assert ok.json()["policy"]["conversation_history"]["retention_days"] == 30

    from sqlalchemy import select

    from database.models import AuditLog

    entry = db.scalar(select(AuditLog).where(AuditLog.event == "retention_policy_changed"))
    assert entry is not None
    assert entry.actor == "user:admin"


def test_retention_purges_expired_rows_and_is_idempotent(client, admin_session, db):
    from retention import apply_retention
    from database.models import ConversationTurn, User
    from sqlalchemy import select
    from timeutil import utcnow

    user = db.scalar(select(User).where(User.username == "admin"))
    old = utcnow() - timedelta(days=400)
    db.add(ConversationTurn(user_id=user.id, role="user", content="ancient chatter"))
    db.add(ConversationTurn(user_id=user.id, role="user", content="recent chatter"))
    db.commit()
    # Backdate only the ancient row.
    rows = db.query(ConversationTurn).all()
    rows[0].created_at = old
    db.commit()

    first = apply_retention(db)
    assert first["conversation_turns"] == 1

    remaining = db.query(ConversationTurn).all()
    assert len(remaining) == 1
    assert remaining[0].content == "recent chatter"

    second = apply_retention(db)
    assert second["conversation_turns"] == 0  # idempotent


def test_retention_never_touches_open_security_events(client, admin_session, db):
    from security import monitoring
    from retention import apply_retention
    from database.models import SecurityEvent
    from timeutil import utcnow

    event = monitoring.record_bug_event(
        db, component="core", error_type="repeated_failure",
        description="open event that must survive",
    )
    # Age it far beyond every retention window.
    event.created_at = utcnow() - timedelta(days=2000)
    db.commit()

    apply_retention(db)
    still = db.get(SecurityEvent, event.id)
    assert still is not None, "OPEN security events are under hold"


def test_privacy_audit_report_answers_the_seven_questions(client, admin_session):
    body = client.get(
        "/api/v1/privacy/data", headers=auth_headers(admin_session["token"])
    ).json()
    assert "categories" in body
    assert "never_stored" in body

    from retention import privacy_audit_report
    from database.session import SessionLocal

    session = SessionLocal()
    try:
        report = privacy_audit_report(session)
    finally:
        session.close()
    for key in ("what_is_stored", "why", "retention_days", "who_can_access",
                "how_to_delete", "generated_at"):
        assert key in report


def test_conversation_deletion_is_truthful(client, student_session):
    h = auth_headers(student_session["token"])
    client.post("/api/v1/brain/chat", headers=h, json={"message": "hello"})
    resp = client.delete("/api/v1/privacy/conversations", headers=h)
    assert resp.status_code == 200
    assert resp.json()["status"] == "DELETION_COMPLETED"
    turns = client.get("/api/v1/brain/history", headers=h).json()["turns"]
    assert turns == []


def test_account_deletion_requires_password_and_confirmation(client, student_session):
    h = auth_headers(student_session["token"])
    wrong = client.post(
        "/api/v1/privacy/account-deletion", headers=h,
        json={"password": "not-the-password", "confirm": True},
    )
    assert wrong.status_code == 401

    unconfirmed = client.post(
        "/api/v1/privacy/account-deletion", headers=h,
        json={"password": "correct-horse-1", "confirm": False},
    )
    assert unconfirmed.status_code == 400

    ok = client.post(
        "/api/v1/privacy/account-deletion", headers=h,
        json={"password": "correct-horse-1", "confirm": True},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "DELETION_COMPLETED"
    assert ok.json()["sessions_revoked"] >= 1

    # The account is really gone: old token rejected, password no longer works.
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401
    login = client.post(
        "/api/v1/auth/login", json={"username": "sohan", "password": "correct-horse-1"}
    )
    assert login.status_code in (401, 403)
