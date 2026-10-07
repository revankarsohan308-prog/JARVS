# Repair Report — ALYO stabilization pass

**Date:** 2026-10-06 · **Branch:** `repair/stabilize-v1` (from `master` @ `2bb1317`)
**Outcome:** 108/108 tests pass, Ruff clean. Root causes of the user-facing
failures identified and fixed at the *configuration and distribution* layer —
the engine itself was already real and tested.

---

## A. Root causes of the original problem

The user's three complaints were all real, and none of them meant "the project
is broken":

| Complaint | Root cause | Verified evidence |
| --- | --- | --- |
| "Chat doesn't answer like real AI" | No `.env` existed → `ALYO_LLM_PROVIDER=local` → the rule engine answered honestly ("no AI model is configured"). The OpenAI provider was already fully implemented. | `GET /api/v1/system/health` reported `ai_provider.provider=local` |
| "Can't even open/sign in to the app from my phone" | Server bound to `127.0.0.1` only. Additionally, a **stray second uvicorn process** was listening on the same port, so some requests hit old pre-fix code; and the user was clicking "Create account" (register) rather than "Sign in". | Two PIDs on port 8760 (16840 + 2492); log showed `POST /register 400` twice while login works |
| "Google login does nothing" | Google OAuth 2.0 + PKCE is fully implemented but **not configured** (no OAuth client credentials). The UI honestly showed "Integration not configured.", but there were no diagnostics to say *why* or *what to do*. | `GET /api/v1/auth/oauth/providers` → `configured=false` with exact env var names |

Two further issues found during the audit:

1. **Session token in `localStorage`** — the browser stored the bearer token
   where any XSS can steal it. Moved to an HttpOnly, SameSite=Lax cookie.
2. **UI stated "online" without proof** — the connection badge was hard-set to
   "online" after login even if the backend died. Replaced with a live
   heartbeat against the real health endpoint.

No fake login, fake AI, or fake success was ever present, and none was added.

---

## B. Files changed

- `config/settings.py` — unchanged (already had everything needed)
- `api/v1/auth.py` — login/register now set the `alyo_session` HttpOnly cookie;
  logout reads the cookie and clears it
- `api/v1/oauth.py` — OAuth success callback also sets the `alyo_session`
  cookie (browser no longer depends on the URL fragment)
- `api/deps.py` — `current_user` resolves sessions from the cookie as well as
  from bearer / `x-alyo-token` headers (device clients unchanged)
- `api/v1/system.py` — new `GET /api/v1/system/diagnostics` (status only, never
  secrets) and AI status field enrichment
- `api/app.py` — gated `/dev/diagnostics` page route (404 in production)
- `apps/web/app.js` — removed `localStorage` session storage; added live
  connection heartbeat (online/degraded/offline), AI status chip, auth-screen
  status strip
- `apps/web/index.html` — added `ai-chip` badge and `auth-status` strip
- `apps/web/styles.css` — badges for online/degraded/offline/local AI, auth
  card polish, status dots
- `tests/conftest.py` — tests pinned to the local brain so a developer `.env`
  can never change test outcomes
- `docs/SETUP.md`, `docs/TROUBLESHOOTING.md` — updated with real fixes
- `.env` — **created** (git-ignored) with `ALYO_APP_HOST=0.0.0.0` and
  `ALYO_LLM_PROVIDER=openai`; the API key is left blank for the owner to paste

## C. Files created

- `docs/REPAIR_REPORT.md` — this report
- `tests/test_diagnostics.py` — diagnostics honesty + dev gating tests
- `tests/test_cookie_session.py` — cookie-session lifecycle tests
- `scripts/stop-alyo.ps1` — stops stale listeners on the ALYO port
- `scripts/start-alyo.bat` — stop-then-start helper
- `dev_tools/diagnostics.html` — the diagnostics page body

## D. Dependencies added/removed

None. The stack (FastAPI, SQLAlchemy, SQLite, pydantic-settings, httpx,
psutil, uvicorn, pytest, ruff) is unchanged.

## E. Authentication architecture

- Password registration/login: PBKDF2-HMAC-SHA256 (per-user salt); sessions are
  random 256-bit tokens stored only as SHA-256 digests.
- **Transport:** the browser carries the token in an `alyo_session` cookie
  (`HttpOnly`, `SameSite=Lax`, `Secure` in production, `Path=/`). Device and
  gateway clients keep using `Authorization: Bearer` or `x-alyo-token`.
- Social sign-in: real OAuth 2.0 authorization-code + PKCE (S256), CSRF-bound
  `state` cookie, verified-email account linking, per-outcome audit events.
- RBAC: 7 roles (`SUPER_ADMIN`…`GUEST`); every permission enforced server-side.
- No authentication token is ever stored in `localStorage`.

## F. Google OAuth configuration required

