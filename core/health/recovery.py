"""Bounded, classified recovery primitives (Reliability Addendum Module 1).

Rules enforced here, centrally, so no call site has to remember them:

- Transient failures are retried with capped exponential backoff — never past
  ``max_attempts``.
- Permanent failures (invalid credentials, "not configured", 4xx that will not
  change on retry) are **never** retried.
- Recovery episodes per component are rate-limited by a cooldown so a broken
  dependency cannot cause a retry storm.
- Every outcome is reported truthfully: SUCCESS is only ever returned after
  the operation itself confirmed success. A timeout is treated as UNKNOWN —
  the caller decides whether the action is safe to repeat.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

from config.settings import settings

T = TypeVar("T")


class FailureKind(StrEnum):
    TRANSIENT = "TRANSIENT"  # likely to succeed on retry (network blip, lock)
    PERMANENT = "PERMANENT"  # will not change on retry (bad credential, 404)
    UNKNOWN = "UNKNOWN"  # e.g. timeout: outcome of the action is unknown
    UNSAFE = "UNSAFE"  # retrying could duplicate a side effect


class PermanentFailure(Exception):
    """Raise from an operation that must not be retried (e.g. bad credentials)."""


class OutcomeUnknown(Exception):
    """Raise when the operation timed out and its real outcome is unknown.

    Recovery never retries these blindly — the caller must inspect actual
    state first (Reliability Addendum: "a timeout must not blindly repeat a
    potentially completed action").
    """


class RetryBudgetExceeded(Exception):
    """Raised when an operation keeps failing past the configured limit."""

    def __init__(self, component: str, attempts: int, last_error: str) -> None:
        super().__init__(
            f"{component}: gave up after {attempts} attempt(s); last error: {last_error}"
        )
        self.component = component
        self.attempts = attempts
        self.last_error = last_error


def classify_exception(exc: BaseException) -> FailureKind:
    """Classify an exception for retry decisions (default: TRANSIENT).

    Anything unrecognized defaults to TRANSIENT because the common failure
    mode here is a flaky dependency — but explicit permanent signals always
    win, so invalid credentials are never hammer-retried.
    """
    if isinstance(exc, (PermanentFailure,)):
        return FailureKind.PERMANENT
    if isinstance(exc, (OutcomeUnknown,)):
        return FailureKind.UNKNOWN
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return FailureKind.TRANSIENT
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return FailureKind.PERMANENT
        if 400 <= status < 500:
            return FailureKind.PERMANENT
        if status >= 500:
            return FailureKind.TRANSIENT
    return FailureKind.TRANSIENT


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 0
    base_delay: float = 0.0
    max_delay: float = 5.0
    multiplier: float = 2.0

    @classmethod
    def from_settings(cls) -> RetryPolicy:
        return cls(
            max_attempts=max(1, settings.health_recovery_max_attempts),
            base_delay=max(0.0, settings.health_retry_base_delay_seconds),
            max_delay=max(0.1, settings.health_retry_max_delay_seconds),
        )

    def delay_for(self, attempt: int) -> float:
        """Capped exponential backoff for the given 0-based attempt index."""
        return min(self.base_delay * (self.multiplier ** attempt), self.max_delay)


@dataclass
class RecoveryOutcome:
    """Truthful summary of one bounded recovery episode."""

    success: bool
    attempts: int
    outcome: str  # SUCCESS|GAVE_UP|FAILED_PERMANENT|OUTCOME_UNKNOWN
    detail: str = ""
    value: object = None


def run_bounded(
    operation: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    classify: Callable[[BaseException], FailureKind] = classify_exception,
) -> RecoveryOutcome:
    """Run ``operation`` with bounded, classified retries.

    Stops at the first permanent failure, the first unknown-outcome failure,
    or ``policy.max_attempts`` — whichever comes first. ``sleep`` is
    injectable so tests never actually wait.
    """
    policy = policy or RetryPolicy.from_settings()
    last_error = ""
    for attempt in range(1, max(1, policy.max_attempts) + 1):
        try:
            value = operation()
            return RecoveryOutcome(
                success=True, attempts=attempt, outcome="SUCCESS", value=value
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            last_error = f"{type(exc).__name__}: {exc}"
            kind = classify(exc)
            if kind is FailureKind.PERMANENT:
                return RecoveryOutcome(
                    success=False,
                    attempts=attempt,
                    outcome="FAILED_PERMANENT",
                    detail=f"Not retried ({kind}): {last_error}",
                )
            if kind is FailureKind.UNKNOWN:
                return RecoveryOutcome(
                    success=False,
                    attempts=attempt,
                    outcome="OUTCOME_UNKNOWN",
                    detail=(
                        "Operation outcome is unknown (e.g. a timeout); it was "
                        f"NOT retried to avoid duplicating a completed action. Last: {last_error}"
                    ),
                )
            if attempt < policy.max_attempts:
                sleep(policy.delay_for(attempt - 1))
    return RecoveryOutcome(
        success=False,
        attempts=policy.max_attempts,
        outcome="GAVE_UP",
        detail=f"Exhausted {policy.max_attempts} attempt(s): {last_error}",
    )


# ----------------------------------------------------------------------
# Cooldown guard (retry-storm prevention)
# ----------------------------------------------------------------------

class CooldownGuard:
    """In-process rate limiter for recovery episodes, keyed by component.

    While a component is cooling down, new recovery episodes are refused
    (recorded as SKIPPED_COOLDOWN by the caller) so a hard-down dependency
    cannot produce an unbounded stream of retries, audit rows or alerts.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._last: dict[str, float] = {}
        self._clock = clock

    def allow(self, key: str, cooldown_seconds: float | None = None) -> bool:
        """True when recovery may run for ``key`` now (does not start it)."""
        window = (
            cooldown_seconds
            if cooldown_seconds is not None
            else settings.health_recovery_cooldown_seconds
        )
        now = self._clock()
        last = self._last.get(key)
        if last is not None and (now - last) < window:
            return False
        return True

    def start(self, key: str) -> None:
        """Mark a recovery episode as started (begins the cooldown window)."""
        self._last[key] = self._clock()

    def reset(self) -> None:
        self._last.clear()


# Module-level guard used by the health service; tests reset it directly.
guard = CooldownGuard()


@dataclass
class RecoveryReport:
    """Result of a full DETECT→…→REPORT episode for one component."""

    component: str
    status_before: str
    status_after: str
    strategy: str
    outcome: str
    detail: str = ""
    probes: int = 1
    history: list[dict[str, str]] = field(default_factory=list)
