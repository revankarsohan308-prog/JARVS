"""Security layer: events, scoring, incidents, audit, escalation, emergency."""

from __future__ import annotations

import json

from security.monitoring import risk_band
from tests.conftest import auth_headers


def test_risk_bands_match_spec():
    assert risk_band(0) == "LOW"
    assert risk_band(29) == "LOW"
    assert risk_band(30) == "MEDIUM"
    assert risk_band(59) == "MEDIUM"
    assert risk_band(60) == "HIGH"
    assert risk_band(79) == "HIGH"
    assert risk_band(80) == "CRITICAL"
    assert risk_band(100) == "CRITICAL"


def test_security_status_available_to_all_authenticated_users(client, student_session):
    resp = client.get(
        "/api/v1/security/status", headers=auth_headers(student_session["token"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["threat_level"] in ("NORMAL", "GUARDED", "ELEVATED")
    assert "active_incidents" in body
    assert "failed_logins" in body


def test_bug_detector_records_event_fields(client, admin_session, db):
    from security import monitoring

    event = monitoring.record_bug_event(
        db,
        component="database",
        error_type="database_error",
        description="Test: connection dropped during write.",
        evidence=["operation=insert", "table=users"],
    )
    assert event.id
    assert event.severity == "HIGH"
    assert event.possible_cause
    assert event.recommended_action
    assert event.status == "OPEN"
    assert json.loads(event.evidence) == ["operation=insert", "table=users"]

    listed = client.get(
        "/api/v1/security/events", headers=auth_headers(admin_session["token"])
    ).json()["events"]
    assert any(e["id"] == event.id for e in listed)


def test_event_exposes_transparency_shape(client, admin_session, db):
    from security import monitoring

    event = monitoring.record_bug_event(
        db, component="auth", error_type="auth_failure_burst",
        description="5 failed logins for admin.",
    )
    detail = client.get(
        f"/api/v1/security/events/{event.id}",
        headers=auth_headers(admin_session["token"]),
    ).json()["event"]
    for field in (
        "what_happened", "why_flagged", "seriousness",
        "what_alyo_did", "what_you_can_do",
    ):
        assert detail[field], f"missing transparency field: {field}"
    # Never a panic claim.
    combined = " ".join(str(detail[f]) for f in ("what_happened", "why_flagged")).lower()
    assert "you are being hacked" not in combined


def test_high_risk_event_opens_incident_and_revokes_sessions(client, admin_session, student_session, db):
    from security import monitoring

    # student_session registered + logged in -> active session exists (user id 2)
    monitoring.record_threat_event(
        db,
        "brute_force_login",
        component="auth",
        risk_score=85,
        evidence=["12 failed logins in 60s", "source 127.0.0.1"],
        user_id=2,
    )
    token = student_session["token"]
    # Automatic containment: the affected user's sessions are revoked.
    assert client.get(
        "/api/v1/auth/me", headers=auth_headers(token)
    ).status_code == 401

    # The incident exists and carries its evidence, visible to an administrator.
    incidents = client.get(
        "/api/v1/security/incidents", headers=auth_headers(admin_session["token"])
    ).json()["incidents"]
    assert len(incidents) == 1
    assert incidents[0]["state"] == "CONTAINED"
    assert incidents[0]["evidence_refs"]


def test_incident_lifecycle(client, admin_session, db):
    from security import monitoring

    incident = monitoring.open_incident(
        db,
        title="Potential security incident: config change detected",
        severity="HIGH",
        risk_score=70,
        component="config",
        evidence=["checksum mismatch: .env"],
        recommended_action="Restore .env from a trusted copy.",
    )
    h = auth_headers(admin_session["token"])

    moved = client.post(
        f"/api/v1/security/incidents/{incident.id}/state",
        headers=h, json={"state": "INVESTIGATING", "note": "checking"},
    )
    assert moved.status_code == 200
    assert moved.json()["incident"]["state"] == "INVESTIGATING"
    assert len(moved.json()["incident"]["timeline"]) >= 2

    bad = client.post(
        f"/api/v1/security/incidents/{incident.id}/state",
        headers=h, json={"state": "MAYBE", "note": ""},
    )
    assert bad.status_code == 400

    resolved = client.post(
        f"/api/v1/security/incidents/{incident.id}/state",
        headers=h, json={"state": "RESOLVED", "note": "restored"},
    )
    assert resolved.json()["incident"]["resolved_at"]

    # Resolved incidents leave the active list but remain in history.
    active = client.get("/api/v1/security/incidents", headers=h).json()["incidents"]
    assert all(i["id"] != incident.id for i in active)
    history = client.get(
        "/api/v1/security/incidents", headers=h, params={"include_resolved": True}
    ).json()["incidents"]
    assert any(i["id"] == incident.id for i in history)


def test_audit_is_append_only(client, admin_session, db):
    from sqlalchemy import select

    from database.models import AuditLog
    from security.audit import audit

    entry = audit(db, "test_event", "before tamper", component="tests")
    entry.detail = "tampered"
    try:
        db.commit()
        raised = False
    except Exception:
        raised = True
        db.rollback()
    assert raised, "audit log update must be rejected"
    db.expire_all()
    fresh = db.scalar(select(AuditLog).where(AuditLog.id == entry.id))
    assert fresh.detail == "before tamper"


def test_audit_redacts_secrets(client, admin_session, db):
    from sqlalchemy import select

    from database.models import AuditLog
    from security.audit import audit

    audit(db, "test_secret", "password=hunter2 api_key=sk-abcdefghijklmnop", component="tests")
    row = db.scalar(select(AuditLog).where(AuditLog.event == "test_secret"))
    assert "hunter2" not in row.detail
    assert "sk-abcdefghijklmnop" not in row.detail
    assert "[REDACTED]" in row.detail


def test_audit_requires_permission(client, admin_session, student_session):
    resp = client.get(
        "/api/v1/security/audit", headers=auth_headers(student_session["token"])
    )
    assert resp.status_code == 403


def test_escalation_contacts_never_auto_send(client, student_session):
    body = client.get(
        "/api/v1/security/escalation", headers=auth_headers(student_session["token"])
    ).json()
    assert "automatically" in body["policy"]
    contacts = {c["label"]: c for c in body["contacts"]}
    # Unconfigured internal contacts are honestly marked.
    assert contacts["College cybersecurity contact"]["configured"] is False
    # Official public channels exist as reference data.
    assert contacts["National cybercrime reporting (India)"]["configured"] is True
    # No guessed "nearest" anything.
    assert not any("nearest" in c["label"].lower() for c in body["contacts"])


def test_emergency_is_separate_from_cyber_and_honest(client, student_session):
    body = client.get(
        "/api/v1/security/emergency", headers=auth_headers(student_session["token"])
    ).json()
    assert body["kind"] == "PHYSICAL_EMERGENCY"
    assert body["emergency_number"] == "112"
    assert body["auto_contact_enabled"] is False
    assert "does not place emergency calls" in body["note"]
    assert "does not by itself warrant contacting police" in body["cyber_vs_physical"]


def test_containment_never_deletes_evidence(client, admin_session, db):
    from security import monitoring

    monitoring.record_threat_event(
        db, "config_tamper", component="config", risk_score=90,
        evidence=["sha256 mismatch .env"],
    )
    h = auth_headers(admin_session["token"])
    events = client.get("/api/v1/security/events", headers=h).json()["events"]
    assert events, "evidence must survive containment"
    incidents = client.get(
        "/api/v1/security/incidents", headers=h
    ).json()["incidents"]
    assert incidents
    assert incidents[0]["evidence_refs"]
