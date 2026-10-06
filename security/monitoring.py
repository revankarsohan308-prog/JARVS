"""ALYO security monitoring: bug detector + threat detection + incidents.

Everything here is driven by real observed events — callers pass actual
evidence (failed logins, exceptions, permission denials). The module never
invents findings to make a dashboard look busy (spec §40).

Terminology rule (spec §15.3 / 15.13): findings are described as *potential*
incidents with the evidence that flagged them, never as confirmed attacks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from database.models import Incident, PendingConfirmation, SecurityEvent, SessionToken, User
from security.audit import audit
from timeutil import utcnow

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
INCIDENT_STATES = ("DETECTED", "INVESTIGATING", "CONTAINED", "RESOLVED", "FALSE_POSITIVE", "ESCALATED")


def risk_band(score: int) -> str:
    """0-29 LOW | 30-59 MEDIUM | 60-79 HIGH | 80-100 CRITICAL (spec §15.3)."""
    if score >= 80:
        return "CRITICAL"
    if score >= 60:
        return "HIGH"
    if score >= 30:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Bug detector (spec §15.2)
# ---------------------------------------------------------------------------

# (error_type, severity, possible_cause, recommended_action) templates.
_BUG_PROFILES: dict[str, tuple[str, str, str, str]] = {
    "unhandled_exception": (
        "Unhandled exception",
        "HIGH",
        "An uncaught error escaped a request handler.",
        "Inspect the traceback in logs/alyo.log and add handling for this path.",
    ),
    "api_failure": (
        "API failure",
        "MEDIUM",
        "An upstream or internal API call returned an error.",
        "Check the target service status and retry behaviour for this endpoint.",
    ),
    "database_error": (
        "Database error",
        "HIGH",
        "A database operation failed; data may not have been persisted.",
        "Check database connectivity and disk space, then verify the last write.",
    ),
    "repeated_failure": (
        "Repeated failed operations",
        "MEDIUM",
        "The same operation failed multiple times in a short window.",
        "Review the failing operation's inputs and dependencies.",
    ),
    "resource_problem": (
        "Resource problem",
        "HIGH",
        "CPU, memory or disk pressure is degrading the application.",
        "Free disk space or reduce load; check System Health for current values.",
    ),
    "permission_failure": (
        "Permission failure",
        "MEDIUM",
        "A principal attempted an action their role does not grant.",
        "Confirm this was legitimate; adjust the role's permissions only if intended.",
    ),
    "config_error": (
        "Configuration error",
        "MEDIUM",
        "A required configuration value is missing or invalid.",
        "Compare .env with .env.example and restart the service.",
    ),
    "integration_failure": (
        "Broken integration",
        "MEDIUM",
        "A configured external integration did not respond correctly.",
        "Verify credentials/provider status for this integration.",
    ),
    "auth_failure_burst": (
        "Repeated authentication failures",
        "HIGH",
        "Multiple failed logins for one account in a short window.",
        "Verify the account owner; reset the password if compromise is suspected.",
    ),
    "data_corruption_indicator": (
        "Data corruption indicator",
        "CRITICAL",
        "A record failed structural validation.",
        "Restore affected data from backup and investigate the write path.",
    ),
}


def record_bug_event(
    db: Session,
    component: str,
    error_type: str,
    description: str,
    *,
    user_id: int | None = None,
    evidence: list[str] | None = None,
) -> SecurityEvent:
    """Create a bug-detector event with full metadata (spec §15.2 fields)."""
    if error_type in _BUG_PROFILES:
        _label, severity, cause, action = _BUG_PROFILES[error_type]
    else:
        severity, cause, action = "MEDIUM", "Unknown cause.", "Review component logs."
    risk = {"INFO": 5, "LOW": 15, "MEDIUM": 40, "HIGH": 65, "CRITICAL": 90}[severity]
    event = SecurityEvent(
        component=component,
        severity=severity,
        error_type=error_type,
        description=description[:2000],
        possible_cause=cause,
        recommended_action=action,
        risk_score=risk,
        user_id=user_id,
        evidence=json.dumps(evidence or []),
    )
    db.add(event)
    db.commit()
    audit(db, "security_event_recorded", f"type={error_type} severity={severity} component={component}",
          component="security")
    return event


# ---------------------------------------------------------------------------
# Threat detection (spec §15.3)
# ---------------------------------------------------------------------------

# A finding is only raised when the caller supplies real observed counts.
_THREAT_PROFILES: dict[str, tuple[str, str, str, str]] = {
    "brute_force_login": (
        "Repeated failed logins",
        "Multiple failed login attempts for one account match brute-force-like behaviour.",
        "Verify the account owner. Reset credentials if compromise is suspected.",
    ),
    "permission_bypass_attempt": (
        "Repeated permission-denied responses",
        "One principal repeatedly attempted actions their role does not grant.",
        "Confirm the attempts were legitimate before changing any role.",
    ),
    "config_tamper": (
        "Configuration/file tamper indicator",
        "A protected file's checksum no longer matches the value recorded at startup.",
        "Restore the file from a trusted copy and review who had write access.",
    ),
    "unexpected_tool_execution": (
        "Unexpected tool execution",
        "A restricted tool ran outside its expected permission boundary.",
        "Review the audit entry for this tool call and the acting principal.",
    ),
    "suspicious_session_activity": (
        "Suspicious session activity",
        "Session usage patterns deviate from the recorded baseline for this account.",
        "Review active sessions and revoke any that are unrecognized.",
    ),
}


def record_threat_event(
    db: Session,
    threat_type: str,
    component: str,
    risk_score: int,
    evidence: list[str],
    *,
    user_id: int | None = None,
) -> SecurityEvent | None:
    """Score and persist a potential security incident.

    ``risk_score`` must be derived from the evidence the caller actually
    observed; ``evidence`` is stored verbatim so the alert can explain itself
    (spec §15.13).
    """
    risk_score = max(0, min(100, int(risk_score)))
    profile = _THREAT_PROFILES.get(threat_type)
    if profile is None:
        raise ValueError(f"Unknown threat type: {threat_type}")
    title, cause, action = profile
    severity = risk_band(risk_score)
    if risk_score >= 60:
        severity = "HIGH" if risk_score < 80 else "CRITICAL"
    description = f"Potential security incident detected: {title}. {cause} Evidence: {'; '.join(evidence[:5])}"
    event = SecurityEvent(
        component=component,
        severity=severity,
        error_type=threat_type,
        description=description[:2000],
        possible_cause=cause,
        recommended_action=action,
        risk_score=risk_score,
        user_id=user_id,
        evidence=json.dumps(evidence[:20]),
    )
    db.add(event)
    db.commit()
    audit(db, "threat_detected", f"type={threat_type} risk={risk_score}({severity}) component={component}",
          component="security")
    if risk_score >= 60 and settings.auto_containment_enabled:
        contain(db, event)
    return event


# ---------------------------------------------------------------------------
# Automatic protection (spec §15.4)
# ---------------------------------------------------------------------------

def contain(db: Session, event: SecurityEvent) -> Incident:
    """High-risk containment: revoke this user's sessions, open an incident.

    Deliberately conservative — evidence is preserved, nothing is deleted, and
    destructive follow-ups always need a human (spec §15.4).
    """
    actions: list[str] = []
    if event.user_id:
        rows = db.scalars(
            select(SessionToken).where(SessionToken.user_id == event.user_id, SessionToken.revoked.is_(False))
        ).all()
        for row in rows:
            row.revoked = True
        if rows:
            actions.append(f"Revoked {len(rows)} active session token(s) for the affected user.")
    timeline = [
        {
            "at": utcnow().isoformat() + "Z",
            "action": "detected",
            "detail": f"{event.error_type} on {event.component} (risk {event.risk_score})",
        }
    ]
    if actions:
        timeline.append({"at": utcnow().isoformat() + "Z", "action": "contained", "detail": "; ".join(actions)})
    incident = Incident(
        title=f"Potential security incident: {event.error_type} on {event.component}",
        state="CONTAINED" if actions else "DETECTED",
        severity=event.severity,
        risk_score=event.risk_score,
        component=event.component,
        timeline_json=json.dumps(timeline),
        evidence_json=json.dumps([f"security_event:{event.id}"]),
        recommended_action=event.recommended_action,
    )
    db.add(incident)
    db.commit()
    audit(db, "incident_opened", f"event={event.id} state={incident.state}", component="security")
    return incident


# ---------------------------------------------------------------------------
# Incident lifecycle (spec §15.5)
# ---------------------------------------------------------------------------

def open_incident(
    db: Session,
    title: str,
    severity: str,
    risk_score: int,
    component: str,
    evidence: list[str],
    recommended_action: str = "",
) -> Incident:
    timeline = [{"at": utcnow().isoformat() + "Z", "action": "detected", "detail": title}]
    incident = Incident(
        title=title[:255],
        severity=severity,
        risk_score=risk_score,
        component=component,
        timeline_json=json.dumps(timeline),
        evidence_json=json.dumps(evidence[:20]),
        recommended_action=recommended_action,
    )
    db.add(incident)
    db.commit()
    audit(db, "incident_opened", f"title={title[:80]}", component="security")
    return incident


def transition_incident(
    db: Session, incident_id: str, new_state: str, actor: str, note: str = ""
) -> Incident:
    if new_state not in INCIDENT_STATES:
        raise ValueError(f"Unknown incident state: {new_state}")
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise LookupError("Incident not found.")
    timeline = json.loads(incident.timeline_json or "[]")
    timeline.append(
        {"at": utcnow().isoformat() + "Z", "action": new_state.lower(), "detail": note or "-", "actor": actor}
    )
    incident.timeline_json = json.dumps(timeline)
    incident.state = new_state
    incident.updated_at = utcnow()
    if new_state in ("RESOLVED", "FALSE_POSITIVE"):
        incident.resolved_at = utcnow()
    db.commit()
    audit(db, "incident_state_changed", f"incident={incident_id} state={new_state} note={note[:120]}",
          actor=actor, component="security")
    return incident


def security_status(db: Session) -> dict[str, object]:
    """Aggregated live security status for the dashboard (spec §15.9)."""
    open_events = db.scalars(select(SecurityEvent).where(SecurityEvent.status == "OPEN")).all()
    active_incidents = db.scalars(
        select(Incident).where(Incident.state.notin_(("RESOLVED", "FALSE_POSITIVE")))
    ).all()
    by_severity = {s: 0 for s in SEVERITIES}
    max_risk = 0
    for event in open_events:
        by_severity[event.severity] = by_severity.get(event.severity, 0) + 1
        max_risk = max(max_risk, event.risk_score)
    if any(e.severity in ("HIGH", "CRITICAL") for e in open_events):
        threat_level = "ELEVATED"
    elif any(e.severity == "MEDIUM" for e in open_events):
        threat_level = "GUARDED"
    else:
        threat_level = "NORMAL"
    return {
        "status": threat_level,
        "threat_level": threat_level,
        "overall_risk_score": max_risk,
        "risk_band": risk_band(max_risk) if open_events else "LOW",
        "open_event_count": len(open_events),
        "events_by_severity": by_severity,
        "active_incidents": [
            {
                "id": i.id,
                "title": i.title,
                "state": i.state,
                "severity": i.severity,
                "risk_score": i.risk_score,
                "component": i.component,
                "created_at": i.created_at.isoformat() + "Z",
            }
            for i in active_incidents
        ],
        "auto_containment_enabled": settings.auto_containment_enabled,
        "checked_at": utcnow().isoformat() + "Z",
    }


@dataclass
class EscalationContact:
    label: str
    channel: str
    value: str
    configured: bool


def escalation_contacts() -> list[EscalationContact]:
    """Configured cyber escalation contacts (spec §15.6).

    No location guessing: only explicitly configured contacts are returned.
    """
    contacts = [
        EscalationContact(
            "College cybersecurity contact", "configured",
            settings.college_security_contact, bool(settings.college_security_contact),
        ),
        EscalationContact(
            "IT / security administrator", "configured",
            settings.it_security_contact, bool(settings.it_security_contact),
        ),
        EscalationContact(
            "National cybercrime reporting (India)", "web",
            "https://cybercrime.gov.in", True,
        ),
        EscalationContact(
            "CERT-In (Indian CERT)", "web",
            "https://www.cert-in.org.in", True,
        ),
    ]
    return contacts


def failed_login_stats(db: Session, window_minutes: int = 15) -> dict[str, object]:
    """Accounts with open brute-force bursts inside the window (real counts)."""
    from datetime import timedelta

    from sqlalchemy import func

    cutoff = utcnow() - timedelta(minutes=window_minutes)
    rows = db.execute(
        select(User.username, func.count(SecurityEvent.id))
        .join(SecurityEvent, SecurityEvent.user_id == User.id)
        .where(
            SecurityEvent.error_type == "auth_failure_burst",
            SecurityEvent.status == "OPEN",
            SecurityEvent.created_at >= cutoff,
        )
        .group_by(User.username)
    ).all()
    return {
        "window_minutes": window_minutes,
        "accounts_with_bursts": [{"username": u, "count": c} for u, c in rows],
    }


def expire_pending_confirmations(db: Session, max_age_minutes: int = 30) -> int:
    """Fail-safe: stale confirmation requests expire instead of lingering."""
    from datetime import timedelta

    cutoff = utcnow() - timedelta(minutes=max_age_minutes)
    rows = db.scalars(
        select(PendingConfirmation).where(
            PendingConfirmation.resolved.is_(False),
            PendingConfirmation.created_at < cutoff,
        )
    ).all()
    for row in rows:
        row.resolved = True
        row.resolution = "expired"
    if rows:
        db.commit()
        audit(db, "confirmations_expired", f"count={len(rows)}", component="security")
    return len(rows)
