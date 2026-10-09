"""Module 3 tests: audit correlation, risk surfacing, autonomy policy.

These verify the *real* behaviour of the audit trail and risk metadata — not a
hand-written list: the tool catalog is read from the live registry.
"""

from __future__ import annotations

from sqlalchemy import select

from core.agent.registry import registry, tool_requires_approval
from core.missions import service as missions
from database.models import AuditLog, Task, User
from security.audit import audit, recent_events, trail
from tests.conftest import auth_headers


def _user(db, username: str) -> User:
    return db.scalar(select(User).where(User.username == username))


def test_audit_rows_carry_and_group_by_correlation_id(db):
    audit(db, "unit_event_a", "first", correlation_id="corr-xyz")
    audit(db, "unit_event_b", "second", correlation_id="corr-xyz")
    audit(db, "unit_event_other", "unrelated", correlation_id="corr-abc")

    grouped = recent_events(db, correlation_id="corr-xyz")
    assert {r.event for r in grouped} == {"unit_event_a", "unit_event_b"}
    assert all(r.correlation_id == "corr-xyz" for r in grouped)

    ordered = trail(db, "corr-xyz")
    assert [r.event for r in ordered] == ["unit_event_a", "unit_event_b"]


def test_mission_trail_is_reconstructable_by_correlation(client, db, student_session):
    user = _user(db, "sohan")
    mission = missions.create_mission(db, user.id, "remember corr_fact = yes", actor="test")
    missions.run_mission(
        db, user.id, mission.id, role=user.role, actor="test", sleep=lambda _s: None
    )

    rows = recent_events(db, correlation_id=mission.correlation_id)
    events = {r.event for r in rows}
    # Mission lifecycle and the underlying tool execution share one trail.
    assert "mission_created" in events
    assert "mission_finished" in events
    assert "tool_executed" in events
    assert all(r.correlation_id == mission.correlation_id for r in rows)


def test_secret_redaction_applies_within_a_correlation(db):
    audit(db, "leaky", "api_key=placeholder_value_123", correlation_id="corr-secret")
    row = trail(db, "corr-secret")[0]
    assert "placeholder_value_123" not in row.detail
    assert "[REDACTED]" in row.detail


def test_tool_risk_catalog_reflects_registry(client, admin_session):
    headers = auth_headers(admin_session["token"])
    resp = client.get("/api/v1/security/tools", headers=headers)
    assert resp.status_code == 200, resp.text
    by_name = {t["name"]: t for t in resp.json()["tools"]}
    # The registry is the source of truth.
    for name, spec in ((s.name, s) for s in registry.all_specs()):
        assert by_name[name]["risk_level"] == spec.risk_level
        assert by_name[name]["irreversible"] == spec.irreversible
        assert by_name[name]["requires_approval"] == tool_requires_approval(spec)
    # A genuinely dangerous tool is flagged as such.
    assert by_name["task.delete"]["risk_level"] == "HIGH"
    assert by_name["task.delete"]["irreversible"] is True
    assert by_name["task.delete"]["requires_approval"] is True


def test_tool_catalog_requires_security_read(client, student_session):
    headers = auth_headers(student_session["token"])
    assert client.get("/api/v1/security/tools", headers=headers).status_code == 403
    assert client.get("/api/v1/security/policy", headers=headers).status_code == 403


def test_policy_reports_real_enforced_boundaries(client, admin_session):
    headers = auth_headers(admin_session["token"])
    body = client.get("/api/v1/security/policy", headers=headers).json()
    assert body["irreversible_actions_are_never_auto_retried"] is True
    assert body["unknown_outcomes_are_never_blindly_repeated"] is True
    assert body["security_is_never_weakened_for_availability"] is True
    assert body["secrets_are_never_written_to_logs"] is True


def test_confirmation_summary_states_risk_and_reversibility(db):
    from core.agent.registry import _summarize

    spec = registry.get("task.delete")
    summary = _summarize(spec, {"task_id": 7})
    assert "HIGH" in summary
    assert "PERMANENT" in summary
    assert "task.delete" in summary


def test_audit_endpoint_exposes_correlation_id(client, db, admin_session):
    audit(db, "endpoint_corr", "detail", correlation_id="corr-endpoint")
    headers = auth_headers(admin_session["token"])
    resp = client.get("/api/v1/security/audit?correlation_id=corr-endpoint", headers=headers)
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert entries and all(e["correlation_id"] == "corr-endpoint" for e in entries)


def test_audit_trail_endpoint_returns_ordered_history(client, db, admin_session):
    audit(db, "step_one", "", correlation_id="corr-trail")
    audit(db, "step_two", "", correlation_id="corr-trail")
    headers = auth_headers(admin_session["token"])
    resp = client.get("/api/v1/security/audit/trail/corr-trail", headers=headers)
    assert resp.status_code == 200
    events = [e["event"] for e in resp.json()["entries"]]
    assert events == ["step_one", "step_two"]
    assert resp.json()["count"] == 2


def test_task_still_exists_when_delete_denied_has_correlation(client, db, student_session):
    """A denied high-risk action leaves a correlated audit trail and no effect."""
    user = _user(db, "sohan")
    task = Task(user_id=user.id, title="keep me")
    db.add(task)
    db.commit()
    mission = missions.create_mission(db, user.id, f"delete task {task.id}", actor="test")
    missions.approve_mission(db, user.id, mission.id, approve=True, actor="test")
    missions.run_mission(
        db, user.id, mission.id, role=user.role, actor="test", sleep=lambda _s: None
    )

    denied = db.scalars(
        select(AuditLog).where(
            AuditLog.event == "tool_permission_denied",
            AuditLog.correlation_id == mission.correlation_id,
        )
    ).all()
    assert denied, "permission denial must be audited under the mission correlation"
    assert db.get(Task, task.id) is not None
