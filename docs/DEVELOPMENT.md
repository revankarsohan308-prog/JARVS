# Development

## Commands

```powershell
.\.venv\Scripts\python -m pytest -q          # test suite (94 tests)
.\.venv\Scripts\python -m pytest tests/test_brain.py -q
.\.venv\Scripts\python -m ruff check .       # lint (clean baseline)
.\.venv\Scripts\python -m ruff check . --fix # auto-fix
.\.venv\Scripts\python main.py               # run dev server
```

## Conventions

- **Python 3.11**, `from __future__ import annotations`, type hints on public
  functions, Ruff-clean.
- Routers are thin: parse → call service → serialize. Business rules live in
  `core/`, never in route bodies.
- Guards use `Annotated[User, Depends(require_permission(Permission.X))]` —
  never call `require_permission` as a default value.
- Datetimes: **naive UTC everywhere** in storage (`timeutil.py`); API responses
  append `Z`; human-facing chat text shows local time (`catalog.format_due`).
- Errors users see go through `HTTPException(4xx, detail=…)`,
  `TaskError`/`MemoryRejected`, or `ExecutionResult.message` — never raw
  tracebacks.

## Testing

`tests/conftest.py` gives you:

| Fixture | What it does |
| --- | --- |
| `client` | Fresh `TestClient`, limiter + denial windows reset |
| `db` | Fresh schema (drop/create) per test |
| `admin_session` | First registration → bootstrap SUPER_ADMIN (`admin`) |
| `student_session` | Second account (`sohan`) → STUDENT |

Test rules:

- Assert **real effects** (row gone from DB, session revoked, honest status
  string), not just status codes.
- Every security claim in a test must reference the event/audit row that backs it.
- When a test needs isolation, reset global state via `limiter.reset()` and
  `reset_denial_windows()` (both already in the `client` fixture).

## Adding a tool (checklist)

1. Handler in `core/agent/catalog.py`:
   `def _thing(db, args, *, user_id, role) -> dict` returning `{"verified": True, ...}`.
2. Register a `ToolSpec(name, description, required_permission, permission_level, handler)`.
3. Optional: rule-engine shortcut in `orchestrator._rule_engine` that calls
   `_run_tool(db, user, "tool.name", args, response)`.
4. Test: happy path, wrong-role denial (403/`denied_reason`), and — if
   `CONFIRMATION_REQUIRED` — that nothing runs before human approval.
5. If unavailable in this build: register with `available=False` and an exact
   `unavailable_reason`; never simulate results.

## Adding a route (checklist)

1. `api/v1/<name>.py` with `APIRouter(prefix="/…", tags=[…])`.
2. Guard every data access with `CurrentUser` (own data) or
   `require_permission(...)` (privileged data).
3. Mount in `api/app.py`.
4. Test authorized path, 401 without token, 403 without permission, isolation
   from other users' rows.
5. Document the endpoint in `docs/API.md`.

## Adding a provider (social sign-in)

OAuth providers live in one registry — `core/identity/oauth.py::PROVIDERS`:

1. Add a `Provider(...)` entry: display name, authorization/token/userinfo
   endpoints, scopes, and the env var names for its client id/secret
   (add those two fields to `config/settings.py` and `.env.example`).
2. Add a button with `data-provider="<id>"` in `apps/web/index.html` (the UI
   picks it up automatically from `GET /auth/oauth/providers`).
3. No route changes needed — `/{provider}/start` and `/callback` are generic.
4. Test with `tests/test_oauth.py` as the template (stub `_post_json` /
   `_get_json`; never hit the real network in tests).

## Code style notes

- Ruff is the only linter/formatter gate.
- Keep secrets out of code: read `settings`, never literals.
- Audit every state change: `audit(db, event, detail, actor=…, component=…)`.
- Detail strings must never contain secret values (redaction helps, but do not
  rely on it).
