"""Mission supervisor tests (Reliability Addendum Modules 2 & 10).

Covered guarantees:
- persistent lifecycle + checkpoints + final report
- high-risk steps cannot run without a plan-bound approval (#3, #4)
- approval is invalidated when the plan changes
- completed steps are never re-executed on resume (#7)
- a timeout/unknown outcome is never blindly repeated (#8)
- retries are bounded, and irreversible steps are never retried (#15)
- missions are isolated per user (#2)
- an interrupted RUNNING mission is parked, not auto-run, on restart
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from config.settings import settings
from core.agent.registry import PermissionLevel, ToolSpec, registry
from core.health.recovery import OutcomeUnknown
from core.missions import planner
from core.missions import service as missions
from core.permissions.rbac import Permission
from database.models import Mission, MissionEvent, Memory, Task, User
from tests.conftest import auth_headers, register


def _user(db, username: str) -> User:
    return db.scalar(select(User).where(User.username == username))


def _add_tool(monkeypatch, name, handler, *, risk="LOW", irreversible=False):
    spec = ToolSpec(
        name=name,
        description="test mission tool",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        permission_level=PermissionLevel.SAFE,
        required_permission=Permission.CHAT,
        risk_level=risk,
        irreversible=irreversible,
    )
    monkeypatch.setitem(registry._tools, name, spec)


def _new_task(db, user_id: int, title: str = "mission target") -> Task:
    task = Task(user_id=user_id, title=title)
    db.add(task)
    db.commit()
    return task


# ----------------------------------------------------------------------
# Lifecycle: plan -> run -> verify -> report
# ----------------------------------------------------------------------

def test_low_risk_mission_plan_runs_and_verifies(client, db, student_session):
    user = _user(db, "sohan")
    mission = missions.create_mission(
        db,
        user.id,
        'remember course = BBA and create task "submit assignment" on 2026-10-14',
        actor=f"user:{user.username}",
    )
    assert mission.state == "READY"
    assert mission.approval_required is False
    assert len(missions.serialize_mission(db, mission)["steps"]) == 2

    result = missions.run_mission(
        db, user.id, mission.id, role=user.role, actor=f"user:{user.username}", sleep=lambda _s: None
    )
    assert result.state == "COMPLETED"
    assert result.progress == 100

    detail = missions.serialize_mission(db, result)
    assert all(s["state"] == "COMPLETED" for s in detail["steps"])
    assert all(s["verified"] for s in detail["steps"])
    assert len(detail["checkpoints"]) >= 2
    assert "COMPLETED" in detail["final_report"]

    # Real side effects actually happened.
    assert db.scalar(select(Memory).where(Memory.user_id == user.id, Memory.key == "course"))
    assert db.scalar(select(Task).where(Task.user_id == user.id))


def test_plan_not_possible_is_honest_not_faked(client, db, student_session):
    user = _user(db, "sohan")
    with pytest.raises(planner.PlanNotPossible):
        missions.create_mission(db, user.id, "paint the moon purple")


# ----------------------------------------------------------------------
# Approval boundary
# ----------------------------------------------------------------------

def test_high_risk_step_cannot_run_without_approval(client, db, student_session):
    user = _user(db, "sohan")
    task = _new_task(db, user.id)
    mission = missions.create_mission(
        db,
        user.id,
        f"delete task {task.id}",
        actor=f"user:{user.username}",
    )
    assert mission.state == "WAITING_FOR_APPROVAL"
    assert mission.risk_level == "HIGH"

    with pytest.raises(missions.MissionError):
        missions.run_mission(db, user.id, mission.id, role=user.role, actor="test")

    # The destructive action never ran, and the task still exists.
    assert db.get(Task, task.id) is not None


def test_denied_approval_cancels_without_execution(client, db, student_session):
    user = _user(db, "sohan")
    task = _new_task(db, user.id)
    mission = missions.create_mission(db, user.id, f"delete task {task.id}", actor="test")
    missions.approve_mission(db, user.id, mission.id, approve=False, actor="test")

    refreshed = db.get(Mission, mission.id)
    assert refreshed.state == "CANCELLED"
    assert db.get(Task, task.id) is not None


def test_approval_is_bound_to_plan_hash(client, db, student_session):
    user = _user(db, "sohan")
    task = _new_task(db, user.id)
    mission = missions.create_mission(db, user.id, f"delete task {task.id}", actor="test")

    # Simulate the plan changing after approval was requested.
    mission.plan_hash = "0" * 64
    db.commit()

    with pytest.raises(missions.MissionError):
        missions.approve_mission(db, user.id, mission.id, approve=True, actor="test")
    assert db.get(Mission, mission.id).state == "WAITING_FOR_APPROVAL"


def test_unauthorized_tool_call_inside_mission_is_rejected(client, db, student_session):
    """A STUDENT cannot delete a task, even via an approved mission (#3)."""
    user = _user(db, "sohan")
    task = _new_task(db, user.id)
    mission = missions.create_mission(db, user.id, f"delete task {task.id}", actor="test")
    missions.approve_mission(db, user.id, mission.id, approve=True, actor="test")

    result = missions.run_mission(
        db, user.id, mission.id, role=user.role, actor="test", sleep=lambda _s: None
    )
    assert result.state == "FAILED"
    step = missions.serialize_mission(db, result)["steps"][0]
    assert step["state"] == "FAILED"
    assert db.get(Task, task.id) is not None  # permission denial had no effect


def test_cancelled_mission_does_not_execute(client, db, student_session):
    user = _user(db, "sohan")
    mission = missions.create_mission(db, user.id, "remember secret_plan = do nothing", actor="test")
    missions.cancel_mission(db, user.id, mission.id, actor="test")
    assert db.get(Mission, mission.id).state == "CANCELLED"
    assert db.scalar(select(Memory).where(Memory.user_id == user.id, Memory.key == "secret_plan")) is None


# ----------------------------------------------------------------------
# Idempotency, resume, restart
# ----------------------------------------------------------------------

def test_idempotency_key_prevents_duplicate_mission(client, db, student_session):
    user = _user(db, "sohan")
    first = missions.create_mission(
        db, user.id, "remember a = 1", idempotency_key="req-123", actor="test"
    )
    second = missions.create_mission(
        db, user.id, "remember a = 2", idempotency_key="req-123", actor="test"
    )
    assert first.id == second.id
    assert db.scalar(select(Mission).where(Mission.user_id == user.id)) is not None


def test_resume_skips_completed_steps_and_does_not_repeat_timeout(
    client, db, student_session, monkeypatch
):
    user = _user(db, "sohan")
    counters = {"a": 0, "b": 0}

    def handler_a(db_, args, *, user_id, role):
        counters["a"] += 1
        return {"verified": True, "message": "a done"}

    def handler_b(db_, args, *, user_id, role):
        counters["b"] += 1
        if counters["b"] == 1:
            raise OutcomeUnknown("external call timed out; outcome unknown")
        return {"verified": True, "message": "b done"}

    _add_tool(monkeypatch, "test.a", handler_a)
    _add_tool(monkeypatch, "test.b", handler_b)

    mission = missions.create_mission(
        db,
        user.id,
        "two-step objective",
        steps=[{"tool": "test.a"}, {"tool": "test.b"}],
        actor="test",
    )
    first = missions.run_mission(
        db, user.id, mission.id, role=user.role, actor="test", sleep=lambda _s: None
    )
    assert first.state == "PAUSED"
    detail = missions.serialize_mission(db, first)
    assert detail["steps"][0]["state"] == "COMPLETED"
    assert detail["steps"][1]["state"] == "UNKNOWN_VERIFICATION"
    assert detail["steps"][1]["attempts"] == 1  # timeout never blindly retried
    assert counters == {"a": 1, "b": 1}

    resumed = missions.resume_mission(
        db, user.id, mission.id, role=user.role, actor="test", sleep=lambda _s: None
    )
    assert resumed.state == "COMPLETED"
    assert counters == {"a": 1, "b": 2}  # step A never re-executed


def test_reconcile_on_startup_parks_running_mission(client, db, student_session):
    user = _user(db, "sohan")
    mission = missions.create_mission(db, user.id, "remember restart = safe", actor="test")
    mission.state = "RUNNING"
    db.commit()

    assert missions.reconcile_on_startup(db) == 1
    refreshed = db.get(Mission, mission.id)
    assert refreshed.state == "PAUSED"
    assert db.scalar(
        select(MissionEvent).where(
            MissionEvent.mission_id == mission.id,
            MissionEvent.event == "interrupted_by_restart",
        )
    )


# ----------------------------------------------------------------------
# Bounded retries
# ----------------------------------------------------------------------

def test_transient_step_retries_are_bounded(client, db, student_session, monkeypatch):
    user = _user(db, "sohan")
    calls = {"n": 0}

    def always_fail(db_, args, *, user_id, role):
        calls["n"] += 1
        raise RuntimeError("dependency unavailable")

    _add_tool(monkeypatch, "test.flaky", always_fail)
    mission = missions.create_mission(
        db, user.id, "flaky objective", steps=[{"tool": "test.flaky"}], actor="test"
    )
    result = missions.run_mission(
        db, user.id, mission.id, role=user.role, actor="test", sleep=lambda _s: None
    )
    assert result.state == "FAILED"
    step = missions.serialize_mission(db, result)["steps"][0]
    assert step["attempts"] == settings.mission_step_max_attempts == calls["n"]


def test_irreversible_step_is_never_retried(client, db, student_session, monkeypatch):
    user = _user(db, "sohan")
    calls = {"n": 0}

    def always_fail(db_, args, *, user_id, role):
        calls["n"] += 1
        raise RuntimeError("external send failed")

    _add_tool(monkeypatch, "test.irreversible", always_fail, risk="MEDIUM", irreversible=True)
    mission = missions.create_mission(
        db, user.id, "irreversible objective", steps=[{"tool": "test.irreversible"}], actor="test"
    )
    result = missions.run_mission(
        db, user.id, mission.id, role=user.role, actor="test", sleep=lambda _s: None
    )
    assert result.state == "FAILED"
    assert calls["n"] == 1  # no automatic repeat of an irreversible action


# ----------------------------------------------------------------------
# Isolation (#2)
# ----------------------------------------------------------------------

def test_missions_are_isolated_between_users(client, db, student_session):
    owner = _user(db, "sohan")
    mission = missions.create_mission(db, owner.id, "remember mine = yes", actor="test")

    other = register(client, "other-user")
    other_user = _user(db, "other-user")

    with pytest.raises(missions.MissionError):
        missions.get_owned_mission(db, other_user.id, mission.id)

    resp = client.get(f"/api/v1/missions/{mission.id}", headers=auth_headers(other["token"]))
    assert resp.status_code == 404  # never reveals existence


# ----------------------------------------------------------------------
# API surface
# ----------------------------------------------------------------------

def test_missions_api_requires_auth(client, db):
    assert client.get("/api/v1/missions").status_code == 401


def test_missions_api_happy_path(client, db, student_session):
    headers = auth_headers(student_session["token"])
    resp = client.post(
        "/api/v1/missions",
        headers=headers,
        json={"objective": "remember api_key_note = real", "steps": [
            {"tool": "memory.store", "args": {"key": "api_note", "content": "real"}}
        ]},
    )
    assert resp.status_code == 201, resp.text
    mission_id = resp.json()["id"]

    run = client.post(f"/api/v1/missions/{mission_id}/run", headers=headers)
    assert run.status_code == 200, run.text
    assert run.json()["state"] == "COMPLETED"

    report = client.get(f"/api/v1/missions/{mission_id}/report", headers=headers)
    assert report.status_code == 200
    assert report.json()["verification"]["steps_completed"] == 1


def test_missions_api_rejects_unplannable_objective(client, db, student_session):
    headers = auth_headers(student_session["token"])
    resp = client.post("/api/v1/missions", headers=headers, json={"objective": "fly me to mars"})
    assert resp.status_code == 422
    assert "fly me to mars" in resp.json()["detail"]
