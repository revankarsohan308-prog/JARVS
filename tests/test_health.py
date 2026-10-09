"""Health & recovery subsystem tests (Reliability Addendum Modules 1 & 10).

These tests prove truthful health reporting and bounded recovery:
- a failing probe is reported failed/degraded, never healthy (#12)
- NOT_CONFIGURED components are never reported healthy, e.g. backup (#9)
- a provider auth failure is permanent and never hammer-retried (#6/#15)
- a timeout ("outcome unknown") is never blindly repeated (#8)
- recovery stops at its configured safety limit and records the episode (#15)
- health diagnostics never contain secrets (#13)
"""

from __future__ import annotations

import pytest

from config.settings import settings
from core.health import checks, recovery, service


@pytest.fixture(autouse=True)
def _clean_runtime_state():
    service.reset_runtime_state()
    yield
    service.reset_runtime_state()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """No real waiting between re-probes in tests."""
    monkeypatch.setattr(settings, "health_retry_base_delay_seconds", 0.0)


class _FakeEngine:
    """Stands in for the SQLAlchemy engine; can be told to fail."""

    class _Conn:
        def execute(self, _stmt):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.dialect = type("D", (), {"name": "sqlite"})()

    def connect(self):
        if self.fail:
            raise ConnectionError("db file is locked")
        return self._Conn()


# ----------------------------------------------------------------------
# Individual probes are truthful
# ----------------------------------------------------------------------

def test_database_check_healthy_reports_latency(db):
    result = checks.check_database()
    assert result.status == checks.HEALTHY
    assert result.latency_ms is not None and result.latency_ms >= 0


def test_database_check_failure_is_not_healthy(db, monkeypatch):
    from database import session as db_session

    monkeypatch.setattr(db_session, "engine", _FakeEngine(fail=True))
    result = checks.check_database()
    assert result.status == checks.OFFLINE
    assert result.diagnostics["error_type"] == "ConnectionError"


def test_backup_is_never_reported_healthy(db):
    """A missing backup system must not read as healthy (Module 10 #9)."""
    result = checks.check_backup()
    assert result.status == checks.NOT_CONFIGURED
    assert result.ok is False
    report = service.run_component(db, "backup", recover=True)
    assert report["status"] == checks.NOT_CONFIGURED
    # NOT_CONFIGURED is not a recoverable failure: no episode may be recorded.
    assert service.recovery_history(db, "backup") == []


def test_storage_check_matches_real_disk(db):
    result = checks.check_storage()
    assert result.status in (checks.HEALTHY, checks.DEGRADED, checks.FAILED)
    assert "used_percent" in result.diagnostics


def test_ai_provider_local_brain_is_healthy_not_fake_cloud(db):
    result = checks.check_ai_provider()
    assert result.status == checks.HEALTHY
    assert result.diagnostics["mode"] == "local-rules"


def test_ai_provider_rejected_credentials_report_failed(db, monkeypatch):
    """An authenticated probe that gets 401 is FAILED and permanent (#6)."""

    class _Resp:
        status_code = 401

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-should-never-leak")
    monkeypatch.setattr(settings, "openai_base_url", "https://example.invalid/v1")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())

    result = checks.check_ai_provider()
    assert result.status == checks.FAILED
    assert result.diagnostics["recoverable"] is False
    assert "sk-test-should-never-leak" not in result.detail
    assert "sk-test-should-never-leak" not in str(result.diagnostics)


def test_ai_provider_server_error_is_not_reported_healthy(db, monkeypatch):
    class _Resp:
        status_code = 503

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_base_url", "https://example.invalid/v1")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())

    result = checks.check_ai_provider()
    assert result.status != checks.HEALTHY
    assert result.status == checks.DEGRADED


def test_worker_heartbeat_states(db):
    assert service.worker_heartbeat_age_seconds("retention") is None
    result = checks.check_workers()
    assert result.status == checks.UNKNOWN
    service.record_worker_heartbeat("retention")
    result = checks.check_workers()
    assert result.status == checks.HEALTHY


