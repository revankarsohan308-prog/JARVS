# Troubleshooting

Real symptoms → real causes. ALYO itself reports problems the same way:
check `GET /api/v1/security/status` and the Security panel first — bug-detector
events name the component, likely cause and suggested action.

## Server won't start

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: fastapi` | Wrong interpreter / venv not installed | `.\.venv\Scripts\python -m pip install -r requirements.txt` |
| `address already in use` | An older server still holds port 8760 | `Get-NetTCPConnection -LocalPort 8760 \| ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }` (or change `ALYO_APP_PORT`) |
| `sqlite3.OperationalError: database is locked` | Two processes writing the same DB | Run one instance; WAL is enabled but single-writer still applies |
| Settings error mentioning `.env` | Malformed `.env` line | Fix or delete `.env` — defaults work without it |

## Can't log in

1. **401 on everything** — token expired/revoked: sign in again
   (Sign out → Sign in). Tokens are stored only as digests; re-login is the
   only fix by design.
2. **429 "Too many attempts"** — the login limiter or brute-force detector
   tripped (5 failures/minute per user+host). Wait for the `Retry in Ns`
   window; the breach is recorded as a scored security event.
3. **First account on a fresh DB is not you** — the *first* registration on an
   empty database becomes SUPER_ADMIN. If you are testing repeatedly, delete
   `data/alyo.db` (stops server first) to reset.

## 403 on an endpoint

Not a bug: the route requires a permission your role lacks. The message names
the missing permission (e.g. `Requires task:delete (STAFF+).`). Five denials in
five minutes intentionally raise a `permission_bypass_attempt` event.
Check `GET /api/v1/auth/roles` for your role's matrix.

## Chat replies "no AI model is configured"

Honest fallback: `ALYO_LLM_PROVIDER` is `local` (default) or `openai` without
a key. Set `ALYO_OPENAI_API_KEY` in `.env` and restart — the System panel shows
the active provider mode. Everything else (status, time, memory, tasks) works
offline by design.

## A tool says "Integration not configured."

That capability is registered but intentionally disabled. The message lists the
exact setting required. ALYO will not simulate results.

## Tests

| Symptom | Cause | Fix |
| --- | --- | --- |
| Failures cascade between runs | Stale dev DB confusion | Tests use `data/test_alayo.db` and drop/create per test — just rerun; delete the file if it is corrupted |
| Rate-limit tests fail | A server is irrelevant — limits reset per test via `limiter.reset()` | Ensure you did not edit `tests/conftest.py` |
| `Permission denied` writing DB | Read-only directory / OneDrive sync | Move the project out of synced folders or fix ACLs |

## Web UI shows "offline"

- Server not running → start `main.py`.
- Token expired → UI falls back to the sign-in screen automatically.
- Open browser dev tools → Console: JS errors are surfaced there; the UI never
  fabricates data — panels show an explicit "unavailable" reason instead.

## Windows-specific

- **OneDrive-synced paths** can lock `data/alyo.db`; keep the project on a
  local disk for development.
- `psutil.sensors_battery()` may return `None` on desktops — battery cards are
  then omitted rather than faked.

## Getting help from the system

```powershell
Invoke-RestMethod http://127.0.0.1:8760/api/v1/system/health | ConvertTo-Json -Depth 5
```

- `checks.database` ≠ ONLINE → storage/permissions issue above.
- Security panel events carry `what_happened / why_flagged / what_alyo_did /
  what_you_can_do` — start there for anything unexplained.
