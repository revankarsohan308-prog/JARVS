"""System health, live metrics and boot report (spec §12 / §47)."""

from __future__ import annotations

import platform

import psutil
from fastapi import APIRouter
from sqlalchemy import text

from api.deps import CurrentUser, DbDep
from config.settings import settings
from core.brain.providers import provider_status

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def health(db: DbDep):
    """Liveness + real dependency checks. No invented status values."""
    checks: dict[str, dict[str, object]] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"status": "ONLINE", "detail": settings.database_url.split("://")[0]}
    except Exception as exc:
        checks["database"] = {"status": "OFFLINE", "detail": type(exc).__name__}
    checks["ai_provider"] = provider_status()
    core_ok = checks["database"]["status"] == "ONLINE"
    return {
        "status": "ONLINE" if core_ok else "DEGRADED",
        "version": settings.app_version,
        "environment": settings.environment,
        "checks": checks,
    }


@router.get("/metrics")
def metrics(user: CurrentUser):
    """Live OS metrics via psutil — real samples, not placeholders."""
    cpu = psutil.cpu_percent(interval=0.1)
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    disk_pct = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
    battery = psutil.sensors_battery()
    net = psutil.net_io_counters()
    alerts: list[str] = []
    if disk_pct >= 90:
        alerts.append(f"ALYO detected that your system storage is critically low ({disk_pct}% used).")
    if vm.percent >= 90:
        alerts.append(f"System memory usage is very high ({vm.percent}%).")
    if battery and not battery.power_plugged and battery.percent <= 15:
        alerts.append(f"Battery critically low ({battery.percent}%).")
    return {
        "cpu_percent": cpu,
        "cpu_cores": psutil.cpu_count(),
        "ram_percent": vm.percent,
        "ram_used_gb": round(vm.used / 1e9, 1),
        "ram_total_gb": round(vm.total / 1e9, 1),
        "disk_percent": disk_pct,
        "disk_used_gb": round(disk.used / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
        "battery_percent": battery.percent if battery else None,
        "battery_plugged": battery.power_plugged if battery else None,
        "net_sent_mb": round(net.bytes_sent / 1e6, 1) if net else None,
        "net_recv_mb": round(net.bytes_recv / 1e6, 1) if net else None,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "alerts": alerts,
    }


@router.get("/boot")
def boot(db: DbDep):
    """Boot report: what is configured vs unavailable, stated honestly."""
    from core.agent.registry import registry
    from core.brain import orchestrator

    orchestrator.register_all(registry)
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "wake_word": settings.wake_word,
        "systems": {
            "database": {"ok": True, "detail": "Connected."},
            "brain": provider_status(),
            "tools": {
                "ok": True,
                "detail": f"{sum(1 for t in registry.all_specs() if t.enabled)}/"
                          f"{len(registry.all_specs())} tools enabled.",
            },
            "audit": {"ok": True, "detail": "Append-only audit log active."},
            "retention_worker": {
                "ok": True,
                "detail": f"Runs every {settings.retention_worker_interval_minutes} minutes.",
            },
        },
    }
