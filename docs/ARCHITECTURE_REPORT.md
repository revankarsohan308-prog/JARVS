# NEXORA - Production Architecture Report

**Date**: 2026-10-08  
**Repository**: JARVS (ALYO-origin)  
**Owner**: Sohan Revankar  
**Target**: NEXORA Migration (Real, Verified, No Fakes)

## 1. Repository Inspection Summary

- **Python files**: 75
- **TSX files**: 2 (desktop React UI)
- **Tests**: 150 passed
- **Linting**: ruff - all checks passed
- **Existing stack**: FastAPI + SQLAlchemy + Uvicorn (backend), React + Vite + Electron (desktop), SQLite for dev (ALYO_DATABASE_URL defaults to sqlite:///./data/alyo.db)
- **Voice stack**: backend/voice/ with WS server, STT/TTS engines (real), honest offline states
- **Auth/RBAC/Tasks/Memory/Devices**: Fully implemented and tested (real)
- **Alembic**: Installed (1.15.2) and configured with database/migrations/ (env.py updated to use Base.metadata)

## 2. Current Structure (Working)

- `api/` - REST API endpoints (v1)
- `core/` - Brain, orchestrator, permission, context, mission
- `backend/voice/` - Real voice server (port 8761)
- `database/` - Models, session
- `security/` - Auth, audit, RBAC
- `tasks/`, `memory/`, `devices/` - Core domains
- `apps/desktop/` - Electron desktop app (real, compiles)
- `apps/web/` - Web UI
- `config/` - Settings (ALYO_ prefixed)
- `tests/` - Comprehensive test suite (150 passing)
- `docs/` - Architecture, API, deployment docs

## 3. ALYO → NEXORA Branding Audit

**Search results**: ALYO/Alyo/alyo found across codebase. 
Need to classify:
- Product brand references (e.g., "ALYO API", "ALYO configuration", .env ALYO_* prefix) - need migration
- Code identifiers (class names, log messages) - evaluate case by case
- Historical docs/comments - may keep if clearly historical
- Database/schema naming - careful migration via Alembic

**Priority**: Migrate user-facing (UI, docs, assistant identity), keep internal technical names where not breaking; add backward compatibility.

## 4. Phase 1 Status

**IMPLEMENTED**:
- [x] Repository inspection complete
- [x] Tests passing (150/150)  
- [x] Lint clean
- [x] Alembic installed and configured
- [x] pyproject.toml created
- [x] PHASES.md created
- [x] Migrations skeleton (alembic.ini + env.py + versions/)

**REMAINING FOR PHASE 1**:
- [ ] Extend health check foundations (real checks)
- [ ] Create comprehensive .env.example aligned with NEXORA (maintain ALYO_ compatibility if needed)
- [ ] Document migration plan
- [ ] Final Phase 1 verification report

## 5. Next Steps (Phase 1 Completion)

1. Add health check endpoints (readiness/liveness) with real checks
2. Update .env.example with NEXORA naming (keep ALYO_ as primary for compatibility, add NEXORA_ as aliases? Or migrate)
3. Generate initial migration if models changed - baseline exists
4. Run full verification
5. Report Phase 1 COMPLETE

**Constraints**: No fake functionality, preserve data, preserve working behavior.
