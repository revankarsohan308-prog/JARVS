# J.A.R.V.S — Architecture

**JUST A RATHER VERY INTELLIGENT SYSTEM** · Owner: **Sohan Revankar**

> Design rule: real functionality only. Every component is either working,
> honestly `NOT IMPLEMENTED` with a documented integration path, or reports
> "Integration not configured." with the exact environment variables it needs.
> No fake buttons, fake AI, fake microphone, fake payments, or fake security.

---

## 1. What J.A.R.V.S is

A personal & institutional AI command center that understands natural
language, remembers permitted information, creates and manages tasks, plans
multi-step "missions", executes **approved** tools behind a permission +
confirmation gate, and communicates naturally.

It serves many roles on one install: OWNER, SUPER_ADMIN, ADMINISTRATOR,
VICE_CHANCELLOR, FINANCE_OFFICER, DEPARTMENT_HEAD, LECTURER, TEACHER, STAFF,
STUDENT, SENIOR_USER, GENERAL_USER — each with its own permissions, tasks,
memory, and dashboards. **User A must never see User B's private data.**

## 2. Stack (chosen against this machine's real toolchain)

| Layer | Choice | Why (honest) |
|---|---|---|
| API + engine | Python **FastAPI** (existing, 109 tests, running) | Already real, tested, and owns auth/RBAC/tasks/memory/security/health |
| Desktop shell | **Electron** (Node 24 present) | Rust is **not installed**; Tauri cannot build here today. React/TS frontend is shell-agnostic, so Tauri is a drop-in later |
| Desktop UI | **React + TypeScript + Vite** (Node 24/npm 11) | Spec's preferred frontend |
| Web | `apps/web` (existing static UI against `/api/v1`) | Ship now, replace look with the shared React UI later |
| Database | **SQLite** local (dev, WAL) → **PostgreSQL** production | Connection string (`ALYO_DATABASE_URL`) is config-only; Postgres is a deployment change, not a rewrite |
| AI | Provider abstraction (`local` rules → `openai` → Claude etc.) | No key = honest local rules; key = real AI. No hidden charges |
| Voice | Real WS voice plane `backend/voice` (:8761): VAD → STT → Brain → TTS, same Brain as chat | Engine-gated: `ALYO_STT_PROVIDER` / `ALYO_TTS_PROVIDER` (see [docs/voice.md](voice.md)); offline engines answer `stt_offline`/`tts_offline` honestly. Wake-word *triggering* is a later phase |
| Payments | Abstraction only; no provider, no secrets | `NOT IMPLEMENTED` — real provider + webhook verification is owner-configured |

**Why not Tauri right now:** `rustc`/`cargo` are absent. Installing Rust +
MSVC toolchain is a ~2 GB, admin-required step that can fail silently on this
box. Electron gives a genuine installable Windows app today; the UI is built
so the shell can be swapped. (Decision recorded per "choose the safest
practical default and explain it".)

## 3. Repository structure

```
JARVS/
├── apps/
│   ├── web/            # browser UI (live today)
│   └── desktop/        # Electron + React/TS desktop foundation (Phase 2+)
├── api/v1/             # versioned REST API (auth, brain, tasks, memory, devices, …)
├── backend/voice/      # real voice plane — WS :8761, VAD/STT/Brain/TTS, device auth
├── core/               # identity, brain, memory, tasks, permissions, devices, pc …
├── database/           # SQLAlchemy models + session (Device model included)
├── config/             # pydantic-settings, ALYO_ env prefix (stable)
├── security/           # audit, monitoring, rate-limit, containment
├── firmware/           # J.A.R.V.S LINK V1 — ESP32-S3 (PlatformIO; pins = -1 until confirmed)
├── dev_tools/          # dev-only diagnostics page (404 in production)
├── docs/               # ARCHITECTURE, SECURITY, DATABASE, API, VOICE, HARDWARE, …
├── scripts/            # start/stop, firewall helper (port-8760, admin)
├── tests/              # 144 tests, pytest
├── .env                # git-ignored secrets/configuration
└── .env.example
```

Internal module names and the `ALYO_` env prefix are kept stable so the
existing `.env`, tests, and scripts keep working. Only the user-visible
identity is **J.A.R.V.S**.

## 4. Seed data (real accounts, first run)

- `demo-admin` / `demo-pass-123` — SUPER_ADMIN (owner bootstrap)
- Owner contact account: `revankarsohan308@gmail.com` (STUDENT) — the owner's
  real account can be promoted via RBAC, **never** via a hardcoded bypass.

## 5. Security model (already implemented)

- Pbkdf2 password hashing (configurable rounds; Argon2id is a settings swap)
- Server-side authorization on **every** endpoint; never trust the frontend
- RBAC roles + permission checks; tenant/user isolation on memory/tasks
- HttpOnly SameSite=Lax session cookie; bearer/`x-alyo-token` for device clients
- Rate limiting (login 5/60s, chat 30/60s, OAuth 20/300s) + denial windows
- Audit log + security-event scoring + suspicious-login detection
- Diagnostics that report status without ever exposing secrets
- No hardcoded passwords or keys anywhere

## 6. Cost model

| Component | Cost |
|---|---|
| Engine, DB, desktop shell, UI, local rules AI | **Free / open-source** |
| OpenAI real chat | **Paid** (your key; `ALYO_OPENAI_API_KEY`) — never claimed as free |
| Google/LinkedIn sign-in | Free, but the OAuth clients must be created in the owner's accounts |
| Payments | Provider fees only, once a provider is chosen (owner decision) |
| Voice STT/TTS | Local engines preferred; cloud engines would be provider-paid |

## 7. Milestones (phase plan from the spec)

1. ✅ Architecture + repository (**this doc**)
2. ◐ Desktop shell + futuristic UI (started: `apps/desktop`)
3. ▪ Authentication (exists via API — desktop login next)
4. ▪ Database (exists)
5. ▪ RBAC/security (exists, hardening next)
6. ▪ Task engine (exists; desktop screens next)
7. ▪ Long-term memory (exists)
8. ▪ AI brain/orchestrator (exists; tools/planner expansion)
9. ▪ Mission/agent engine (new)
10. ◐ Voice plane + wake word — backend **real + tested** (docs/voice.md);
    LINK firmware scaffolded (docs/hardware.md, compile check pending);
    **wake-word triggering** new/real-engine-only
11. ▪ Vision (new)
12. ▪ PC tools + file intelligence (new; allowlist first)
13. ▪ Notifications / automation (new)
14. ▪ Trial/subscription + payment abstraction (price config exists; webhook path new)
15. ▪ Owner Control Center (web exists; desktop panel next)
16. ▪ Windows installer (electron-builder)
17. ▪ Mobile API/client foundation (API-ready by design)

## 8. What is real today vs later

**Real now:** login/register, sessions, RBAC, tasks, reminders, memory, chat
(local rules, or real AI once key is set), security center, audit, health,
diagnostics, data export/delete, subscriptions config, **device pairing +
ownership (J.A.R.V.S LINK)**, **voice plane** (VAD→STT→Brain→TTS over a
token-authenticated WebSocket; engines honest-offline by default).

**NOT IMPLEMENTED (honest labels in the UI):** wake-word *triggering*, vision,
PC tool allowlist execution, notifications, payments, Android client.

## 9. Verification loop

Every phase: build → run tests (`pytest`) → run lint (`ruff`) → run the app →
UI check against the live API → commit. An error stops the phase until the
real cause is fixed. No "done" that isn't verified.