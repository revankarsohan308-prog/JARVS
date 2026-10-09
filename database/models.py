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
    Float,
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
    """Long-term memory. User-controlled; secrets are rejected at write time.

    Quality metadata (Reliability Addendum Module 4) makes each memory's
    trustworthiness inspectable: confidence, importance, verification state,
    provenance and a retention policy with an optional expiry.
    """

    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("user_id", "category", "key", name="uq_mem_user_cat_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(48), default="IMPORTANT_FACTS", index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="user")  # user | assistant
    # 0.0-1.0 self-reported certainty; lowered when a contradiction is found.
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    importance: Mapped[int] = mapped_column(Integer, default=3)  # 1 (trivial) - 5 (critical)
    # UNVERIFIED | USER_VERIFIED | CONFLICT | EXPIRED
    verification_state: Mapped[str] = mapped_column(String(24), default="UNVERIFIED", index=True)
    source_ref: Mapped[str] = mapped_column(String(255), default="")  # where the fact came from
    retention_policy: Mapped[str] = mapped_column(String(24), default="KEEP")  # KEEP | WINDOW
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MemoryRevision(Base):
    """A superseded memory value, preserved so corrections are never silent.

    When a memory is overwritten, corrected or merged, the previous value is
    written here first. This is what makes contradiction handling auditable
    instead of a destructive overwrite (Reliability Addendum Module 4).
    """

    __tablename__ = "memory_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    memory_id: Mapped[int] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(48), default="")
    key: Mapped[str] = mapped_column(String(128), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    importance: Mapped[int] = mapped_column(Integer, default=3)
    verification_state: Mapped[str] = mapped_column(String(24), default="")
    reason: Mapped[str] = mapped_column(String(64), default="")  # overwrite|correction|merge|revert
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    # Groups every audit row that belongs to one logical action (e.g. a mission
    # or a request), so its full trail can be reconstructed (Module 3).
    correlation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
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


class ComponentStatusRecord(Base):
    """Persistent per-component health (Reliability Addendum Module 1).

    One row per monitored component. "Configured" is never assumed to mean
    healthy: the status only ever reflects the last real probe result.
    """

    __tablename__ = "component_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # HEALTHY|DEGRADED|OFFLINE|FAILED|NOT_CONFIGURED|UNKNOWN|RECOVERING
    status: Mapped[str] = mapped_column(String(24), default="UNKNOWN", index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")  # INFO|LOW|MEDIUM|HIGH|CRITICAL
    detail: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    recovery_strategy: Mapped[str] = mapped_column(String(48), default="")
    diagnostics_json: Mapped[str] = mapped_column(Text, default="{}")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RecoveryAttempt(Base):
    """One recovery episode for one component (Reliability Addendum Module 1).

    Outcomes are truthful: SUCCESS only when the component was re-verified
    healthy after the recovery action.
    """

    __tablename__ = "recovery_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    component: Mapped[str] = mapped_column(String(64), index=True)
    strategy: Mapped[str] = mapped_column(String(48), default="")
    # SUCCESS|FAILED|GAVE_UP|SKIPPED_COOLDOWN|SKIPPED_UNSAFE|SKIPPED_NOT_CONFIGURED
    outcome: Mapped[str] = mapped_column(String(24), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    probes: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ----------------------------------------------------------------------
# Missions (Reliability Addendum Module 2)
# ----------------------------------------------------------------------

class Mission(Base):
    """A persistent, checkpointed objective owned by exactly one user.

    State is durable so a mission survives application restarts: an
    interrupted RUNNING mission is reconciled to PAUSED on boot and can be
    resumed explicitly. An approval is bound to the plan hash it approved.
    """

    __tablename__ = "missions"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_mission_user_idem"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    objective: Mapped[str] = mapped_column(Text)
    # PLANNING|WAITING_FOR_APPROVAL|READY|RUNNING|PAUSED|RECOVERING|VERIFYING|
    # COMPLETED|PARTIALLY_COMPLETED|FAILED|CANCELLED
    state: Mapped[str] = mapped_column(String(24), default="PLANNING", index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="LOW")
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # Idempotency key so a retried client request cannot create two missions.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(32), default=new_id, index=True)
    plan_hash: Mapped[str] = mapped_column(String(64), default="")
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_json: Mapped[str] = mapped_column(Text, default="{}")
    final_report: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MissionStep(Base):
    """One authorized step of a mission plan, executed one at a time."""

    __tablename__ = "mission_steps"
    __table_args__ = (
        UniqueConstraint("mission_id", "position", name="uq_mission_step_position"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    mission_id: Mapped[str] = mapped_column(
        ForeignKey("missions.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    tool_name: Mapped[str] = mapped_column(String(64))
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    depends_on_json: Mapped[str] = mapped_column(Text, default="[]")  # list of positions
    # PENDING|WAITING_FOR_APPROVAL|RUNNING|COMPLETED|FAILED|SKIPPED|CANCELLED|
    # UNKNOWN_VERIFICATION
    state: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    risk_level: Mapped[str] = mapped_column(String(16), default="LOW")
    irreversible: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MissionCheckpoint(Base):
    """Durable progress marker written after each verified step."""

    __tablename__ = "mission_checkpoints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    mission_id: Mapped[str] = mapped_column(
        ForeignKey("missions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(24), default="")
    label: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MissionEvent(Base):
    """Append-style execution history for a mission (audit correlation)."""

    __tablename__ = "mission_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    mission_id: Mapped[str] = mapped_column(
        ForeignKey("missions.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(96), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MissionApproval(Base):
    """Human approval request/decision bound to a specific plan hash.

    If the plan changes after approval is requested, the approval is stale and
    a fresh decision is required (Reliability Addendum Module 3).
    """

    __tablename__ = "mission_approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    mission_id: Mapped[str] = mapped_column(
        ForeignKey("missions.id", ondelete="CASCADE"), index=True
    )
    plan_hash: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    # PENDING|APPROVED|DENIED|STALE
    decided_by: Mapped[str] = mapped_column(String(96), default="")
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Index("ix_tasks_user_status", Task.user_id, Task.status)
Index("ix_turns_user_created", ConversationTurn.user_id, ConversationTurn.created_at)


class ReleaseRun(Base):
    """A stored release-gate run (Reliability Addendum Module 6).

    The gate never deploys — it records what it observed so the last verdict is
    inspectable and a deploy can be blocked on real, cited failures.
    """

    __tablename__ = "release_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    profile: Mapped[str] = mapped_column(String(32), default="full")
    status: Mapped[str] = mapped_column(String(16), default="NOT_RUN", index=True)
    deploy_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    triggered_by: Mapped[str] = mapped_column(String(96), default="system")
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Notification(Base):
    """A durable, de-duplicated notification (Reliability Addendum Module 8).

    Severity-aware: the engine de-duplicates repeats within a cooldown window,
    tracks delivery/read/acknowledgement, and escalates unacknowledged
    high-severity items. External channels are NOT_CONFIGURED — only the real
    in-app channel is ever claimed as working.
    """

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(24), default="SYSTEM", index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO", index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    dedup_key: Mapped[str] = mapped_column(String(160), default="", index=True)
    # UNREAD | READ | ACKNOWLEDGED | ESCALATED
    status: Mapped[str] = mapped_column(String(16), default="UNREAD", index=True)
    channel: Mapped[str] = mapped_column(String(24), default="IN_APP")
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    correlation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NotificationPreference(Base):
    """Per-user notification preferences (Reliability Addendum Module 8)."""

    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    min_severity: Mapped[str] = mapped_column(String(16), default="INFO")
    muted_categories_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SyncRecord(Base):
    """Current synchronised value of one entity, per user (Module 5).

    Optimistic concurrency: each record carries a monotonically increasing
    ``version``. A client push states the version it based its edit on; if the
    server has moved on, that is a real conflict and is never silently
    overwritten. Sensitive entities always require explicit resolution.
    """

    __tablename__ = "sync_records"
    __table_args__ = (
        UniqueConstraint("user_id", "entity_type", "entity_key", name="uq_sync_entity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_key: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    op: Mapped[str] = mapped_column(String(16), default="UPSERT")  # UPSERT | DELETE
    device_id: Mapped[str] = mapped_column(String(64), default="")
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    conflict_state: Mapped[str] = mapped_column(String(24), default="NONE")  # NONE | CONFLICT
    pending_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_device_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SyncChange(Base):
    """Append-only change log; ``id`` is the sync cursor (Module 5).

    ``client_change_id`` makes offline queue retries idempotent: replaying the
    same client change never applies twice.
    """

    __tablename__ = "sync_changes"
    __table_args__ = (
        UniqueConstraint("user_id", "client_change_id", name="uq_sync_client_change"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_key: Mapped[str] = mapped_column(String(255))
    op: Mapped[str] = mapped_column(String(16), default="UPSERT")
    version: Mapped[int] = mapped_column(Integer, default=0)
    device_id: Mapped[str] = mapped_column(String(64), default="")
    client_change_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
