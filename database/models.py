"""ALYO data model.

Design rules:
- Every user-owned row carries ``user_id``: one user's private data must never
  leak into another user's account even on a shared device (spec 17.3 / 23).
- Audit logs and security events are append-only (enforced in security/audit.py).
- No plaintext secrets ever reach these tables; passwords are PBKDF2 hashes,
  session tokens are stored as SHA-256 digests.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from timeutil import utcnow


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, default=new_id, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # True only when a provider (or a future local verification flow) proved
    # ownership of the email. OAuth account-linking trusts this flag and this
    # flag only — an unverified claim can never receive someone else's login.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="STUDENT", index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-user privacy controls (spec §16.4) — never shared across accounts.
    conversation_history_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    long_term_memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SessionToken(Base):
    """Bearer session token. Only the SHA-256 digest is stored."""

    __tablename__ = "session_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    client_label: Mapped[str] = mapped_column(String(128), default="")


class OAuthIdentity(Base):
    """A linked social sign-in (Google, LinkedIn, ...).

    Links an external provider subject to a local account. Only the subject id
    and the provider-asserted email are stored — no provider tokens are kept
    after the login exchange.
    """

    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_oauth_provider_subject"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), default="")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Device(Base):
    """A J.A.R.V.S LINK (or other paired client) owned by a user.

    - ``device_id`` is the public stable identifier the firmware displays.
    - ``token_hash`` is the SHA-256 digest of the one-time device token that
      was shown exactly once at pairing time — the token itself is never
      stored (same rule as session tokens).
    - ``pairing_code_hash`` holds the digest + expiry of an in-flight pairing
      code so a revoked code can never be replayed.
    - ``state`` mirrors the OLED lifecycle: offline / ready / listening /
      thinking / speaking / error.
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, default=new_id)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="J.A.R.V.S LINK")
    kind: Mapped[str] = mapped_column(String(32), default="jarvs-link")
    firmware: Mapped[str] = mapped_column(String(64), default="unknown")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    pairing_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="offline", index=True)
    battery_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_dbm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ConversationTurn(Base):
    """Short-term memory: the running conversation, per user."""

    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16), default="en")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Memory(Base):
    """Long-term memory. User-controlled; secrets are rejected at write time."""

    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("user_id", "category", "key", name="uq_mem_user_cat_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(48), default="IMPORTANT_FACTS", index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="user")  # user | assistant
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # low|normal|high|urgent
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|done|cancelled
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    recurrence: Mapped[str] = mapped_column(String(32), default="")  # "" | daily | weekly | monthly
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    """Append-only audit trail. Updates and deletes are rejected in code."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(96), default="system")  # user:username or system
    detail: Mapped[str] = mapped_column(Text, default="")
    component: Mapped[str] = mapped_column(String(64), default="core")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PendingConfirmation(Base):
    """Human-in-the-loop gate for dangerous tool executions."""

    __tablename__ = "pending_confirmations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolution: Mapped[str] = mapped_column(String(16), default="")  # approved|denied|expired


class SecurityEvent(Base):
    """Bug-detector / threat-detection finding (spec 15.2 / 15.3)."""

    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    component: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)  # INFO|LOW|MEDIUM|HIGH|CRITICAL
    error_type: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    possible_cause: Mapped[str] = mapped_column(Text, default="")
    recommended_action: Mapped[str] = mapped_column(Text, default="")
    risk_score: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    status: Mapped[str] = mapped_column(String(24), default="OPEN", index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    evidence: Mapped[str] = mapped_column(Text, default="")  # JSON list of evidence refs
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_notes: Mapped[str] = mapped_column(Text, default="")


class Incident(Base):
    """Incident record with lifecycle states (spec 15.5)."""

    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(24), default="DETECTED", index=True)
    # DETECTED|INVESTIGATING|CONTAINED|RESOLVED|FALSE_POSITIVE|ESCALATED
    severity: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    component: Mapped[str] = mapped_column(String(64), default="")
    timeline_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    recommended_action: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Index("ix_tasks_user_status", Task.user_id, Task.status)
Index("ix_turns_user_created", ConversationTurn.user_id, ConversationTurn.created_at)
