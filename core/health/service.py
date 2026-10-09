"""Health & recovery service (Reliability Addendum Module 1).

The single place that knows which components are monitored, runs their real
probes, persists the outcome, and — when it is safe — performs a bounded
recovery and re-verifies.

Recovery workflow (enforced by ``run_component``):
    DETECT -> CLASSIFY -> DIAGNOSE -> CHECK SAFE -> RECOVER IF SAFE
            -> VERIFY -> REPORT

Safety rules:
- Only components in ``checks.RECOVERABLE`` ever auto-recover, and only with
  a read-only re-probe under a bounded RetryPolicy.
- NOT_CONFIGURED components are never "recovered" — there is nothing to fix
  automatically; the diagnostic says what configuration is required.
- Cooldown prevents retry storms: while a component is cooling down, new
  recovery episodes are recorded as SKIPPED_COOLDOWN, not executed.
- Every episode lands in ``recovery_attempts`` and the audit log; state
  *transitions* are audited once, so a component that stays down does not
  flood the audit trail.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.health import checks
from core.health.checks import CheckResult
from core.health.recovery import (
    PermanentFailure,
    RetryPolicy,
    RecoveryOutcome,
    guard,
    run_bounded,
)
from database.models import ComponentStatusRecord, RecoveryAttempt, new_id
from security.audit import audit
from timeutil import utcnow

# Component catalog: name -> (probe, human description).
COMPONENTS: dict[str, tuple[Any, str]] = {
    "database": (checks.check_database, "Primary database round-trip."),
    "ai_provider": (checks.check_ai_provider, "AI provider availability (real round trip when configured)."),
    "voice_server": (checks.check_voice_server, "Voice WS server reachability and engine configuration."),
    "storage": (checks.check_storage, "Disk capacity on the data volume."),
    "workers": (checks.check_workers, "Background worker heartbeats."),
    "devices": (checks.check_devices, "Paired-device connectivity."),
    "backup": (checks.check_backup, "Backup subsystem status."),
    "payments": (checks.check_payments, "Payment integration status."),
}

_RECOVERY_STRATEGY = "bounded_recheck"

# In-process worker heartbeats: name -> monotonic timestamp.
_heartbeats: dict[str, float] = {}


def record_worker_heartbeat(name: str) -> None:
    """Called by background workers so their health is observable."""
    _heartbeats[name] = time.monotonic()


def worker_heartbeat_age_seconds(name: str) -> float | None:
    seen = _heartbeats.get(name)
    if seen is None:
        return None
    return round(time.monotonic() - seen, 2)


def reset_runtime_state() -> None:
    """Test helper: clear worker heartbeats and the recovery cooldown guard."""
    _heartbeats.clear()
    guard.reset()


# ----------------------------------------------------------------------
# Persistence of component status
# ----------------------------------------------------------------------

def _get_or_create(db: Session, component: str) -> ComponentStatusRecord:
    row = db.scalar(select(ComponentStatusRecord).where(ComponentStatusRecord.component == component))
    if row is None:
        row = ComponentStatusRecord(component=component)
        db.add(row)
        db.flush()
    return row


def record_result(
    db: Session, component: str, result: CheckResult, *, strategy: str = ""
) -> tuple[ComponentStatusRecord, bool]:
    """Persist a probe result. Returns (row, state_changed).

    Auditing happens only on state *transitions* (or the first record) so a
    component that stays broken does not generate one audit row per check.
    """
    row = _get_or_create(db, component)
    previous = row.status
    row.status = result.status
    row.severity = result.severity
    row.detail = result.detail[:2000]
    row.latency_ms = result.latency_ms
    row.recovery_strategy = strategy or row.recovery_strategy
    row.diagnostics_json = json.dumps(result.diagnostics)
    now = utcnow()
    if result.ok:
        row.failure_count = 0
        row.last_success_at = now
    else:
        row.failure_count = (row.failure_count or 0) + 1
        row.last_failure_at = now
    row.updated_at = now
    changed = previous != result.status
    db.commit()
    if changed:
        audit(
            db,
            "health_state_changed",
            f"component={component} {previous or 'UNRECORDED'}->{result.status} detail={result.detail[:200]}",
            component="health",
        )
        _notify_health_transition(db, component, previous, result)
    return row, changed


def _notify_health_transition(db: Session, component: str, previous: str | None, result: CheckResult) -> None:
    """Raise a notification on a real health transition (Module 8)."""
    unhealthy = {"OFFLINE", "FAILED", "DEGRADED"}
    try:
        from core.notifications import service as notifications

        if result.status in ("OFFLINE", "FAILED"):
            notifications.notify_admins(
                db,
                category="HEALTH",
                severity="HIGH" if result.status == "FAILED" else "MEDIUM",
                title=f"Component '{component}' is {result.status}",
                body=result.detail[:500],
                dedup_key=f"health:{component}:{result.status}",
                actor="health",
            )
        elif previous in unhealthy and result.status == "HEALTHY":
            notifications.notify_admins(
                db,
                category="HEALTH",
                severity="INFO",
                title=f"Component '{component}' recovered",
                body="A real re-probe confirmed the component is healthy again.",
                dedup_key=f"health:{component}:recovered",
                actor="health",
            )
    except Exception:  # notifications must never break health monitoring
        import logging

        logging.getLogger("alyo").exception("health notification hook failed")


def _record_recovery_outcome(
    db: Session,
    component: str,
    strategy: str,
    outcome: str,
    detail: str,
    probes: int,
) -> RecoveryAttempt:
    attempt = RecoveryAttempt(
        id=new_id(),
        component=component,
        strategy=strategy,
        outcome=outcome,
        detail=detail[:2000],
        probes=probes,
    )
    db.add(attempt)
    db.commit()
    audit(
        db,
        "recovery_attempted",
        f"component={component} strategy={strategy} outcome={outcome} probes={probes}",
        component="health",
    )
    return attempt


# ----------------------------------------------------------------------
# The workflow: DETECT -> CLASSIFY -> ... -> REPORT
# ----------------------------------------------------------------------

def run_component(
    db: Session,
    name: str,
    *,
    recover: bool = True,
    policy: RetryPolicy | None = None,
    sleep=None,  # tests inject a no-op; production waits between re-probes
) -> dict[str, Any]:
    """Probe one component, persist status, and recover if safe and useful."""
    entry = COMPONENTS.get(name)
    if entry is None:
        raise KeyError(f"Unknown component: {name}")
    probe, description = entry

    # DETECT
    result = probe()

    # CLASSIFY / DIAGNOSE / CHECK SAFE
    recovery: RecoveryOutcome | None = None
    strategy = ""
    if (
        recover
        and not result.ok
        and name in checks.RECOVERABLE
        and result.diagnostics.get("recoverable") is not False
    ):
        # SAFE: the recovery action is a read-only re-probe with bounded
        # backoff — it cannot modify data or bypass any control.
        if guard.allow(name):
            guard.start(name)
            strategy = _RECOVERY_STRATEGY
            row = _get_or_create(db, name)
            row.status = "RECOVERING"
            db.commit()
            attempts_used = 0

            def _reprobe() -> CheckResult:
                nonlocal attempts_used
                attempts_used += 1
                outcome = probe()
                if not outcome.ok:
                    # A probe that reports a non-recoverable failure (e.g.
                    # rejected credentials) must never be retried.
                    if outcome.diagnostics.get("recoverable") is False:
                        raise PermanentFailure(outcome.detail or "failure is permanent")
                    raise _ProbeFailed(outcome)
                return outcome

            recovery = run_bounded(
                _reprobe,
                policy=policy or RetryPolicy.from_settings(),
                sleep=sleep or time.sleep,
            )
            result = _reprobe_result(recovery, result)
            _record_recovery_outcome(
                db,
                name,
                strategy,
                recovery.outcome,
                recovery.detail or "Re-probe succeeded after recovery.",
                max(1, attempts_used),
            )
        else:
            strategy = "cooldown"
            _record_recovery_outcome(
                db,
                name,
                "cooldown",
                "SKIPPED_COOLDOWN",
                "Recovery skipped: still within the recovery cooldown window "
                "(retry-storm guard).",
                1,
            )

    # VERIFY / REPORT
    row, changed = record_result(db, name, result, strategy=strategy)
    return {
        "component": name,
        "description": description,
        "status": row.status,
        "severity": row.severity,
        "detail": row.detail,
        "latency_ms": row.latency_ms,
        "failure_count": row.failure_count,
        "recovery_strategy": row.recovery_strategy,
        "last_success_at": row.last_success_at.isoformat() + "Z" if row.last_success_at else None,
        "last_failure_at": row.last_failure_at.isoformat() + "Z" if row.last_failure_at else None,
        "state_changed": changed,
        "recovery": (
            {
                "outcome": recovery.outcome,
                "attempts": recovery.attempts,
                "detail": recovery.detail,
            }
            if recovery
            else None
        ),
    }


class _ProbeFailed(Exception):
    """Internal: a re-probe returned a non-healthy result inside run_bounded."""

    def __init__(self, result: CheckResult) -> None:
        super().__init__(result.detail or result.status)
        self.result = result


def _reprobe_result(recovery: RecoveryOutcome, original: CheckResult) -> CheckResult:
    """Fold the recovery outcome into the final reported CheckResult.

    A component is only reported healthy when a re-probe *proved* it healthy.
    """
    if recovery.success and isinstance(recovery.value, CheckResult):
        healed = recovery.value
        healed.detail = f"Recovered after re-probe: {healed.detail}"
        return healed
    failed = original
    failed.detail = (
        f"Recovery {recovery.outcome.lower().replace('_', ' ')} after "
        f"{recovery.attempts} attempt(s). {recovery.detail}"
    ).strip()
    failed.diagnostics = {
        **failed.diagnostics,
        "recovery_outcome": recovery.outcome,
        "recovery_attempts": recovery.attempts,
    }
    return failed


def run_all(db: Session, *, recover: bool = True) -> dict[str, Any]:
    """Run every registered component check."""
    components = [run_component(db, name, recover=recover) for name in COMPONENTS]
    return {
        "overall": overall_status(components),
        "checked_at": utcnow().isoformat() + "Z",
        "components": components,
    }


def overall_status(components: list[dict[str, Any]] | None = None) -> str:
    """Worst real status across components.

    Failures dominate; NOT_CONFIGURED is informational and never masks a
    healthy system, but is reported when it is all there is.
    """
    if components is None:
        return "UNKNOWN"
    order = ["FAILED", "OFFLINE", "DEGRADED", "UNKNOWN", "RECOVERING", "HEALTHY", "NOT_CONFIGURED"]
    statuses = {c["status"] for c in components}
    for status in order:
        if status in statuses:
            return status
    return "UNKNOWN"


def recovery_history(db: Session, component: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    stmt = select(RecoveryAttempt).order_by(RecoveryAttempt.created_at.desc())
    if component:
        stmt = stmt.where(RecoveryAttempt.component == component)
    rows = db.scalars(stmt.limit(min(limit, 100))).all()
    return [
        {
            "id": r.id,
            "component": r.component,
            "strategy": r.strategy,
            "outcome": r.outcome,
            "detail": r.detail,
            "probes": r.probes,
            "created_at": r.created_at.isoformat() + "Z",
        }
        for r in rows
    ]