# ----------------------------------------------------------------------
# Bounded retries (run_bounded)
# ----------------------------------------------------------------------

def test_run_bounded_stops_at_configured_limit():
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky():
        calls["n"] += 1
        raise ConnectionError("down")

    policy = recovery.RetryPolicy(max_attempts=3, base_delay=0.1, max_delay=0.25)
    outcome = recovery.run_bounded(flaky, policy=policy, sleep=sleeps.append)

    assert outcome.success is False
    assert outcome.outcome == "GAVE_UP"
    assert outcome.attempts == 3 == calls["n"]
    # Backoff grows but is capped — no unbounded delay.
    assert sleeps == [0.1, 0.2]
    assert all(s <= policy.max_delay for s in sleeps)


def test_run_bounded_never_retries_permanent_failures():
    calls = {"n": 0}

    def bad_credentials():
        calls["n"] += 1
        raise recovery.PermanentFailure("provider rejected the API key")

    outcome = recovery.run_bounded(bad_credentials, sleep=lambda _s: None)
    assert outcome.outcome == "FAILED_PERMANENT"
    assert outcome.attempts == 1 == calls["n"]


def test_run_bounded_never_blindly_repeats_unknown_outcome():
    """A timeout must not repeat a possibly-completed action (#8)."""
    calls = {"n": 0}

    def timed_out_payment_like_action():
        calls["n"] += 1
        raise recovery.OutcomeUnknown("request timed out; payment may have gone through")

    outcome = recovery.run_bounded(timed_out_payment_like_action, sleep=lambda _s: None)
    assert outcome.outcome == "OUTCOME_UNKNOWN"
    assert outcome.attempts == 1 == calls["n"]
    assert "NOT retried" in outcome.detail


def test_run_bounded_success_after_transient_retries():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("blip")
        return "ok"

    outcome = recovery.run_bounded(flaky, sleep=lambda _s: None)
    assert outcome.success is True
    assert outcome.outcome == "SUCCESS"
    assert outcome.value == "ok"
    assert outcome.attempts == 3


# ----------------------------------------------------------------------
# Cooldown guard (retry-storm prevention)
# ----------------------------------------------------------------------

def test_cooldown_guard_rate_limits_recovery_episodes():
    now = {"t": 0.0}
    guard = recovery.CooldownGuard(clock=lambda: now["t"])

    assert guard.allow("db") is True
    guard.start("db")
    assert guard.allow("db") is False
    now["t"] += settings.health_recovery_cooldown_seconds - 1
    assert guard.allow("db") is False
    now["t"] += 2
    assert guard.allow("db") is True


# ----------------------------------------------------------------------
# Component workflow: status persistence, recovery, audit
# ----------------------------------------------------------------------

def _register_flaky_component(monkeypatch, *, fail_times: int, recoverable: bool = True):
    """Register a test component whose probe fails ``fail_times`` times."""
    state = {"n": 0, "healed": False}

    def probe() -> checks.CheckResult:
        state["n"] += 1
        if not state["healed"] and state["n"] <= fail_times:
            return checks.CheckResult(
                status=checks.OFFLINE, detail="dependency down", diagnostics={"recoverable": True}
            )
        return checks.CheckResult(status=checks.HEALTHY, detail="dependency back")

    monkeypatch.setitem(service.COMPONENTS, "testprobe", (probe, "test probe"))
    if recoverable:
        monkeypatch.setattr(checks, "RECOVERABLE", set(checks.RECOVERABLE) | {"testprobe"})
    return state


def test_component_recovery_reverify_and_record(db, monkeypatch):
    """Recovery only reports success after a re-probe proves it (#15)."""
    _register_flaky_component(monkeypatch, fail_times=1)
    report = service.run_component(db, "testprobe", sleep=lambda _s: None)

    assert report["status"] == checks.HEALTHY  # re-probe proved recovery
    assert report["recovery"]["outcome"] == "SUCCESS"
    attempts = service.recovery_history(db, "testprobe")
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "SUCCESS"


