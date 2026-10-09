"""Autonomous mission supervisor (Reliability Addendum Module 2).

Upgrades the existing mission capability rather than duplicating it: every
step is executed through the *same* ``core.agent.registry.execute_tool`` choke
point the chat brain uses, so the permission engine, confirmation gate and
audit trail all still apply. The supervisor never bypasses them.

Guarantees:
- Persistent state + checkpoints; an interrupted RUNNING mission is reconciled
  to PAUSED on boot and can be resumed explicitly.
- Steps run one at a time, in dependency order, only from READY/RUNNING.
- High-risk or confirmation-gated steps require a mission-level approval bound
  to the exact plan hash; a changed plan invalidates the approval.
- Bounded retries. Irreversible or HIGH/CRITICAL steps are never auto-retried.
- A timeout/unknown outcome is never blindly repeated: the step is parked as
  UNKNOWN_VERIFICATION and the mission is paused for human inspection.
- Cancellation and pause are honoured between steps, never mid-write.
- Idempotency: completed steps are never re-executed on resume.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from core.agent.registry import (
    ExecutionResult,
    execute_tool,
    registry,
    risk_rank,
    tool_requires_approval,
)
from core.health.recovery import PermanentFailure, RetryPolicy, run_bounded
from core.missions import planner
from core.missions.planner import Plan, PlanNotPossible
from database.models import (
    Mission,
    MissionApproval,
    MissionCheckpoint,
    MissionEvent,
    MissionStep,
)
from security.audit import audit
from timeutil import utcnow

STATES = (
    "PLANNING",
    "WAITING_FOR_APPROVAL",
    "READY",
    "RUNNING",
    "PAUSED",
    "RECOVERING",
    "VERIFYING",
    "COMPLETED",
    "PARTIALLY_COMPLETED",
    "FAILED",
    "CANCELLED",
)
TERMINAL_STATES = {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED"}


class MissionError(Exception):
    """User-safe mission error."""


class _StepFailed(Exception):
    """Transient step failure eligible for bounded retry."""


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _event(db: Session, mission: Mission, event: str, detail: str = "", actor: str = "system") -> None:
    db.add(
        MissionEvent(
            mission_id=mission.id,
            event=event,
            detail=detail[:2000],
            actor=actor,
        )
    )
    audit(
        db,
        f"mission_{event}",
        f"mission={mission.id} {detail[:300]}",
        actor=actor,
        component="missions",
        correlation_id=mission.correlation_id,
    )


def _checkpoint(db: Session, mission: Mission, label: str, summary: str = "") -> MissionCheckpoint:
    seq = db.scalar(
        select(MissionCheckpoint.sequence)
        .where(MissionCheckpoint.mission_id == mission.id)
        .order_by(MissionCheckpoint.sequence.desc())
        .limit(1)
    )
    row = MissionCheckpoint(
        mission_id=mission.id,
        sequence=(seq or 0) + 1,
        state=mission.state,
        label=label[:255],
        summary=summary[:2000],
    )
    db.add(row)
    return row


def get_owned_mission(db: Session, user_id: int, mission_id: str) -> Mission:
    mission = db.get(Mission, mission_id)
    if mission is None or mission.user_id != user_id:
        # Never reveal whether another user's mission exists.
        raise MissionError("Mission not found.")
    return mission


def _approved_for_plan(db: Session, mission: Mission) -> bool:
    if not mission.approval_required:
        return True
    row = db.scalar(
        select(MissionApproval)
        .where(
            MissionApproval.mission_id == mission.id,
            MissionApproval.decision == "APPROVED",
            MissionApproval.plan_hash == mission.plan_hash,
        )
        .order_by(MissionApproval.requested_at.desc())
        .limit(1)
    )
    return row is not None


def _steps(db: Session, mission_id: str) -> list[MissionStep]:
    return list(
        db.scalars(
            select(MissionStep)
            .where(MissionStep.mission_id == mission_id)
            .order_by(MissionStep.position)
        )
    )


# ----------------------------------------------------------------------
# Creation
# ----------------------------------------------------------------------

def create_mission(
    db: Session,
    user_id: int,
    objective: str,
    *,
    steps: list[dict[str, Any]] | None = None,
    deadline=None,
    idempotency_key: str | None = None,
    actor: str = "system",
) -> Mission:
    from core.agent import catalog

    catalog.register_all_tools(registry)  # idempotent; ensures plan validation works

    if idempotency_key:
        existing = db.scalar(
            select(Mission).where(
                Mission.user_id == user_id, Mission.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing  # duplicate-execution prevention

    if steps:
        plan = planner.plan_from_explicit(steps)
    else:
        plan = planner.plan_objective(objective)

    mission = Mission(
        user_id=user_id,
        objective=objective[:4000],
        state="PLANNING",
        risk_level=planner.plan_risk(plan),
        plan_hash=planner.plan_hash(plan),
        idempotency_key=idempotency_key,
        deadline=deadline,
    )
    db.add(mission)
    db.flush()

    needs_approval = False
    for position, step in enumerate(plan.steps):
        spec = registry.get(registry.resolve_name(step.tool_name) or "")
        if spec is None:  # pragma: no cover - validate_plan already checked
            raise MissionError(f"Unknown tool: {step.tool_name}")
        requires = tool_requires_approval(spec)
        needs_approval = needs_approval or requires
        db.add(
            MissionStep(
                mission_id=mission.id,
                position=position,
                name=step.name[:255],
                tool_name=spec.name,
                args_json=json.dumps(step.args),
                depends_on_json=json.dumps(step.depends_on),
                risk_level=spec.risk_level,
                irreversible=spec.irreversible,
                requires_approval=requires,
                max_attempts=settings.mission_step_max_attempts,
                idempotency_key=f"{mission.id}:{position}",
            )
        )

    mission.approval_required = needs_approval
    mission.state = "WAITING_FOR_APPROVAL" if needs_approval else "READY"
    if needs_approval:
        db.add(
            MissionApproval(
                mission_id=mission.id,
                plan_hash=mission.plan_hash,
                summary=_plan_summary(plan),
                decision="PENDING",
            )
        )
    _event(db, mission, "created", f"steps={len(plan.steps)} risk={mission.risk_level}", actor=actor)
    _checkpoint(db, mission, "created", _plan_summary(plan))
    db.commit()
    if needs_approval:
        _notify_owner(
            db,
            mission,
            severity="MEDIUM",
            title=f"Mission needs your approval ({mission.risk_level} risk)",
            body=f"Objective: {mission.objective[:300]}\nPlan: {_plan_summary(plan)[:500]}",
            kind="approval_needed",
        )
    return mission


def _plan_summary(plan: Plan) -> str:
    return "; ".join(f"{i + 1}. {s.name} [{s.tool_name}]" for i, s in enumerate(plan.steps))


def _notify_owner(
    db: Session, mission: Mission, *, severity: str, title: str, body: str = "", kind: str = ""
) -> None:
    from core.notifications import service as notifications

    notifications.notify(
        db,
        mission.user_id,
        category="MISSION",
        severity=severity,
        title=title,
        body=body,
        dedup_key=f"mission:{mission.id}:{kind or severity}",
        correlation_id=mission.correlation_id,
        actor="missions",
    )


# ----------------------------------------------------------------------
# Approval
# ----------------------------------------------------------------------

def approve_mission(
    db: Session, user_id: int, mission_id: str, approve: bool, actor: str = "system"
) -> Mission:
    mission = get_owned_mission(db, user_id, mission_id)
    if mission.state != "WAITING_FOR_APPROVAL":
        raise MissionError(f"Mission is {mission.state}; it is not awaiting approval.")
    approval = db.scalar(
        select(MissionApproval)
        .where(MissionApproval.mission_id == mission.id, MissionApproval.decision == "PENDING")
        .order_by(MissionApproval.requested_at.desc())
        .limit(1)
    )
    if approval is None:
        raise MissionError("No pending approval request for this mission.")
    if approval.plan_hash != mission.plan_hash:
        # The plan changed after approval was requested: the old approval does
        # not cover the new action.
        approval.decision = "STALE"
        approval.decided_at = utcnow()
        db.commit()
        raise MissionError("The mission plan changed; a fresh approval is required.")

    approval.decision = "APPROVED" if approve else "DENIED"
    approval.decided_by = actor
    approval.decided_at = utcnow()
    mission.state = "READY" if approve else "CANCELLED"
    if not approve:
        mission.finished_at = utcnow()
        mission.final_report = "Cancelled: the owner denied the approval request."
    _event(db, mission, "approved" if approve else "denied", f"plan_hash={mission.plan_hash}", actor=actor)
    _checkpoint(db, mission, "approval", approval.decision)
    db.commit()
    return mission


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------

def _effective_attempts(step: MissionStep) -> int:
    """Irreversible / high-risk steps are never retried automatically."""
    if step.irreversible or risk_rank(step.risk_level) >= risk_rank("HIGH"):
        return 1
    return max(1, step.max_attempts)


def run_mission(
    db: Session,
    user_id: int,
    mission_id: str,
    *,
    role: str,
    actor: str = "system",
    sleep=None,
) -> Mission:
    mission = get_owned_mission(db, user_id, mission_id)
    if mission.state in TERMINAL_STATES:
        return mission
    if mission.cancel_requested:
        return cancel_mission(db, user_id, mission_id, actor=actor)
    if not _approved_for_plan(db, mission):
        mission.state = "WAITING_FOR_APPROVAL"
        _event(db, mission, "blocked", "approval required before running", actor=actor)
        db.commit()
        raise MissionError("This mission requires approval before it can run.")

    if mission.deadline is not None and utcnow() > mission.deadline:
        return _pause(db, mission, "Deadline passed before the mission could run.", actor)

    mission.state = "RUNNING"
    mission.started_at = mission.started_at or utcnow()
    _event(db, mission, "started", f"risk={mission.risk_level}", actor=actor)
    db.commit()

    steps = _steps(db, mission.id)
    total = len(steps)
    policy_base = RetryPolicy(
        max_attempts=settings.mission_step_max_attempts,
        base_delay=settings.mission_retry_base_delay_seconds,
        max_delay=settings.health_retry_max_delay_seconds,
    )

    for index, step in enumerate(steps):
        if mission.cancel_requested:
            return _finalize_cancelled(db, mission, steps[index:], actor)

        if mission.deadline is not None and utcnow() > mission.deadline:
            return _pause(db, mission, "Deadline reached mid-mission; progress preserved.", actor)

        if step.state in ("COMPLETED", "CANCELLED"):
            continue  # idempotency: never re-execute a completed step

        deps = json.loads(step.depends_on_json or "[]")
        if any(steps[d].state != "COMPLETED" for d in deps if 0 <= d < total):
            step.state = "SKIPPED"
            step.last_error = "A prerequisite step did not complete."
            step.finished_at = utcnow()
            db.commit()
            continue

        spec = registry.get(step.tool_name)
        if spec is None:
            step.state = "FAILED"
            step.last_error = f"Tool '{step.tool_name}' is no longer available."
            db.commit()
            return _finalize(db, mission, steps, actor)

        step.state = "RUNNING"
        step.started_at = step.started_at or utcnow()
        db.commit()

        args = json.loads(step.args_json or "{}")
        approved = _approved_for_plan(db, mission)
        attempts_used = {"n": 0}

        def operation() -> ExecutionResult:
            attempts_used["n"] += 1
            result = execute_tool(
                db,
                spec,
                args,
                user_id=user_id,
                role=role,
                settings_obj=settings,
                actor=actor,
                confirmed=approved,
                correlation_id=mission.correlation_id,
            )
            if result.needs_confirmation:
                raise PermanentFailure("step requires confirmation")
            if result.denied_reason:
                raise PermanentFailure(result.denied_reason)
            if not result.ok:
                raise _StepFailed(result.message or "step did not succeed")
            return result

        policy = RetryPolicy(
            max_attempts=_effective_attempts(step),
            base_delay=policy_base.base_delay,
            max_delay=policy_base.max_delay,
        )
        outcome = run_bounded(operation, policy=policy, sleep=sleep or _sleep_default)
        step.attempts += max(1, attempts_used["n"])
        mission.retry_count += max(0, attempts_used["n"] - 1)

        if outcome.success and isinstance(outcome.value, ExecutionResult):
            result = outcome.value
            step.state = "COMPLETED"
            step.verified = result.verified
            step.result_json = json.dumps({"data": result.data, "message": result.message})
            step.finished_at = utcnow()
            mission.progress = int(((index + 1) / total) * 100)
            _checkpoint(db, mission, f"step_{index + 1}_completed", step.name)
            _event(db, mission, "step_completed", f"#{index + 1} {step.name}", actor=actor)
            db.commit()
            continue

        if outcome.outcome == "OUTCOME_UNKNOWN":
            # A timeout must never be blindly repeated: park for human review.
            step.state = "UNKNOWN_VERIFICATION"
            step.last_error = outcome.detail[:2000]
            step.finished_at = utcnow()
            db.commit()
            return _pause(db, mission, f"Step '{step.name}' outcome is unknown: {outcome.detail}", actor)

        # FAILED_PERMANENT or GAVE_UP
        step.state = "FAILED"
        step.last_error = outcome.detail[:2000]
        step.finished_at = utcnow()
        db.commit()
        _event(db, mission, "step_failed", f"#{index + 1} {step.name}: {outcome.detail[:200]}", actor=actor)
        return _finalize(db, mission, steps, actor)

    return _finalize(db, mission, steps, actor)


def _sleep_default(seconds: float) -> None:
    import time as _time

    _time.sleep(seconds)


def _pause(db: Session, mission: Mission, reason: str, actor: str) -> Mission:
    mission.state = "PAUSED"
    mission.verification_json = json.dumps({"pause_reason": reason})
    _event(db, mission, "paused", reason, actor=actor)
    _checkpoint(db, mission, "paused", reason)
    db.commit()
    _notify_owner(
        db,
        mission,
        severity="HIGH" if "unknown" in reason.lower() else "MEDIUM",
        title=f"Mission paused: {mission.objective[:80]}",
        body=reason[:500],
        kind="paused",
    )
    return mission


def _finalize(db: Session, mission: Mission, steps: list[MissionStep], actor: str) -> Mission:
    completed = [s for s in steps if s.state == "COMPLETED"]
    failed = [s for s in steps if s.state == "FAILED"]
    unknown = [s for s in steps if s.state == "UNKNOWN_VERIFICATION"]
    total = len(steps) or 1
    mission.progress = int((len(completed) / total) * 100)
    mission.verification_json = json.dumps(
        {
            "steps_total": len(steps),
            "steps_completed": len(completed),
            "steps_failed": len(failed),
            "steps_unknown": len(unknown),
            "verified_steps": [s.position for s in completed if s.verified],
            "unverified_steps": [s.position for s in completed if s.verified is False],
        }
    )
    if len(completed) == len(steps) and steps:
        mission.state = "COMPLETED"
    elif completed:
        mission.state = "PARTIALLY_COMPLETED"
    else:
        mission.state = "FAILED"
    mission.finished_at = utcnow()
    mission.final_report = (
        f"Mission {mission.id}: {mission.state}. "
        f"{len(completed)}/{len(steps)} step(s) completed"
        + (f", {len(failed)} failed" if failed else "")
        + (f", {len(unknown)} awaiting verification" if unknown else "")
        + f". Objective: {mission.objective[:200]}"
    )
    _event(db, mission, "finished", f"state={mission.state} {mission.progress}%", actor=actor)
    _checkpoint(db, mission, "finished", mission.final_report)
    db.commit()
    if mission.state == "FAILED":
        _notify_owner(
            db,
            mission,
            severity="HIGH",
            title=f"Mission failed: {mission.objective[:80]}",
            body=mission.final_report[:800],
            kind="failed",
        )
    elif mission.state == "PARTIALLY_COMPLETED":
        _notify_owner(
            db,
            mission,
            severity="MEDIUM",
            title=f"Mission partially completed: {mission.objective[:80]}",
            body=mission.final_report[:800],
            kind="partial",
        )
    return mission


def _finalize_cancelled(db: Session, mission: Mission, remaining: list[MissionStep], actor: str) -> Mission:
    for step in remaining:
        if step.state not in ("COMPLETED",):
            step.state = "CANCELLED"
    mission.state = "CANCELLED"
    mission.finished_at = utcnow()
    mission.final_report = "Cancelled by the owner. Completed steps remain recorded."
    _event(db, mission, "cancelled", "requested during execution", actor=actor)
    _checkpoint(db, mission, "cancelled", mission.final_report)
    db.commit()
    return mission


# ----------------------------------------------------------------------
# Lifecycle controls
# ----------------------------------------------------------------------

def pause_mission(db: Session, user_id: int, mission_id: str, actor: str = "system", reason: str = "") -> Mission:
    mission = get_owned_mission(db, user_id, mission_id)
    if mission.state in TERMINAL_STATES:
        raise MissionError(f"Mission is already {mission.state}.")
    return _pause(db, mission, reason or "Paused by the owner.", actor)


def resume_mission(
    db: Session, user_id: int, mission_id: str, *, role: str, actor: str = "system", sleep=None
) -> Mission:
    mission = get_owned_mission(db, user_id, mission_id)
    if mission.state != "PAUSED":
        raise MissionError(f"Mission is {mission.state}; only a PAUSED mission can be resumed.")
    if not _approved_for_plan(db, mission):
        mission.state = "WAITING_FOR_APPROVAL"
        db.commit()
        raise MissionError("This mission needs a fresh approval before it can resume.")
    mission.state = "RUNNING"
    _event(db, mission, "resumed", "explicit resume after pause/restart", actor=actor)
    _checkpoint(db, mission, "resumed", "resuming from last checkpoint")
    db.commit()
    return run_mission(db, user_id, mission_id, role=role, actor=actor, sleep=sleep)


def cancel_mission(db: Session, user_id: int, mission_id: str, actor: str = "system") -> Mission:
    mission = get_owned_mission(db, user_id, mission_id)
    if mission.state in TERMINAL_STATES:
        return mission
    mission.cancel_requested = True
    steps = _steps(db, mission.id)
    for step in steps:
        if step.state not in ("COMPLETED",):
            step.state = "CANCELLED"
    mission.state = "CANCELLED"
    mission.finished_at = utcnow()
    mission.final_report = "Cancelled by the owner. Completed steps remain recorded."
    _event(db, mission, "cancelled", "owner request", actor=actor)
    _checkpoint(db, mission, "cancelled", mission.final_report)
    db.commit()
    return mission


def reconcile_on_startup(db: Session) -> int:
    """Safe resume after restart: never auto-run; park interrupted missions.

    A mission that was RUNNING when the process died is moved to PAUSED with a
    checkpoint, so no step is silently re-executed on boot. The owner resumes
    explicitly, and resume skips already-completed steps.
    """
    rows = db.scalars(select(Mission).where(Mission.state.in_(("RUNNING", "RECOVERING")))).all()
    for mission in rows:
        mission.state = "PAUSED"
        _event(db, mission, "interrupted_by_restart", "process restarted mid-mission", actor="system")
        _checkpoint(db, mission, "interrupted", "application restart; progress preserved")
    if rows:
        db.commit()
    return len(rows)


# ----------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------

def serialize_mission(db: Session, mission: Mission, *, include_history: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": mission.id,
        "objective": mission.objective,
        "state": mission.state,
        "risk_level": mission.risk_level,
        "progress": mission.progress,
        "retry_count": mission.retry_count,
        "cancel_requested": mission.cancel_requested,
        "approval_required": mission.approval_required,
        "plan_hash": mission.plan_hash,
        "correlation_id": mission.correlation_id,
        "deadline": mission.deadline.isoformat() + "Z" if mission.deadline else None,
        "created_at": mission.created_at.isoformat() + "Z",
        "updated_at": mission.updated_at.isoformat() + "Z",
        "started_at": mission.started_at.isoformat() + "Z" if mission.started_at else None,
        "finished_at": mission.finished_at.isoformat() + "Z" if mission.finished_at else None,
        "verification": json.loads(mission.verification_json or "{}"),
        "final_report": mission.final_report,
        "steps": [
            {
                "id": s.id,
                "position": s.position,
                "name": s.name,
                "tool": s.tool_name,
                "args": json.loads(s.args_json or "{}"),
                "depends_on": json.loads(s.depends_on_json or "[]"),
                "state": s.state,
                "risk_level": s.risk_level,
                "irreversible": s.irreversible,
                "requires_approval": s.requires_approval,
                "attempts": s.attempts,
                "verified": s.verified,
                "result": json.loads(s.result_json) if s.result_json else None,
                "last_error": s.last_error,
            }
            for s in _steps(db, mission.id)
        ],
    }
    if include_history:
        data["checkpoints"] = [
            {
                "sequence": c.sequence,
                "state": c.state,
                "label": c.label,
                "summary": c.summary,
                "created_at": c.created_at.isoformat() + "Z",
            }
            for c in db.scalars(
                select(MissionCheckpoint)
                .where(MissionCheckpoint.mission_id == mission.id)
                .order_by(MissionCheckpoint.sequence)
            )
        ]
        data["events"] = [
            {
                "event": e.event,
                "detail": e.detail,
                "actor": e.actor,
                "created_at": e.created_at.isoformat() + "Z",
            }
            for e in db.scalars(
                select(MissionEvent)
                .where(MissionEvent.mission_id == mission.id)
                .order_by(MissionEvent.created_at)
            )
        ]
        data["approvals"] = [
            {
                "id": a.id,
                "decision": a.decision,
                "plan_hash": a.plan_hash,
                "summary": a.summary,
                "decided_by": a.decided_by,
                "requested_at": a.requested_at.isoformat() + "Z",
                "decided_at": a.decided_at.isoformat() + "Z" if a.decided_at else None,
            }
            for a in db.scalars(
                select(MissionApproval)
                .where(MissionApproval.mission_id == mission.id)
                .order_by(MissionApproval.requested_at)
            )
        ]
    return data


def list_missions(db: Session, user_id: int, limit: int = 100) -> list[Mission]:
    return list(
        db.scalars(
            select(Mission)
            .where(Mission.user_id == user_id)
            .order_by(Mission.created_at.desc())
            .limit(limit)
        )
    )


def mission_counts(db: Session, user_id: int) -> dict[str, int]:
    from sqlalchemy import func

    rows = db.execute(
        select(Mission.state, func.count(Mission.id))
        .where(Mission.user_id == user_id)
        .group_by(Mission.state)
    ).all()
    return {state: count for state, count in rows}


__all__ = [
    "MissionError",
    "PlanNotPossible",
    "STATES",
    "TERMINAL_STATES",
    "approve_mission",
    "cancel_mission",
    "create_mission",
    "get_owned_mission",
    "list_missions",
    "mission_counts",
    "pause_mission",
    "reconcile_on_startup",
    "resume_mission",
    "run_mission",
    "serialize_mission",
]
