"""Identity: registration, authentication, session tokens.

Security properties:
- Passwords: PBKDF2-HMAC-SHA256, per-user salt, constant-time comparison.
- Session tokens: random 256-bit values; only SHA-256 digests are stored, so a
  database leak does not yield usable tokens.
- Failed logins are audited and counted by the threat detector.
- No plaintext secret is ever written to the database or the logs.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from core.permissions.rbac import Role
from database.models import SessionToken, User
from security.audit import audit
from timeutil import utcnow, utcnow_plus

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
# People naturally type their email — accept that as a username too.
_EMAIL_USERNAME_RE = re.compile(r"^[^@\s]{1,61}@[^@\s]{1,61}\.[A-Za-z]{2,}$")
_MIN_PASSWORD_LEN = 8

# A dummy hash used when the username does not exist, so login timing does not
# reveal whether an account is present.
_DUMMY_HASH = "pbkdf2_sha256$310000$placeholder$placeholder"


class IdentityError(Exception):
    """User-facing authentication/registration failure."""


def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or settings.pbkdf2_iterations
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations_s, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations_s)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def register_user(
    db: Session,
    username: str,
    password: str,
    *,
    role: str = Role.STUDENT,
    email: str | None = None,
    display_name: str = "",
    actor: str = "system",
) -> User:
    username = (username or "").strip()
    if _EMAIL_USERNAME_RE.match(username):
        username = username.lower()  # email addresses are case-insensitive
    elif not _USERNAME_RE.match(username):
        raise IdentityError(
            "Username must be 3-64 characters: letters, digits, dot, dash, "
            "underscore — or an email address like you@example.com."
        )
    if len(password or "") < _MIN_PASSWORD_LEN:
        raise IdentityError(f"Password must be at least {_MIN_PASSWORD_LEN} characters.")
    if db.scalar(select(User).where(User.username == username)):
        raise IdentityError(
            "That username is already taken — switch to the Sign in tab "
            "and use your password."
        )
    if email and db.scalar(select(User).where(User.email == email)):
        raise IdentityError("That email is already registered.")
    try:
        role_value = Role(role).value
    except ValueError as exc:
        raise IdentityError(f"Unknown role: {role}") from exc

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role=role_value,
        display_name=display_name or username,
    )
    db.add(user)
    db.commit()
    audit(db, "user_registered", f"username={username} role={role_value}", actor=actor, component="identity")
    return user


def authenticate(db: Session, username: str, password: str) -> User:
    """Return the user on success; raise IdentityError on failure.

    Failures are audited here and counted by the threat detector at the API
    layer, where client context (IP, request id) is available.
    """
    username = (username or "").strip()
    if _EMAIL_USERNAME_RE.match(username):
        username = username.lower()  # same normalisation as registration
    user = db.scalar(select(User).where(User.username == username))
    stored = user.password_hash if user else _DUMMY_HASH
    if not verify_password(password or "", stored) or user is None:
        audit(
            db,
            "login_failed",
            f"username={username!r}",
            actor=f"user:{username}" if username else "anonymous",
            component="identity",
        )
        raise IdentityError("Invalid username or password.")
    if not user.is_active:
        audit(db, "login_blocked_inactive", f"username={user.username}", actor=f"user:{user.username}", component="identity")
        raise IdentityError("This account is deactivated.")
    user.last_login_at = utcnow()
    db.commit()
    audit(db, "login_success", f"username={user.username}", actor=f"user:{user.username}", component="identity")
    return user


def create_session(db: Session, user: User, client_label: str = "") -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = utcnow_plus(minutes=settings.session_ttl_minutes)
    db.add(
        SessionToken(
            token_hash=token_hash,
            user_id=user.id,
            expires_at=expires,
            client_label=client_label[:128],
        )
    )
    db.commit()
    audit(
        db,
        "session_created",
        f"user={user.username} expires={expires.isoformat()}Z",
        actor=f"user:{user.username}",
        component="identity",
    )
    return token, expires


def resolve_session(db: Session, token: str) -> User | None:
    """Resolve a bearer token to an active, unexpired, unrevoked user."""
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.scalar(select(SessionToken).where(SessionToken.token_hash == token_hash))
    if row is None or row.revoked or row.expires_at <= utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return user


def revoke_session(db: Session, token: str, actor: str) -> bool:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.scalar(select(SessionToken).where(SessionToken.token_hash == token_hash))
    if row is None or row.revoked:
        return False
    row.revoked = True
    db.commit()
    audit(db, "session_revoked", f"session_id={row.id}", actor=actor, component="identity")
    return True


def revoke_all_sessions(db: Session, user_id: int, actor: str) -> int:
    rows = db.scalars(
        select(SessionToken).where(SessionToken.user_id == user_id, SessionToken.revoked.is_(False))
    ).all()
    for row in rows:
        row.revoked = True
    db.commit()
    if rows:
        audit(db, "sessions_revoked", f"user_id={user_id} count={len(rows)}", actor=actor, component="identity")
    return len(rows)


def active_sessions(db: Session, user_id: int) -> list[SessionToken]:
    return db.scalars(
        select(SessionToken)
        .where(
            SessionToken.user_id == user_id,
            SessionToken.revoked.is_(False),
            SessionToken.expires_at > utcnow(),
        )
        .order_by(SessionToken.created_at.desc())
    ).all()
