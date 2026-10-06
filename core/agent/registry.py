"""Tool registry: the single choke point for every capability ALYO exposes.

Every tool declares:
- a permission level (SAFE / CONFIRMATION_REQUIRED / RESTRICTED / USER_ENABLED),
- the RBAC permission it needs,
- an availability flag with a human-readable reason when unavailable.

Execution always runs: permission check -> confirmation gate -> handler ->
verification. Handlers return a ``verified`` boolean so the brain can never
report success for something that did not actually happen (spec §49).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from core.permissions.rbac import Permission, has_permission
from database.models import PendingConfirmation, new_id
from security.audit import audit


class PermissionLevel(StrEnum):
    SAFE = "SAFE"  # runs immediately
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"  # asks a human first
    RESTRICTED = "RESTRICTED"  # off by default AND asks first
    USER_ENABLED = "USER_ENABLED"  # off until the user enables it


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema
    handler: Callable[..., dict[str, Any]]
    permission_level: PermissionLevel = PermissionLevel.SAFE
    required_permission: Permission = Permission.CHAT
    enabled: bool = True
    unavailable_reason: str = ""
    settings_flag: str = ""  # settings attribute that must be truthy, if any
    requires_tool_confirm: bool = False  # RESTRICTED needs tool:restricted too


@dataclass
class ExecutionResult:
    ok: bool
    verified: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    needs_confirmation: bool = False
    confirmation_id: str = ""
    denied_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "data": self.data,
            "message": self.message,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_id": self.confirmation_id,
            "denied_reason": self.denied_reason,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def openai_tools(self) -> list[dict[str, Any]]:
        """Tool list shaped for OpenAI-compatible function calling."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name.replace(".", "_"),
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
            if t.enabled
        ]

    def resolve_name(self, name: str) -> str | None:
        """Map ``foo_bar`` (LLM style) or ``foo.bar`` to the canonical name."""
        if name in self._tools:
            return name
        dotted = name.replace("_", ".")
        if dotted in self._tools:
            return dotted
        return None

    # ------------------------------------------------------------------
    def availability(self, spec: ToolSpec, settings_obj: Any) -> tuple[bool, str]:
        if not spec.enabled:
            return False, spec.unavailable_reason or "This tool is disabled."
        if spec.settings_flag and not getattr(settings_obj, spec.settings_flag, False):
            return False, (
                f"Not configured. Set {spec.settings_flag} in .env to enable it."
            )
        return True, ""

    def check_permission(
        self, spec: ToolSpec, role: str, settings_obj: Any, confirmed: bool = False
    ) -> tuple[bool, bool, str]:
        """Returns (allowed, needs_confirmation, reason)."""
        available, reason = self.availability(spec, settings_obj)
        if not available:
            return False, False, reason
        if not has_permission(role, spec.required_permission):
            return False, False, (
                f"Your role ({role}) does not grant '{spec.required_permission.value}'."
            )
        if spec.requires_tool_confirm and not has_permission(role, Permission.TOOL_RESTRICTED):
            return False, False, f"Your role ({role}) cannot run restricted tools."
        if spec.permission_level in (
            PermissionLevel.CONFIRMATION_REQUIRED,
            PermissionLevel.RESTRICTED,
            PermissionLevel.USER_ENABLED,
        ) and not confirmed:
            return False, True, ""
        return True, False, ""


registry = ToolRegistry()


# ----------------------------------------------------------------------
# Confirmation flow
# ----------------------------------------------------------------------

def create_confirmation(
    db: Session, user_id: int, tool_name: str, args: dict[str, Any], summary: str
) -> str:
    row = PendingConfirmation(
        id=new_id(),
        user_id=user_id,
        tool_name=tool_name,
        payload_json=json.dumps(args),
        summary=summary[:512],
    )
    db.add(row)
    db.commit()
    audit(db, "confirmation_requested", f"tool={tool_name} summary={summary[:120]}",
          actor=f"user_id:{user_id}", component="agent")
    return row.id


def resolve_confirmation(
    db: Session, user_id: int, confirmation_id: str, approve: bool, actor: str
) -> tuple[bool, str, dict[str, Any], str]:
    """Returns (found, tool_name, args, resolution)."""
    row = db.get(PendingConfirmation, confirmation_id)
    if row is None or row.user_id != user_id:
        return False, "", {}, ""
    if row.resolved:
        return True, row.tool_name, {}, row.resolution
    row.resolved = True
    row.resolution = "approved" if approve else "denied"
    db.commit()
    audit(
        db,
        "confirmation_accepted" if approve else "confirmation_denied",
        f"tool={row.tool_name} id={confirmation_id}",
        actor=actor,
        component="agent",
    )
    args = json.loads(row.payload_json) if approve else {}
    return True, row.tool_name, args, row.resolution


def execute_tool(
    db: Session,
    spec: ToolSpec,
    args: dict[str, Any],
    *,
    user_id: int,
    role: str,
    settings_obj: Any,
    actor: str,
    confirmed: bool = False,
) -> ExecutionResult:
    """The one path through which a tool may run."""
    allowed, needs_confirmation, reason = registry.check_permission(
        spec, role, settings_obj, confirmed=confirmed
    )
    if not allowed:
        if needs_confirmation:
            confirmation_id = create_confirmation(
                db, user_id, spec.name, args, _summarize(spec, args)
            )
            audit(db, "tool_awaiting_confirmation", f"tool={spec.name}", actor=actor, component="agent")
            return ExecutionResult(
                ok=False,
                verified=False,
                needs_confirmation=True,
                confirmation_id=confirmation_id,
                message=_summarize(spec, args),
            )
        audit(db, "tool_permission_denied", f"tool={spec.name} reason={reason}",
              actor=actor, component="agent")
        return ExecutionResult(ok=False, verified=False, denied_reason=reason, message=reason)

    try:
        raw = spec.handler(db, args, user_id=user_id, role=role)
    except Exception as exc:  # handlers raise user-facing errors
        db.rollback()
        audit(db, "tool_error", f"tool={spec.name} error={type(exc).__name__}: {exc}",
              actor=actor, component="agent")
        return ExecutionResult(ok=False, verified=False, message=str(exc))

    if not isinstance(raw, dict):
        raw = {"result": raw}
    verified = bool(raw.pop("verified", True))
    ok = bool(raw.pop("ok", True)) and verified
    audit(
        db,
        "tool_executed",
        f"tool={spec.name} ok={ok} verified={verified}",
        actor=actor,
        component="agent",
    )
    return ExecutionResult(ok=ok, verified=verified, data=raw, message=str(raw.get("message", "")))


def _summarize(spec: ToolSpec, args: dict[str, Any]) -> str:
    readable = ", ".join(f"{k}={v}" for k, v in list(args.items())[:5])
    return f"Confirm: {spec.name}({readable})"
