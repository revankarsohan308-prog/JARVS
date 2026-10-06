# ALYO API (v1)

Base path: `/api/v1` · Interactive docs: `/docs` · Auth: `Authorization: Bearer <token>`
(also accepted: `x-alyo-token`). Errors are JSON: `{"detail": "…"}`.

Status codes: `401` missing/invalid token · `403` RBAC denial (audited) ·
`404` not found *or* not yours (no existence leak) · `409` conflict ·
`422` validation/business rule · `429` rate limited.

## Auth — `/auth`

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/register` | `{username, password, email?, display_name?}` → 201. Username may be a plain name or **an email address** (normalised to lowercase). **First account = SUPER_ADMIN** (`bootstrap_admin: true`); later accounts forced to STUDENT. Open registration can be closed with `ALYO_ALLOW_OPEN_REGISTRATION=false`. |
| POST | `/login` | → `{token, expires_at, user}`. Rate-limited; failures feed the brute-force detector. |
| POST | `/logout` | Revokes the presented token. |
| GET | `/me` | Current identity incl. privacy toggles. |
| GET | `/sessions` | Your active sessions. |
| DELETE | `/sessions/{id}` | Revoke one of your sessions. |
| POST | `/sessions/revoke-all` | Revoke every session incl. current. |
| GET | `/roles` | Role → permission matrix (transparency). |
| POST | `/users/{id}/role` | Requires `USER_MANAGE`. Audited `role_changed`. |

### Social sign-in — `/auth/oauth`

Real OAuth 2.0 authorization-code flow with PKCE (CSRF `state` bound to an
HttpOnly SameSite cookie). Unconfigured providers report
*"Integration not configured."* + the exact `.env` variables — never a fake button.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/providers` | Public. Per provider: `configured`, `redirect_uri`, `scopes`, and `note` (setup instructions when not configured). Never exposes client ids/secrets. |
| GET | `/{provider}/start` | 302 → provider consent screen. Requires configured credentials (else 400 with setup instructions). Rate-limited 20/5 min per host. |
| GET | `/{provider}/callback` | Validates `state` (400 + scored `oauth_state_mismatch` threat event on mismatch; 429 after 5 failures/5 min), exchanges the code, resolves the account (known subject → login; **provider-proven** email → link; else auto-provision as STUDENT unless the DB is empty → bootstrap SUPER_ADMIN), issues a session and redirects to `/#alyo_token=…` (fragment — never in server logs). Provider cancellation or refusal → `/?oauth_error=…` + audit, no session. |

## Brain — `/brain`

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/chat` | `{message, source?}` → `{text, tools[], needs_confirmation, confirmation_id, confirmation_summary, source, context_used}`. `tools[]` entries carry `verified`. Rate-limited per user. |
| GET | `/history?limit=` | Your turns + `history_enabled`. |
| DELETE | `/history` | Delete your conversation history (audited, count only). |
| POST | `/confirmations/{id}` | `{confirmation_id, approve}` — approve executes the tool with `confirmed=true`; deny runs nothing. Owner-scoped (404 for others). |
| GET | `/tools` | Tool catalog with `permission_level`, `your_role_allowed`, `needs_confirmation`, `unavailable_reason` — honest capability view. |

## Memory — `/memory`

Owner-scoped. Sensitive content (passwords/keys/tokens) is rejected with 422
and audited as `memory_rejected_sensitive`.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/` | List + per-user stats. |
| POST | `""` | `{key, content, category?, importance?}` → 201; same key overwrites. |
| GET | `/search?q=` | Substring search within *your* memories. |
| GET | `/export` | JSON export of your memories (audited). |
| PATCH | `/{id}` | Update key/content/category/importance. |
| DELETE | `/{id}` | `"Deletion completed."` only when really deleted. |

## Tasks — `/tasks`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/?include_completed=&day=` | Owner-scoped list. |
| POST | `""` | `{title, notes?, priority?, due_at?, recurrence?, reminder_at?}` → 201. Bad dates → 422 with a readable message. |
| GET | `/{id}` | Yours or 404. |
| PATCH | `/{id}` | Partial update; recurrence validated. |
| POST | `/{id}/complete` | Completes; recurring tasks spawn the next occurrence (same audit id). |
| DELETE | `/{id}` | Requires `TASK_DELETE` (STAFF+). Brain route additionally demands human confirmation. |
| POST | `/clear-completed` | Bulk clear of your completed tasks. |

## System — `/system`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Public. `status`, database check, AI provider availability/mode. |
| GET | `/metrics` | Auth required. Live psutil CPU/RAM/disk/battery/network + human alerts. |
| GET | `/boot` | Startup report: systems, tools, wake word, providers. |

## Security — `/security`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/status` | Every authenticated user (free tier): threat level, risk band, open events, incidents, auto-containment, failed-login bursts. |
| GET | `/events?limit=&status_filter=` | §15.13 shape: `what_happened, why_flagged, seriousness, what_alyo_did, what_you_can_do`, score/band/status/evidence. |
| GET | `/events/{id}` | Single event detail. |
| POST | `/events/{id}/resolve` | Requires `SECURITY_MANAGE`; note required. |
| GET | `/incidents?include_resolved=` | Requires `SECURITY_READ`. |
| POST | `/incidents/{id}/state` | `{state, note}` — validated lifecycle; invalid → 400. |
| GET | `/audit` | Requires `AUDIT_READ`; paginated, redacted, append-only. |
| GET | `/escalation` | Escalation contacts; unconfigured ones honestly flagged. |
| GET | `/emergency` | Physical-emergency info: number (112), `auto_contact_enabled: false`, cyber-vs-physical note. |

## Privacy — `/privacy`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/retention` | Retention policy table + stored record counts. |
| PUT | `/retention` | `USER_MANAGE`; validated 0–3650 days; audited. |
| GET | `/data` | What ALYO stores for you + `never_stored` list. |
| GET | `/export` | Full personal export (no hashes/tokens/keys), audited. |
| DELETE | `/conversations` | Truthful `DELETION_COMPLETED` / `DELETION_PARTIALLY_COMPLETED` + counts. |
| POST | `/account-deletion` | `{password, confirm}` — wrong password → 401 (nothing deleted); success revokes sessions, deletes owned rows, disables the account, returns per-table counts. |
| PUT | `/conversation-history` | Per-user toggle (not global). |
| PUT | `/long-term-memory` | Per-user toggle; when off, memory writes are refused with a clear reason. |

## Example

```powershell
$tok = (Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8760/api/v1/auth/login `
        -ContentType application/json -Body '{"username":"admin","password":"…"}').token

Invoke-RestMethod -Uri http://127.0.0.1:8760/api/v1/brain/chat -Method Post `
  -Headers @{Authorization="Bearer $tok"} -ContentType application/json `
  -Body '{"message":"Hey Jarvis, system status"}'
```
