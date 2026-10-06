"""FastAPI application factory.

Responsibilities beyond routing:
- Bug detector (spec §15.2): every unhandled exception becomes a SecurityEvent
  with component, severity, cause and recommended action.
- Repeated API failures (spec §15.2): 5xx responses are counted per path; a
  threshold opens a real `api_failure` event.
- Retention worker (spec §16.7): periodic, idempotent purge of expired data.
- Static delivery of the ALYO web interface at /.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config.settings import PROJECT_ROOT, settings
from core.agent.registry import registry
from core.brain import orchestrator
from database.session import SessionLocal, init_db
from security import monitoring
from security.audit import audit

logger = logging.getLogger("alyo")

# Per-path counter of consecutive/recent 5xx responses.
_api_failures: dict[str, list[float]] = {}
_API_FAILURE_WINDOW = 300.0
_API_FAILURE_THRESHOLD = 3


def create_app() -> FastAPI:
    init_db()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        orchestrator.register_all(registry)
        db = SessionLocal()
        try:
            audit(db, "app_started", f"version={settings.app_version}", component="core")
        finally:
            db.close()
        _record_startup_findings()
        worker = asyncio.create_task(_retention_worker())
        yield
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        db = SessionLocal()
        try:
            audit(db, "app_stopped", component="core")
        finally:
            db.close()

    app = FastAPI(
        title="ALYO",
        version=settings.app_version,
        description=(
            "ALYO personal assistant platform. Milestone 1: core, memory, tasks, "
            "brain, security baseline."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8760", "http://localhost:8760"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Bug detector: unhandled exceptions -> SecurityEvent (§15.2)
    # ------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        path = request.url.path
        component = _component_for(path)
        db = SessionLocal()
        try:
            event = monitoring.record_bug_event(
                db,
                component=component,
                error_type="unhandled_exception",
                description=f"{type(exc).__name__} while handling {request.method} {path}: {exc}",
                evidence=[f"method={request.method}", f"path={path}"],
            )
            event_id = event.id
        except Exception:  # pragma: no cover - detector must never mask the error
            event_id = "unknown"
            logger.exception("failed to record bug event")
        finally:
            db.close()
        logger.exception("unhandled exception on %s %s", request.method, path)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error. The failure was recorded in the security log.",
                "error_ref": event_id,
            },
        )

    # ------------------------------------------------------------------
    # 5xx tracking -> repeated api_failure events (§15.2)
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def track_api_failures(request: Request, call_next):
        response = await call_next(request)
        if response.status_code >= 500:
            now = time.monotonic()
            path = request.url.path
            bucket = _api_failures.setdefault(path, [])
            bucket[:] = [t for t in bucket if now - t < _API_FAILURE_WINDOW]
            bucket.append(now)
            if len(bucket) == _API_FAILURE_THRESHOLD:
                db = SessionLocal()
                try:
                    monitoring.record_bug_event(
                        db,
                        component=_component_for(path),
                        error_type="api_failure",
                        description=(
                            f"{len(bucket)} server errors on {path} within "
                            f"{int(_API_FAILURE_WINDOW)}s."
                        ),
                        evidence=[f"path={path}", f"window={int(_API_FAILURE_WINDOW)}s"],
                    )
                except Exception:  # pragma: no cover
                    logger.exception("failed to record api_failure")
                finally:
                    db.close()
        elif response.status_code < 500:
            _api_failures.pop(request.url.path, None)
        return response

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    from api.v1 import (
        auth,
        brain,
        oauth,
        privacy,
        security,
        system,
        tasks,
    )

    prefix = "/api/v1"
    for module in (auth, brain, oauth, privacy, security, system, tasks):
        app.include_router(module.router, prefix=prefix)

    from api.v1 import memory as memory_routes

    app.include_router(memory_routes.router, prefix=prefix)

    @app.get("/api/v1")
    def api_root():
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "api": "v1",
            "docs": "/docs",
        }

    # Web interface (served only if present).
    web_dir = PROJECT_ROOT / "apps" / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


def _component_for(path: str) -> str:
    if path.startswith("/api/v1/auth"):
        return "auth"
    if path.startswith("/api/v1/brain"):
        return "brain"
    if path.startswith("/api/v1/memory"):
        return "memory"
    if path.startswith("/api/v1/tasks"):
        return "tasks"
    if path.startswith("/api/v1/security"):
        return "security"
    if path.startswith("/api/v1/privacy"):
        return "privacy"
    if path.startswith("/api/v1/system"):
        return "system"
    return "api"


async def _retention_worker() -> None:
    """Idempotent scheduled purge (spec §16.7). Runs until cancelled."""
    interval = max(settings.retention_worker_interval_minutes, 1) * 60
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_run_retention_once)
        except Exception:  # pragma: no cover - worker must not die silently
            logger.exception("retention worker iteration failed")


def _run_retention_once() -> dict:
    from retention import apply_retention

    db = SessionLocal()
    try:
        result = apply_retention(db)
        if any(v for k, v in result.items() if isinstance(v, int)):
            audit(db, "retention_cleanup", f"result={result}", component="privacy")
        return result
    finally:
        db.close()


def _record_startup_findings() -> None:  # pragma: no cover - used by scripts
    db = SessionLocal()
    try:
        if not settings.openai_api_key and settings.llm_provider == "openai":
            monitoring.record_bug_event(
                db,
                component="brain",
                error_type="config_error",
                description="LLM provider is 'openai' but ALYO_OPENAI_API_KEY is empty.",
                evidence=["ALYO_OPENAI_API_KEY missing"],
            )
    finally:
        db.close()


app = create_app()
