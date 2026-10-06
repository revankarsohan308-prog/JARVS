"""RBAC: role boundaries, permission denial, evidence of denials."""

from __future__ import annotations

from core.permissions.rbac import (
    Permission,
    Role,
    describe_roles,
    has_permission,
    permissions_for,
)
from tests.conftest import auth_headers


def test_super_admin_has_every_permission():
    assert permissions_for(Role.SUPER_ADMIN) == set(Permission)


def test_guest_has_no_permissions():
    assert permissions_for(Role.GUEST) == set()


def test_student_cannot_read_audit_or_manage_users():
    assert not has_permission(Role.STUDENT, Permission.AUDIT_READ)
    assert not has_permission(Role.STUDENT, Permission.USER_MANAGE)
    assert not has_permission(Role.STUDENT, Permission.SECURITY_MANAGE)


def test_least_privilege_escalation_is_monotonic():
    order = [
        Role.GUEST,
        Role.STUDENT,
        Role.STAFF,
        Role.FACULTY,
        Role.HOD,
        Role.COLLEGE_ADMIN,
        Role.SUPER_ADMIN,
    ]
    previous: set = set()
    for role in order:
        current = permissions_for(role)
        assert previous <= current, f"{role.value} lost a permission vs previous role"
        previous = current


def test_unknown_role_and_permission_fail_closed():
    assert permissions_for("MADE_UP_ROLE") == set()
    assert not has_permission(Role.SUPER_ADMIN, "not:a:permission")


def test_describe_roles_covers_all():
    described = describe_roles()
    assert len(described) == len(list(Role))
    assert all(r["permissions"] for r in described if r["role"] != "GUEST")


def test_student_denied_audit_endpoint(client, admin_session, student_session):
    resp = client.get(
        "/api/v1/security/audit", headers=auth_headers(student_session["token"])
    )
    assert resp.status_code == 403
    assert "STUDENT" in resp.json()["detail"]


def test_student_denied_role_change(client, admin_session, student_session, db):
    resp = client.post(
        "/api/v1/auth/users/1/role",
        headers=auth_headers(student_session["token"]),
        json={"role": "SUPER_ADMIN"},
    )
    assert resp.status_code == 403


def test_admin_can_change_role_and_it_is_audited(client, admin_session, student_session, db):
    # student user id: admin is id 1 (bootstrap), sohan is id 2
    resp = client.post(
        "/api/v1/auth/users/2/role",
        headers=auth_headers(admin_session["token"]),
        json={"role": "FACULTY"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "FACULTY"

    from sqlalchemy import select

    from database.models import AuditLog

    entry = db.scalar(select(AuditLog).where(AuditLog.event == "role_changed"))
    assert entry is not None
    assert "STUDENT->FACULTY" in entry.detail


def test_denied_requests_are_recorded_as_evidence(client, admin_session, student_session):
    headers = auth_headers(student_session["token"])
    for _ in range(5):
        client.get("/api/v1/security/audit", headers=headers)

    # The denials themselves are audited evidence...
    entries = client.get(
        "/api/v1/security/audit",
        headers=auth_headers(admin_session["token"]),
    ).json()["entries"]
    denials = [e for e in entries if e["event"] == "permission_denied"]
    assert len(denials) >= 5

    # ...and after the 5th denial a scored threat event exists.
    events = client.get(
        "/api/v1/security/events",
        headers=auth_headers(admin_session["token"]),
    ).json()["events"]
    threat = [e for e in events if e["error_type"] == "permission_bypass_attempt"]
    assert threat, "repeated permission denials must raise a scored event"
    assert threat[0]["risk_score"] >= 30
