"""FastAPI dependencies: authentication, RBAC guards, rate limiting."""

from __future__ import annotations

from typing import Annotated, Callable

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from core.identity import service as identity
from core.permissions.rbac import Permission, has_permission
from database.models import User
from database.session import get_db

DbDep = Annotated[Session, Depends(get_db)]


def _extract_token(request: Request, authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    # Single-token header kept for device/gateway clients (spec §20).
    return request.headers.get("x-alyo-token", "").strip()


def current_user(
    request: Request,
    db: DbDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    token = _extract_token(request, authorization)
    user = identity.resolve_session(db, token)
    if user is None:
        raise HTTPException(status_code=401, detail="Missing or invalid session token.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_permission(permission: Permission) -> Callable[..., User]:
    """Route guard: 403 outside the role's grants, with an audited trail.

    Denied attempts are recorded as real evidence for the threat detector —
    repeated denials from one principal raise a scored security event.
    """

    def guard(request: Request, user: CurrentUser, db: DbDep) -> User:
        if not has_permission(user.role, permission):
            audit_denial(request, db, user, permission)
            raise HTTPException(
                status_code=403,
                detail=f"Your role ({user.role}) does not grant '{permission.value}'.",
            )
        return user

    return guard


# Denied-permission counter, keyed by user for threat scoring.
_denial_windows: dict[int, list[float]] = {}
_DENIAL_WINDOW_SECONDS = 300
_DENIAL_THRESHOLD = 5


def audit_denial(request: Request, db: Session, user: User, permission: Permission) -> None:
    import time

    from security.audit import audit
    from security.monitoring import record_threat_event

    audit(
        db,
        "permission_denied",
        f"permission={permission.value} path={request.url.path}",
        actor=f"user:{user.username}",
        component="api",
    )
    now = time.monotonic()
    window = _denial_windows.setdefault(user.id, [])
    window[:] = [t for t in window if now - t < _DENIAL_WINDOW_SECONDS]
    window.append(now)
    if len(window) == _DENIAL_THRESHOLD:
        record_threat_event(
            db,
            "permission_bypass_attempt",
            component="api",
            risk_score=45,
            evidence=[
                f"{len(window)} permission-denied responses in {_DENIAL_WINDOW_SECONDS}s",
                f"latest permission: {permission.value}",
                f"path: {request.url.path}",
            ],
            user_id=user.id,
        )


def reset_denial_windows() -> None:
    _denial_windows.clear()
