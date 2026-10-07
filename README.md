# J.A.R.V.S — Just A Rather Very Intelligent System

**J.A.R.V.S** is a personal & institutional AI assistant platform with a security-first foundation — a "smart brain + free hands + long-term memory + safe actions" command center for its owner, **Sohan Revankar**, and its users (students → lecturers → staff → finance → administration → VC → senior users).
Wake word: **"Hey Jarvis"**. The engine implements the working core — real authentication, RBAC, tasks, memory, security center, health checks — plus the baseline security layer from the specification.

> **Design rule:** real functionality only. Every tool result carries a
> `verified` flag; anything not wired to a real integration answers
> *"Integration not configured."* and tells you exactly what it needs.

## Product layout

- `apps/web` — browser UI (works today against `/api/v1`)
- `apps/desktop` — **J.A.R.V.S desktop app** (Electron shell, React + TypeScript; Phase 2)
- `api/`, `core/`, `database/` — the FastAPI engine (real, tested)
- `docs/ARCHITECTURE.md` — architecture, security model, milestones, cost model

## What works today (Milestone 1)

| Area | Status |
| --- | --- |
| Auth: register / login / logout / sessions | ✅ real, PBKDF2-hashed passwords, 256-bit tokens (only SHA-256 digests stored); username may be your email |
| Social sign-in (Google, LinkedIn) | ✅ real OAuth 2.0 + PKCE; unconfigured providers say "Integration not configured." with the exact setup steps |
| RBAC: 7 roles, permission matrix, denial evidence | ✅ least-privilege, denials audited, 5 denials/5 min → scored threat event |
| Chat + rule-engine brain | ✅ wake-word stripping, greetings, help, time/status/health, memory & task intents |
| LLM provider | ✅ honest local fallback (no key needed); OpenAI provider when `ALYO_OPENAI_API_KEY` is set |
| Conversation context | ✅ per-user history window, toggleable, deletable |
| Long-term memory | ✅ owner-scoped store/search/update/delete; secrets rejected and audited |
| Tasks | ✅ CRUD, due dates, recurrence, reminders, chat-driven creation |
| Tool registry | ✅ permission gates + human confirmation for destructive actions |
| System health | ✅ live psutil metrics (CPU/RAM/disk/battery/network) |
| Security monitoring | ✅ bug + threat detection with 0–100 risk scores, containment, incident lifecycle |
| Audit log | ✅ append-only (update/delete rejected at the ORM layer), secret redaction |
| Privacy & retention | ✅ retention policy view/update, data export, truthful deletion statuses, idempotent retention worker |
| Web interface | ✅ clean single-page UI at `/` (chat, tasks, memory, system, security, privacy) |

### Honest limitations

- Voice input/output, mobile apps, and external service integrations are **not
  implemented** — endpoints report `Integration not configured.` with the
  required configuration instead of faking success.
- Emergency features are **cyber vs. physical aware**: ALYO never places
  emergency calls (`auto_contact_enabled = false`). India reference number: **112**.
- SQLite is the Milestone 1 database; the engine layer is swappable.

## Quick start

```powershell
# Windows (PowerShell)
cd C:\Users\User\Projects\ALYO
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env        # optional — sensible defaults work out of the box
.\.venv\Scripts\python main.py
```

Open <http://127.0.0.1:8760>. The **first account you register becomes the
SUPER_ADMIN**; every later registration starts as STUDENT.

```powershell
# Run the test suite (98 tests) and the linter
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
```

## Configuration

All settings use the `ALYO_` prefix (see `.env.example`), for example:

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALYO_APP_SECRET_KEY` | *empty* | Session signing key — **set this in production** |
| `ALYO_GOOGLE_CLIENT_ID` / `_SECRET` | *empty* | Enables **Sign in with Google** (real OAuth 2.0; see docs/SETUP.md) |
| `ALYO_LINKEDIN_CLIENT_ID` / `_SECRET` | *empty* | Enables **Sign in with LinkedIn** |
| `ALYO_LLM_PROVIDER` | `local` | `local` (offline rules) or `openai` |
| `ALYO_OPENAI_API_KEY` | *empty* | Enables real LLM replies |
| `ALYO_RATE_LIMIT_CHAT_PER_MINUTE` | `30` | Per-user chat limit |
| `ALYO_RETENTION_CONVERSATIONS_DAYS` | `90` | Conversation auto-deletion window |
| `ALYO_AUTO_CONTAINMENT_ENABLED` | `true` | Revoke sessions on risk ≥ 60 |

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, modules, data flow, extension points |
| [docs/API.md](docs/API.md) | Versioned `/api/v1` endpoints and response shapes |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, RBAC, scoring, incident lifecycle |
| [docs/SETUP.md](docs/SETUP.md) | Installation, configuration, first run |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Conventions, testing, adding a tool/route |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running ALYO as a service, hardening checklist |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common failures and their real fixes |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestone plan and current status |

## Repository layout

```
ALYO/
├── api/              FastAPI app factory + versioned routers (/api/v1)
├── apps/web/         Single-page web interface (no build step)
├── config/           Settings (ALYO_ prefixed, .env aware)
├── core/             identity, permissions (RBAC), memory, tasks, agent, brain
├── database/         SQLAlchemy models + engine
├── security/         audit (append-only), rate limiting, monitoring
├── retention.py      Retention/deletion engine (spec §16)
├── tests/            78 pytest tests
└── docs/             Documentation set
```
