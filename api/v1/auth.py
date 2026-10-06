"""Authentication routes: register, login, sessions, current identity.

Login is rate-limited and failed attempts feed the threat detector
(spec §15.1 / §15.3). Tokens are returned exactly once — only digests persist.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbDep, require_permission
from config.settings import settings
from core.identity import service as identity
from core.identity.service import IdentityError
from core.permissions.rbac import Permission, Role, describe_roles
from database.models import User
from security.monitoring import record_threat_event
from security.ratelimit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])

AdminUser = Annotated[User, Depends(require_permission(Permission.USER_MANAGE))]


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    display_name: str = Field(default="", max_length=128)
    role: str = Field(default="STUDENT", max_length=32)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    client_label: str = Field(default="", max_length=128)


class RoleChangeIn(BaseModel):
    role: str = Field(max_length=32)


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"login:{host}:{username.lower()}"


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: DbDep, request: Request):
    """Open registration.

    The requested role is honoured only for the very first account (bootstrap
    SUPER_ADMIN — otherwise the deployment could never be administered).
    Every later registration is created as STUDENT: assigning roles requires
    an authenticated user:manage administrator (least privilege, spec §15.11).
    """
    if not settings.allow_open_registration:
        raise HTTPException(status_code=403, detail="Registration is closed on this deployment.")
    allowed, retry = limiter.check(
        f"register:{request.client.host if request.client else 'unknown'}", 10, 300
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Retry in {retry}s.")
    from sqlalchemy import func, select

    from database.models import User as UserModel

    user_count = db.scalar(select(func.count(UserModel.id))) or 0
    # First account on a fresh deployment becomes SUPER_ADMIN so the system
    # is administrable from the start; every later signup is STUDENT.
    requested_role = Role.SUPER_ADMIN.value if user_count == 0 else Role.STUDENT.value
    try:
        user = identity.register_user(
            db,
            body.username,
            body.password,
            role=requested_role,
            email=body.email,
            display_name=body.display_name,
            actor=f"user:{body.username}",
        )
    except IdentityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token, expires = identity.create_session(db, user, body.username)
    return {
        "ok": True,
        "token": token,
        "expires_at": expires.isoformat() + "Z",
        "user": _public_user(user),
        "bootstrap_admin": user_count == 0,
    }


@router.post("/login")
def login(body: LoginIn, db: DbDep, request: Request):
    key = _client_key(request, body.username)
    allowed, retry = limiter.check(key, settings.login_rate_limit, settings.login_rate_window)
    if not allowed:
        # Real evidence: count these failures for the brute-force detector.
        failures = limiter.failures(key, settings.login_rate_window)
        from database.models import User as UserModel

        target = db.query(UserModel).filter(UserModel.username == body.username.strip()).first()
        record_threat_event(
            db,
            "brute_force_login",
            component="auth",
            risk_score=min(30 + failures * 8, 85),
            evidence=[
                f"{failures} failed login attempts within {settings.login_rate_window}s",
                f"source {request.client.host if request.client else 'unknown'}",
                f"rate limit {settings.login_rate_limit}/{settings.login_rate_window}s exceeded",
            ],
            user_id=target.id if target else None,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts. Try again in {retry}s.",
        )
    try:
        user = identity.authenticate(db, body.username, body.password)
    except IdentityError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    limiter.reset(key)  # successful login clears the failure window
    token, expires = identity.create_session(db, user, body.client_label or body.username)
    return {
        "ok": True,
        "token": token,
        "expires_at": expires.isoformat() + "Z",
        "user": _public_user(user),
    }


@router.post("/logout")
def logout(
    db: DbDep,
    user: CurrentUser,
    authorization: Annotated[str | None, Header()] = None,
):
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if token:
        identity.revoke_session(db, token, actor=f"user:{user.username}")
    return {"ok": True, "detail": "Session revoked."}


@router.get("/me")
def me(user: CurrentUser, db: DbDep):
    from core.memory import service as memory
    from core.permissions.rbac import permissions_for
    from core.tasks import service as tasks

    return {
        "user": _public_user(user),
        "permissions": sorted(p.value for p in permissions_for(user.role)),
        "stats": {
            "memories": memory.stats(db, user.id)["total"],
            "pending_tasks": len(tasks.list_tasks(db, user.id)),
        },
    }


@router.get("/sessions")
def list_sessions(user: CurrentUser, db: DbDep):
    rows = identity.active_sessions(db, user.id)
    return {
        "sessions": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() + "Z",
                "expires_at": r.expires_at.isoformat() + "Z",
                "client_label": r.client_label,
            }
            for r in rows
        ]
    }


@router.delete("/sessions/{session_id}")
def revoke_session(session_id: int, user: CurrentUser, db: DbDep):
    from database.models import SessionToken
    from sqlalchemy import select

    row = db.scalar(
        select(SessionToken).where(SessionToken.id == session_id, SessionToken.user_id == user.id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    row.revoked = True
    db.commit()
    from security.audit import audit

    audit(db, "session_revoked", f"session_id={session_id}", actor=f"user:{user.username}", component="auth")
    return {"ok": True, "detail": f"Session {session_id} revoked."}


@router.post("/sessions/revoke-all")
def revoke_all(user: CurrentUser, db: DbDep):
    count = identity.revoke_all_sessions(db, user.id, actor=f"user:{user.username}")
    return {"ok": True, "revoked": count}


@router.get("/roles")
def roles(user: CurrentUser):
    return {"roles": describe_roles()}


@router.post("/users/{user_id}/role")
def change_role(user_id: int, body: RoleChangeIn, db: DbDep, user: AdminUser):
    from database.models import User as UserModel

    target = db.get(UserModel, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot change your own role.")
    try:
        new_role = Role(body.role).value
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown role: {body.role}") from exc
    old = target.role
    target.role = new_role
    db.commit()
    from security.audit import audit

    audit(
        db,
        "role_changed",
        f"user={target.username} {old}->{new_role}",
        actor=f"user:{user.username}",
        component="auth",
    )
    return {"ok": True, "user": target.username, "role": new_role}


def _public_user(user) -> dict:
    return {
        "id": user.id,
        "public_id": user.public_id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "email": user.email,
        "conversation_history_enabled": user.conversation_history_enabled,
        "long_term_memory_enabled": user.long_term_memory_enabled,
        "created_at": user.created_at.isoformat() + "Z",
        "last_login_at": user.last_login_at.isoformat() + "Z" if user.last_login_at else None,
    }