def test_component_recovery_gives_up_at_limit(db, monkeypatch):
    _register_flaky_component(monkeypatch, fail_times=99)
    report = service.run_component(db, "testprobe", sleep=lambda _s: None)

    assert report["status"] != checks.HEALTHY
    assert report["failure_count"] == 1
    attempts = service.recovery_history(db, "testprobe")
    assert attempts[0]["outcome"] == "GAVE_UP"
    assert attempts[0]["probes"] == settings.health_recovery_max_attempts


def test_component_cooldown_prevents_retry_storm(db, monkeypatch):
    _register_flaky_component(monkeypatch, fail_times=99)
    service.run_component(db, "testprobe", sleep=lambda _s: None)
    second = service.run_component(db, "testprobe", sleep=lambda _s: None)

    # Second episode was refused by the cooldown guard, not executed.
    assert second["recovery"] is None
    attempts = service.recovery_history(db, "testprobe")
    outcomes = [a["outcome"] for a in attempts]  # newest first
    assert outcomes.count("GAVE_UP") == 1
    assert "SKIPPED_COOLDOWN" in outcomes


def test_state_transitions_audited_once_not_per_check(db, monkeypatch):
    """A component that stays down must not flood the audit trail."""
    from sqlalchemy import func, select

    from database.models import AuditLog

    _register_flaky_component(monkeypatch, fail_times=99)
    service.run_component(db, "testprobe", sleep=lambda _s: None)
    for _ in range(3):
        service.run_component(db, "testprobe", recover=False, sleep=lambda _s: None)

    count = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.event == "health_state_changed", AuditLog.detail.like("%testprobe%")
        )
    )
    assert count == 1


def test_permanent_component_failure_not_retried(db, monkeypatch):
    """A probe whose diagnostics say 'not recoverable' is probed once only."""
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return checks.CheckResult(
            status=checks.FAILED,
            detail="credentials rejected",
            diagnostics={"recoverable": False},
        )

    monkeypatch.setitem(service.COMPONENTS, "testprobe", (probe, "test probe"))
    monkeypatch.setattr(checks, "RECOVERABLE", set(checks.RECOVERABLE) | {"testprobe"})

    report = service.run_component(db, "testprobe", sleep=lambda _s: None)
    assert calls["n"] == 1  # recovery never even started for a permanent failure
    assert report["status"] == checks.FAILED
    assert report["recovery"] is None
    assert service.recovery_history(db, "testprobe") == []


def test_overall_status_reports_worst_truthful_state():
    assert service.overall_status([{"status": "HEALTHY"}, {"status": "FAILED"}]) == "FAILED"
    assert service.overall_status([{"status": "NOT_CONFIGURED"}, {"status": "HEALTHY"}]) == "HEALTHY"
    assert service.overall_status([]) == "UNKNOWN"


# ----------------------------------------------------------------------
# API surface
# ----------------------------------------------------------------------

def test_health_components_requires_auth(client, db):
    assert client.get("/api/v1/system/health/components").status_code == 401


def test_health_components_endpoint_returns_real_sweep(client, db, student_session):
    resp = client.get(
        "/api/v1/system/health/components",
        headers={"Authorization": f"Bearer {student_session['token']}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {c["component"] for c in body["components"]}
    assert {"database", "ai_provider", "storage", "backup"} <= names
    by_name = {c["component"]: c for c in body["components"]}
    assert by_name["database"]["status"] == checks.HEALTHY
    assert by_name["backup"]["status"] == checks.NOT_CONFIGURED
    assert "recovery_history" in body


def test_health_endpoint_never_leaks_secrets(client, db, student_session, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-super-secret-do-not-leak")
    resp = client.get(
        "/api/v1/system/health/components",
        headers={"Authorization": f"Bearer {student_session['token']}"},
    )
    assert resp.status_code == 200
    assert "sk-super-secret-do-not-leak" not in resp.text


def test_single_component_probe_and_unknown_404(client, db, student_session):
    headers = {"Authorization": f"Bearer {student_session['token']}"}
    resp = client.get("/api/v1/system/health/components/storage", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["component"] == "storage"

    resp = client.get("/api/v1/system/health/components/nope", headers=headers)
    assert resp.status_code == 404
