"""Security Center routes (spec §15.5 / §15.9 / §15.6).

Live status, events, incidents, audit access, escalation contacts. Every
alert carries WHAT/WHY/SEVERITY/WHAT-ALYO-DID/NEXT (§15.13) because the
monitoring module stores cause, action and evidence with each event.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.deps import CurrentUser, DbDep, require_permission
from core.permissions.rbac import Permission
from database.models import Incident, SecurityEvent, User
from security import monitoring
from security.audit import recent_events
from timeutil import utcnow

router = APIRouter(prefix="/security", tags=["security"])

SecurityReader = Annotated[User, Depends(require_permission(Permission.SECURITY_READ))]
SecurityManager = Annotated[User, Depends(require_permission(Permission.SECURITY_MANAGE))]
Auditor = Annotated[User, Depends(require_permission(Permission.AUDIT_READ))]


class IncidentStateIn(BaseModel):
    state: str = Field(min_length=1, max_length=24)
    note: str = Field(default="", max_length=1000)


class ResolutionIn(BaseModel):
    resolution_notes: str = Field(min_length=1, max_length=2000)
    status: str = Field(default="RESOLVED", max_length=24)


@router.get("/status")
def status(user: CurrentUser, db: DbDep):
    """Dashboard status. Available to every authenticated user (free tier)."""
    payload = monitoring.security_status(db)
    payload["failed_logins"] = monitoring.failed_login_stats(db)
    payload["your_role"] = user.role
    return payload


@router.get("/events")
def events(user: CurrentUser, db: DbDep, limit: int = 50, status_filter: str = "OPEN"):
    stmt = select(SecurityEvent)
    if status_filter and status_filter != "ALL":
        stmt = stmt.where(SecurityEvent.status == status_filter.upper())
    rows = db.scalars(stmt.order_by(SecurityEvent.created_at.desc()).limit(min(limit, 200))).all()
    return {"events": [_event(e) for e in rows]}


@router.get("/events/{event_id}")
def event_detail(event_id: str, user: CurrentUser, db: DbDep):
    row = db.get(SecurityEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    return {"event": _event(row)}


@router.post("/events/{event_id}/resolve")
def resolve_event(event_id: str, body: ResolutionIn, user: SecurityManager, db: DbDep):
    row = db.get(SecurityEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    row.status = "RESOLVED"
    row.resolved_at = utcnow()
    row.resolution_notes = body.resolution_notes
    db.commit()
    from security.audit import audit

    audit(db, "security_event_resolved", f"event={event_id}", actor=f"user:{user.username}", component="security")
    return {"ok": True, "event": _event(row)}


@router.get("/incidents")
def incidents(user: SecurityReader, db: DbDep, include_resolved: bool = False):
    stmt = select(Incident)
    if not include_resolved:
        stmt = stmt.where(Incident.state.notin_(("RESOLVED", "FALSE_POSITIVE")))
    rows = db.scalars(stmt.order_by(Incident.created_at.desc()).limit(200)).all()
    return {"incidents": [_incident(i) for i in rows]}


@router.post("/incidents/{incident_id}/state")
def transition(incident_id: str, body: IncidentStateIn, user: SecurityManager, db: DbDep):
    try:
        incident = monitoring.transition_incident(
            db, incident_id, body.state.upper(), actor=f"user:{user.username}", note=body.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "incident": _incident(incident)}


@router.get("/audit")
def audit_log(user: Auditor, db: DbDep, limit: int = 100, correlation_id: str | None = None):
    rows = recent_events(db, limit=limit, correlation_id=correlation_id)
    return {
        "entries": [
            {
                "id": r.id,
                "event": r.event,
                "actor": r.actor,
                "component": r.component,
                "detail": r.detail,
                "correlation_id": r.correlation_id,
                "created_at": r.created_at.isoformat() + "Z",
            }
            for r in rows
        ]
    }


@router.get("/audit/trail/{correlation_id}")
def audit_trail(correlation_id: str, user: Auditor, db: DbDep):
    """Reconstruct the full chronological trail of one logical action."""
    from security.audit import trail

    rows = trail(db, correlation_id)
    return {
        "correlation_id": correlation_id,
        "count": len(rows),
        "entries": [
            {
                "id": r.id,
                "event": r.event,
                "actor": r.actor,
                "component": r.component,
                "detail": r.detail,
                "created_at": r.created_at.isoformat() + "Z",
            }
            for r in rows
        ],
    }


@router.get("/tools")
def tool_risk_catalog(user: SecurityReader, db: DbDep):
    """Every registered tool with its real risk, reversibility and gating.

    This is metadata read from the live registry — not a hand-written list.
    """
    from core.agent.registry import registry, tool_requires_approval

    specs = sorted(registry.all_specs(), key=lambda s: s.name)
    return {
        "tools": [
            {
                "name": s.name,
                "description": s.description,
                "permission_level": s.permission_level.value,
                "required_permission": str(s.required_permission),
                "risk_level": s.risk_level,
                "irreversible": s.irreversible,
                "requires_approval": tool_requires_approval(s),
            }
            for s in specs
        ],
        "count": len(specs),
    }


@router.get("/policy")
def autonomy_policy(user: SecurityReader, db: DbDep):
    """The autonomy boundaries the engine actually enforces."""
    from config.settings import settings

    return {
        "approval_required_when": (
            "A tool is gated above SAFE, or its risk is HIGH/CRITICAL, or the "
            "action is irreversible."
        ),
        "irreversible_actions_are_never_auto_retried": True,
        "unknown_outcomes_are_never_blindly_repeated": True,
        "max_plan_steps": settings.mission_max_steps,
        "step_max_attempts": settings.mission_step_max_attempts,
        "high_risk_repairs_require_approval": True,
        "security_is_never_weakened_for_availability": True,
        "secrets_are_never_written_to_logs": True,
    }


@router.get("/escalation")
def escalation(user: CurrentUser):
    """Configured cyber contacts only — never a guessed 'nearest' authority (§15.6)."""
    contacts = monitoring.escalation_contacts()
    return {
        "contacts": [
            {
                "label": c.label,
                "channel": c.channel,
                "value": c.value,
                "configured": c.configured,
            }
            for c in contacts
        ],
        "policy": (
            "ALYO never contacts external services automatically. Any external "
            "report requires your confirmation unless this deployment has an "
            "administrator-approved automated incident-response channel."
        ),
    }


@router.get("/emergency")
def emergency_config(user: CurrentUser):
    """Physical-emergency workflow — separate from cyber incidents (§15.7)."""
    return {
        "kind": "PHYSICAL_EMERGENCY",
        "region": settings_emergency_region(),
        "emergency_number": settings_emergency_number(),
        "note": (
            "For a genuine physical emergency, call your local emergency service "
            "directly. ALYO does not place emergency calls: no telephony "
            "integration is configured on this deployment."
        ),
        "cyber_vs_physical": (
            "Suspicious software activity is a CYBERSECURITY INCIDENT — it does "
            "not by itself warrant contacting police."
        ),
        "auto_contact_enabled": False,
    }


def settings_emergency_region() -> str:
    from config.settings import settings

    return settings.emergency_region


def settings_emergency_number() -> str:
    from config.settings import settings

    return settings.emergency_number


def _event(e: SecurityEvent) -> dict:
    """Spec §15.13 shape: what happened, why, severity, what ALYO did, next."""
    return {
        "id": e.id,
        "created_at": e.created_at.isoformat() + "Z",
        "component": e.component,
        "severity": e.severity,
        "error_type": e.error_type,
        "what_happened": e.description,
        "why_flagged": e.possible_cause,
        "seriousness": f"{e.severity} (risk score {e.risk_score}/100 — {monitoring.risk_band(e.risk_score)})",
        "what_alyo_did": _alyo_action(e),
        "what_you_can_do": e.recommended_action,
        "risk_score": e.risk_score,
        "risk_band": monitoring.risk_band(e.risk_score),
        "status": e.status,
        "evidence": _load(e.evidence),
        "resolution_notes": e.resolution_notes,
        "resolved_at": e.resolved_at.isoformat() + "Z" if e.resolved_at else None,
    }


def _alyo_action(e: SecurityEvent) -> str:
    if e.risk_score >= 60:
        return (
            "Recorded the event, preserved its evidence in the security log, "
            "reopened this as an incident and revoked affected sessions where "
            "applicable. No data was deleted."
        )
    return "Recorded the event in the security log for review."


def _incident(i: Incident) -> dict:
    return {
        "id": i.id,
        "title": i.title,
        "state": i.state,
        "severity": i.severity,
        "risk_score": i.risk_score,
        "component": i.component,
        "created_at": i.created_at.isoformat() + "Z",
        "updated_at": i.updated_at.isoformat() + "Z",
        "resolved_at": i.resolved_at.isoformat() + "Z" if i.resolved_at else None,
        "timeline": _load(i.timeline_json),
        "evidence_refs": _load(i.evidence_json),
        "recommended_action": i.recommended_action,
    }


def _load(raw: str):
    import json

    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