1. Google Cloud Console → Credentials → Create **OAuth client (Web)**.
2. Authorized redirect URI: `http://<host>:8760/api/v1/auth/oauth/google/callback`
   (exact value is always visible via `GET /api/v1/system/diagnostics`).
3. Set in `.env`: `ALYO_GOOGLE_CLIENT_ID`, `ALYO_GOOGLE_CLIENT_SECRET`.
4. Restart. The button flips from "not configured" to a working Google sign-in.

Until credentials exist, ALYO states exactly this and never fakes a login.

## G. Environment variables required

See `.env.example`. The ones that matter for the reported problems:

- `ALYO_APP_HOST=0.0.0.0` — reach ALYO from other devices on the network
- `ALYO_LLM_PROVIDER=openai` + `ALYO_OPENAI_API_KEY` — real AI replies
- `ALYO_GOOGLE_CLIENT_ID` / `ALYO_GOOGLE_CLIENT_SECRET` — Google sign-in
- `ALYO_OPENAI_MODEL`, `ALYO_OPENAI_BASE_URL` — provider tuning (defaults set)

## H. Database changes

None required — `data/alyo.db` was preserved. The schema (users, sessions,
memories, tasks, audit, security events, OAuth identities) already matched the
design. No migrations were needed for this pass.

## I. Security improvements

- Session token moved out of `localStorage` into an HttpOnly SameSite=Lax
  cookie (mitigates XSS token theft; blocks cross-site cookie sends).
- OAuth callback now lands a cookie session too — no dependence on token in URL.
- Diagnostics endpoint and page never emit secret values (covered by tests).
- `/dev/diagnostics` is 404 in production.
- Connection status is now *true*: heartbeat polls the health endpoint instead
  of hard-coding "online".
- `scripts/stop-alyo.ps1` resolves the duplicate-listener confusion that made
  the app appear broken.

## J–L. Tests performed

- Full suite: **108 passed, 0 failed** (was 98; +10 new).
- `ruff check .`: clean.
- New tests cover: diagnostics report correctness without secret leakage,
  diagnostics dev-gating, AI provider honesty (local vs degraded-to-openai),
  cookie session set/use/logout lifecycle, OAuth callback cookie session.
- Server started on `0.0.0.0:8760`; `/health`, `/system/diagnostics`, login,
  `/auth/me` verified live (details withheld here — no secrets in this report).

## M. Remaining configuration (what the owner must do)

| Item | Where | Action |
| --- | --- | --- |
| Real AI chat | `.env` → `ALYO_OPENAI_API_KEY` | Paste an OpenAI API key (platform.openai.com, billing enabled). Restart. |
| Phone access | Windows Firewall | Allow inbound TCP 8760; open `http://<LAN-IP>:8760` (see `ipconfig`). |
| Google sign-in | `.env` → Google client id/secret | Create the OAuth client with the redirect URI from `/system/diagnostics`. |
| LinkedIn sign-in | `.env` → LinkedIn client id/secret | Create the app + OIDC product; same pattern. |

Everything else (memory, tasks, security monitoring, privacy, retention,
audit) runs and has been verified by the test suite and live checks.

## N. Exact commands to start ALYO

```powershell
cd C:\Users\User\Projects\ALYO
.\.venv\Scripts\python main.py
# or the helper (stops stale listeners first):
powershell -ExecutionPolicy Bypass -File scripts\start-alyo.bat
```

## O. Exact browser URL

- This PC: `http://127.0.0.1:8760`
- Phone/other devices (same Wi-Fi): `http://<YOUR-LAN-IP>:8760`
- API docs: `http://127.0.0.1:8760/docs`
- Diagnostics (dev only): `http://127.0.0.1:8760/dev/diagnostics`

## P. Exact steps to test Google login

1. Start ALYO and open the UI.
2. Confirm `/dev/diagnostics` shows Google **CONFIGURED** (or follow F above).
3. Click **Google** on the sign-in screen → provider consent page → back to ALYO.
4. Assert you are signed in (`/auth/me` returns your user) and an
   `oauth_login_success` audit event exists.

## Q. Known limitations (stated honestly)

- Real cloud AI chat is **not active until an API key is pasted**; until then
  the local rule engine answers and every surface says so.
- This pass verified LAN reachability and the app/host binding; it does **not**
  deploy to the public internet (that needs a hosting account / reverse proxy,
  and setting `ALYO_PUBLIC_BASE_URL`).
- Emergency, device-control, and payment features are intentionally not
  simulated — they remain outside Milestone 1 and report non-configured states.
- The repair branch has not been merged to `master`; do that deliberately.

---

**Verdict:** the foundation was stable code + missing configuration. The
foundation is now wired correctly (real AI key slot, phone reachability,
honest diagnostics), the session transport was hardened, and the whole repair
is covered by 108 passing tests. What remains is owner-supplied credentials,
which ALYO will refuse to fake.