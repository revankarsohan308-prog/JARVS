"""Role-based access control.

Roles come from the ALYO spec (section 6). Permissions are explicit strings;
a role grants a set of permissions and nothing more (least privilege).
The permission check is a pure function so every layer — API, tools, brain —
answers it the same way.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    SUPER_ADMIN = "SUPER_ADMIN"
    COLLEGE_ADMIN = "COLLEGE_ADMIN"
    HOD = "HOD"
    FACULTY = "FACULTY"
    STAFF = "STAFF"
    STUDENT = "STUDENT"
    GUEST = "GUEST"


class Permission(StrEnum):
    CHAT = "chat:use"
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    TASK_READ = "task:read"
    TASK_WRITE = "task:write"
    TASK_DELETE = "task:delete"
    MISSION_READ = "mission:read"
    MISSION_WRITE = "mission:write"
    SYSTEM_READ = "system:read"
    AUDIT_READ = "audit:read"
    SECURITY_READ = "security:read"
    SECURITY_MANAGE = "security:manage"
    RELEASE_READ = "release:read"
    RELEASE_RUN = "release:run"
    USER_MANAGE = "user:manage"
    TOOL_CONFIRM = "tool:confirm"
    TOOL_RESTRICTED = "tool:restricted"


# Every role gets CHAT / read basics; escalation is explicit, never implicit.
_BASE = {
    Permission.CHAT,
    Permission.MEMORY_READ,
    Permission.MEMORY_WRITE,
    Permission.TASK_READ,
    Permission.TASK_WRITE,
    Permission.MISSION_READ,
    Permission.MISSION_WRITE,
    Permission.SYSTEM_READ,
}

_ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.GUEST: set(),
    Role.STUDENT: _BASE,
    Role.STAFF: _BASE | {Permission.TASK_DELETE, Permission.TOOL_CONFIRM},
    Role.FACULTY: _BASE
    | {Permission.TASK_DELETE, Permission.TOOL_CONFIRM, Permission.AUDIT_READ},
    Role.HOD: _BASE
    | {
        Permission.TASK_DELETE,
        Permission.TOOL_CONFIRM,
        Permission.AUDIT_READ,
        Permission.SECURITY_READ,
        Permission.TOOL_RESTRICTED,
    },
    Role.COLLEGE_ADMIN: _BASE
    | {
        Permission.TASK_DELETE,
        Permission.TOOL_CONFIRM,
        Permission.AUDIT_READ,
        Permission.SECURITY_READ,
        Permission.SECURITY_MANAGE,
        Permission.RELEASE_READ,
        Permission.RELEASE_RUN,
        Permission.USER_MANAGE,
        Permission.TOOL_RESTRICTED,
    },
    Role.SUPER_ADMIN: set(Permission),
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.SUPER_ADMIN: "Platform configuration, security, organization management.",
    Role.COLLEGE_ADMIN: "Institutional administration, users, security operations.",
    Role.HOD: "Department information, staff workflows, reports.",
    Role.FACULTY: "Classes, teaching materials, assignments, academic workflows.",
    Role.STAFF: "Operational tasks and department workflows.",
    Role.STUDENT: "Personal tasks, timetable, study assistance, notices.",
    Role.GUEST: "Read-only guest access; no personal data operations.",
}


def permissions_for(role: str | Role) -> set[Permission]:
    try:
        return set(_ROLE_PERMISSIONS[Role(role)])
    except (ValueError, KeyError):
        return set()


def has_permission(role: str | Role, permission: Permission | str) -> bool:
    try:
        perm = Permission(permission)
    except ValueError:
        return False
    return perm in permissions_for(role)


def describe_roles() -> list[dict[str, object]]:
    return [
        {
            "role": role.value,
            "description": ROLE_DESCRIPTIONS[role],
            "permissions": sorted(p.value for p in permissions_for(role)),
        }
        for role in Role
    ]
