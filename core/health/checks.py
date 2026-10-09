"""Real health probes (Reliability Addendum Module 1).

Every check here performs an actual observation — a query, a socket connect,
a real HTTP round trip, a filesystem measurement. Nothing in this module may
report HEALTHY based on configuration alone: a configured integration is
``NOT_CONFIGURED`` until proven, and ``FAILED``/``DEGRADED`` once probed and
found broken.

Diagnostics returned by these checks must never contain secrets (API keys,
tokens, passwords) — only configuration *names* and non-sensitive facts.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Any

import psutil
from sqlalchemy import text

from config.settings import settings
from core.brain.providers import provider_status


# Truthful states (Reliability Addendum Module 1).
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
OFFLINE = "OFFLINE"
FAILED = "FAILED"
NOT_CONFIGURED = "NOT_CONFIGURED"
UNKNOWN = "UNKNOWN"
RECOVERING = "RECOVERING"

ALL_STATUSES = (HEALTHY, DEGRADED, OFFLINE, FAILED, NOT_CONFIGURED, UNKNOWN, RECOVERING)

# Status -> alert severity. UNKNOWN is deliberately loud enough to notice:
# "we don't know" is not "fine".
STATUS_SEVERITY = {
    HEALTHY: "INFO",
    DEGRADED: "MEDIUM",
    OFFLINE: "HIGH",
    FAILED: "HIGH",
    NOT_CONFIGURED: "LOW",
    UNKNOWN: "MEDIUM",
    RECOVERING: "LOW",
}


@dataclass
class CheckResult:
    status: str
    detail: str = ""
    latency_ms: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return STATUS_SEVERITY.get(self.status, "MEDIUM")

    @property
    def ok(self) -> bool:
        return self.status == HEALTHY


def _timed(fn) -> CheckResult:
    """Run a probe function and attach real wall-clock latency."""
    start = time.perf_counter()
    result = fn()
    result.latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return result


# ----------------------------------------------------------------------
# Individual probes
# ----------------------------------------------------------------------

def check_database() -> CheckResult:
    """Round-trip query against the engine actually in use."""
    from database.session import engine

    def probe() -> CheckResult:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return CheckResult(
            status=HEALTHY,
            detail="Query round-trip succeeded.",
            diagnostics={"dialect": engine.dialect.name},
        )

    try:
        return _timed(probe)
    except Exception as exc:  # noqa: BLE001 - probe must classify everything
        return CheckResult(
            status=OFFLINE,
            detail=f"Database query failed: {type(exc).__name__}.",
            diagnostics={"error_type": type(exc).__name__, "recoverable": True},
        )


def check_ai_provider() -> CheckResult:
    """Honest AI provider state.

    - ``local`` rules brain: HEALTHY (it genuinely works offline).
    - ``openai`` without a key: NOT_CONFIGURED (configured-but-broken at
      startup is reported by the startup finder, not here).
    - ``openai`` with a key: a *real* authenticated round trip to the
      provider's /models endpoint. Configured is never assumed healthy.
    """
    status = provider_status()
    if status["mode"] == "local-rules":
        return CheckResult(
            status=HEALTHY,
            detail="Local rule brain active (no cloud AI configured).",
            diagnostics={"provider": "local", "mode": "local-rules"},
        )
    if not status["available"]:
        return CheckResult(
            status=NOT_CONFIGURED,
            detail=status["detail"],
            diagnostics={
                "provider": status["provider"],
                "required_env_vars": ["ALYO_LLM_PROVIDER", "ALYO_OPENAI_API_KEY"],
            },
        )

    import httpx

    base = settings.openai_base_url.rstrip("/")
    try:
        response = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=settings.health_probe_timeout_seconds,
        )
    except httpx.TimeoutException:
        return CheckResult(
            status=DEGRADED,
            detail=f"Provider did not answer within {settings.health_probe_timeout_seconds}s.",
            diagnostics={"provider": "openai", "recoverable": True},
        )
    except httpx.HTTPError as exc:
        return CheckResult(
            status=OFFLINE,
            detail=f"Provider unreachable: {type(exc).__name__}.",
            diagnostics={"provider": "openai", "error_type": type(exc).__name__, "recoverable": True},
        )
    if response.status_code in (401, 403):
        # Permanent: retrying with the same key can never succeed.
        return CheckResult(
            status=FAILED,
            detail="Provider rejected the configured credentials (HTTP "
            f"{response.status_code}). The key is invalid or revoked.",
            diagnostics={"provider": "openai", "http_status": response.status_code, "recoverable": False},
        )
    if response.status_code >= 400:
        return CheckResult(
            status=DEGRADED,
            detail=f"Provider answered HTTP {response.status_code}.",
            diagnostics={"provider": "openai", "http_status": response.status_code, "recoverable": True},
        )
    return CheckResult(
        status=HEALTHY,
        detail="Authenticated provider round trip succeeded.",
        diagnostics={
            "provider": "openai",
            "model": settings.openai_model,
            "http_status": response.status_code,
        },
    )


def check_voice_server() -> CheckResult:
    """TCP reachability of the voice WS server + configured engine states."""
    from backend.voice.config import voice_config

    host = "127.0.0.1"
    port = voice_config.port
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=1.0):
            pass
    except OSError as exc:
        return CheckResult(
            status=OFFLINE,
            detail=f"Voice server not reachable on {host}:{port} ({type(exc).__name__}).",
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            diagnostics={
                "host": host,
                "port": port,
                "stt_provider": voice_config.stt_provider or None,
                "tts_provider": voice_config.tts_provider or None,
                "recoverable": True,
            },
        )
    engines = {
        "stt": voice_config.stt_provider or None,
        "tts": voice_config.tts_provider or None,
    }
    detail = "Voice server reachable."
    status = HEALTHY
    if not engines["stt"] or not engines["tts"]:
        status = DEGRADED
        detail = "Voice server reachable, but STT/TTS engines are not configured."
    return CheckResult(
        status=status,
        detail=detail,
        latency_ms=round((time.perf_counter() - start) * 1000, 2),
        diagnostics={"host": host, "port": port, "engines": engines},
    )


def check_storage() -> CheckResult:
    """Disk capacity on the volume holding the data directory."""
    try:
        usage = psutil.disk_usage(str(settings.data_dir))
    except OSError as exc:
        return CheckResult(
            status=FAILED,
            detail=f"Storage could not be measured: {type(exc).__name__}.",
            diagnostics={"recoverable": True},
        )
    pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    free_gb = round(usage.free / 1e9, 2)
    diagnostics = {"used_percent": pct, "free_gb": free_gb}
    if pct >= 97 or free_gb < 0.5:
        return CheckResult(
            status=FAILED,
            detail=f"Storage critically low: {pct}% used, {free_gb} GB free.",
            diagnostics=diagnostics,
        )
    if pct >= 90:
        return CheckResult(
            status=DEGRADED,
            detail=f"Storage low: {pct}% used ({free_gb} GB free).",
            diagnostics=diagnostics,
        )
    return CheckResult(
        status=HEALTHY, detail=f"Storage {pct}% used, {free_gb} GB free.", diagnostics=diagnostics
    )


def check_workers() -> CheckResult:
    """Background worker heartbeats (recorded by the workers themselves)."""
    from core.health.service import worker_heartbeat_age_seconds

    ages = {name: worker_heartbeat_age_seconds(name) for name in ("retention",)}
    stale = settings.health_worker_stale_seconds
    if all(age is None for age in ages.values()):
        return CheckResult(
            status=UNKNOWN,
            detail="No worker heartbeat recorded yet (workers report on first cycle).",
            diagnostics={"heartbeats": ages},
        )
    lagging = {name: age for name, age in ages.items() if age is None or age > stale}
    if lagging:
        return CheckResult(
            status=DEGRADED,
            detail=f"Worker heartbeat(s) stale: {', '.join(sorted(lagging))}.",
            diagnostics={"heartbeats": ages, "stale_after_seconds": stale, "recoverable": True},
        )
    return CheckResult(
        status=HEALTHY, detail="All workers reported recent heartbeats.", diagnostics={"heartbeats": ages}
    )


def check_devices() -> CheckResult:
    """Paired-device connectivity from the registry's own telemetry."""
    from sqlalchemy import func, select

    from database.session import SessionLocal
    from database.models import Device
    from timeutil import utcnow

    db = SessionLocal()
    try:
        total = db.scalar(select(func.count(Device.id))) or 0
        online = db.scalar(
            select(func.count(Device.id)).where(Device.state != "offline")
        ) or 0
    finally:
        db.close()
    if total == 0:
        return CheckResult(
            status=NOT_CONFIGURED,
            detail="No devices are paired yet.",
            diagnostics={"devices_total": 0},
        )
    return CheckResult(
        status=HEALTHY if online else DEGRADED,
        detail=f"{online}/{total} paired device(s) reporting.",
        diagnostics={"devices_total": total, "devices_online": online, "checked_at": utcnow().isoformat()},
    )


def check_backup() -> CheckResult:
    """Truthful: there is no backup subsystem yet, so this is never healthy."""
    return CheckResult(
        status=NOT_CONFIGURED,
        detail="No backup subsystem is implemented; backup status cannot be verified.",
        diagnostics={"recoverable": False, "required": "Backup subsystem (not implemented)"},
    )


def check_payments() -> CheckResult:
    return CheckResult(
        status=NOT_CONFIGURED,
        detail="No payment integration is configured.",
        diagnostics={"recoverable": False},
    )


# Components that a failing check may auto-recover from (safe, read-only
# re-probes). NOT_CONFIGURED and financial components are never auto-recovered.
RECOVERABLE = {"database", "ai_provider", "voice_server", "storage"}
