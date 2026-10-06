# ALYO Architecture

Milestone 1 (ALYO Core) — layered monolith, no external services required.

## Layers

```
┌─────────────────────────────────────────────────────────┐
│  apps/web            Single-page UI (vanilla JS)        │
├─────────────────────────────────────────────────────────┤
│  api/                FastAPI app + versioned routers    │
│    api/v1/*          auth, brain, memory, tasks,        │
│                      system, security, privacy         │
│    api/deps.py       authentication + RBAC guards       │
├─────────────────────────────────────────────────────────┤
│  core/               Domain logic                       │
│    identity          password hashing, sessions         │
│    permissions       pure RBAC functions                │
│    memory            owner-scoped store, secret guard   │
│    tasks             CRUD, recurrence, reminders        │
│    agent             tool registry + permission gates   │
│    brain             orchestrator, providers, rules     │
├─────────────────────────────────────────────────────────┤
│  security/           audit (append-only), rate limits,  │
│                      threat/bug monitoring              │
│  retention.py        Retention & deletion engine (§16)  │
├─────────────────────────────────────────────────────────┤
│  database/           SQLAlchemy models + engine (SQLite)│
│  config/             Settings (ALYO_ env prefix)        │
└─────────────────────────────────────────────────────────┘
```

**Dependency rule:** `api → core → security/database/config`. Lower layers never
import FastAPI. `security/` may be called from any layer but depends only on
`database/` and `config/`.

## Request lifecycle (chat example)

1. `POST /api/v1/brain/chat` → `api/deps.current_user` resolves the bearer
   token to a `User` row (only token digests are stored).
2. `orchestrator.handle_message`:
   - rate limit check (per-user, sliding window),
   - conversation turn saved (unless the user disabled history),
   - wake word stripped (`Hey Jarvis,` / `ALYO`),
   - **rule engine** handles deterministic intents offline
     (greetings, status, time, remember/recall, tasks, confirmations),
   - otherwise the **provider** runs (`local` fallback or `openai`) and may call
     tools through the registry for a bounded number of rounds,
   - if neither can answer honestly, `_fallback_reply` states that no AI model
     is configured and how to configure one — it never invents an answer.
3. Every tool call goes through **one path**: `registry.execute_tool`, which
   checks availability → RBAC permission → confirmation requirement, executes,
   audits, and returns an `ExecutionResult` with `verified` / `ok` flags.

## Key invariants

- **REAL > DEMO.** Handlers return `verified: True` only when work actually ran.
  Unavailable integrations are registered *disabled* with their exact
  configuration requirement.
- **One tool path.** No route or brain branch may call domain logic that skips
  `execute_tool`; permissions cannot be bypassed.
- **Owner scoping.** Memory, tasks, conversations and confirmations are filtered
  by `user_id` in the service layer, not just the route layer.
- **Append-only audit.** `security/audit.py` installs ORM hooks that raise on
  any UPDATE/DELETE of `audit_logs`; secrets in details are redacted before write.
- **Fail closed.** Unknown role or permission → no permission.

## Extension points

| To add… | Do this |
| --- | --- |
| A new tool | Write a handler in `core/agent/catalog.py`, register a `ToolSpec` with a `required_permission` and `PermissionLevel`, then add a rule-engine intent (optional) in `orchestrator._rule_engine`. Permission, confirmation, audit and `verified` handling come for free. |
| A new route | Create `api/v1/<name>.py` with an `APIRouter`, guard with `Annotated[User, Depends(require_permission(Permission.X))]`, mount in `api/app.py`. |
| A new role/permission | Extend `Role`/`Permission` and the map in `core/permissions/rbac.py`; monotonic tests in `tests/test_rbac.py` enforce least-privilege ordering. |
| A new data category | Add the model in `database/models.py`, retention rule in `retention.py`, policy entry in `api/v1/privacy.py::data_retention_policy`. |
| Another database | Create a new engine in `database/session.py`; models are portable SQLAlchemy 2.0. |
| Another LLM | Subclass `LLMProvider` in `core/brain/providers.py` and return it from `get_provider`. |

## Configuration

`config/settings.py` (pydantic-settings) reads `.env` with the `ALYO_` prefix.
The database URL is resolved relative to the project root so relative paths in
`.env` behave the same from any working directory.

## Concurrency & failure

- SQLite runs in WAL mode with foreign keys on.
- Rate limiting and denial tracking are in-process (single-process deployment);
  both degrade to fail-closed limits if the counter store is cleared.
- The retention worker is an asyncio task started in the FastAPI lifespan;
  every iteration is wrapped so one failure never kills it.
- Unhandled exceptions become a **bug-detector event** (`security/monitoring.py`)
  and the client receives a generic 500 with an `error_ref`, never a traceback.
