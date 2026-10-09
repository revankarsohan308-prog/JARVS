"""Release Guardian service: run the gate and persist the verdict."""

from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.release.gate import Runner, run_gate, summarize
from database.models import ReleaseRun
from security.audit import audit
from timeutil import utcnow


def run_and_store(
    db: Session,
    *,
    triggered_by: str = "system",
    only: Iterable[str] | None = None,
    skip: Iterable[str] | None = None,
    runner: Runner | None = None,
    repo_root=None,
) -> dict[str, Any]:
    """Execute the gate and store the report. Never deploys anything."""
    report = run_gate(only=only, skip=skip, runner=runner, repo_root=repo_root)
    run = ReleaseRun(
        profile="full",
        status=report["status"],
        deploy_blocked=report["deploy_blocked"],
        triggered_by=triggered_by,
        items_json=json.dumps(report["items"]),
        summary=summarize(report),
        finished_at=utcnow(),
    )
    db.add(run)
    db.commit()
    audit(
        db,
        "release_gate_run",
        f"status={report['status']} deploy_blocked={report['deploy_blocked']} "
        f"blockers={report['blockers']}",
        actor=triggered_by,
        component="release",
    )
    report["run_id"] = run.id
    return report


def latest(db: Session) -> dict[str, Any]:
    run = db.scalar(select(ReleaseRun).order_by(ReleaseRun.started_at.desc()).limit(1))
    if run is None:
        return {
            "status": "NOT_RUN",
            "deploy_blocked": False,
            "detail": "The release gate has not been run on this deployment.",
        }
    return {
        "id": run.id,
        "status": run.status,
        "deploy_blocked": run.deploy_blocked,
        "triggered_by": run.triggered_by,
        "summary": run.summary,
        "items": json.loads(run.items_json or "[]"),
        "started_at": run.started_at.isoformat() + "Z",
        "finished_at": run.finished_at.isoformat() + "Z" if run.finished_at else None,
    }
