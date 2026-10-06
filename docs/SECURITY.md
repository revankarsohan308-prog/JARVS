# ALYO Security

Baseline security layer implemented in Milestone 1 (spec §15), plus data
retention/privacy controls (spec §16).

## Authentication

- Passwords: PBKDF2-HMAC-SHA256, per-user salt, iterations from
  `ALYO_PBKDF2_ITERATIONS`. Login verifies against a **dummy hash** when the
  user does not exist so response timing does not reveal account existence.
- Session tokens: 256-bit `secrets.token_urlsafe(32)`. Only the **SHA-256
  digest** is persisted; the raw token is returned exactly once (login/register).
- Logout revokes server-side; `revoke-all` kills every session for the account.
- Login is rate-limited per username+host; a breach feeds the brute-force
  threat detector with a real score.

## Authorization (RBAC)

Roles: `SUPER_ADMIN, COLLEGE_ADMIN, HOD, FACULTY, STAFF, STUDENT, GUEST`.
Pure functions `permissions_for()` / `has_permission()` in
`core/permissions/rbac.py` — **fail closed** on unknown role/permission.

- Every mutating route declares its required permission via
  `require_permission(Permission.X)`.
- **Bootstrap:** the first account registered on an empty deployment becomes
  SUPER_ADMIN; every later registration is forced to STUDENT (role assignment
  requires an authenticated `USER_MANAGE` administrator).
- **Denial evidence:** every 403 is audited; ≥5 denials for one user in 5
  minutes creates a scored `permission_bypass_attempt` threat event.

## Threat & bug detection (§15.2/§15.3)

Every event has a **0–100 risk score** with the fixed bands:

| Score | Band | Typical response |
| --- | --- | --- |
| 0–29 | LOW | Logged for review |
| 30–59 | MEDIUM | Logged, flagged on dashboard |
| 60–79 | HIGH | Automatic containment (session revocation) + incident |
| 80–100 | CRITICAL | Containment + incident, escalation guidance shown |

Event sources: failed-login bursts, permission denials, sensitive-data
rejections, config tampering, repeated API 5xx on one path, unhandled
exceptions (bug detector), tool errors.

**Automatic containment** (when `ALYO_AUTO_CONTAINMENT_ENABLED=true`) revokes
the affected user's sessions and opens an incident in `CONTAINED`. It never
deletes data — evidence is preserved.

## Incident lifecycle

`DETECTED → INVESTIGATING → CONTAINED → ERADICATED → RECOVERED → RESOLVED`
(+ `ESCALATED`). Transitions are validated, audited, and time-stamped in the
incident timeline. Invalid transitions are rejected with 400.

## Event transparency (§15.13)

Every event exposes: what happened, why it was flagged, seriousness (band +
score), what ALYO did, and what the user can do next. Language is deliberately
non-alarmist — ALYO never claims "you are being hacked".

## Audit log

- Append-only: SQLAlchemy `before_update` / `before_delete` hooks raise on any
  attempt to modify an audit row (tested).
- Secrets redaction: `password=…`, `token=…`, `api_key=…`, `secret=…`,
  `authorization=…` values are replaced with `[REDACTED]` before write.
- Every privileged action (auth, RBAC change, tool execution, denial, privacy
  operation, retention run) writes an entry.

## Rate limits (in-process sliding window)

| Key | Default |
| --- | --- |
| Chat per user | `ALYO_RATE_LIMIT_CHAT_PER_MINUTE` = 30/min |
| Login per user+host | 5/min |
| Registration per host | 10/5 min |

## Cyber vs. physical emergency (§15.11)

- **Cyber** events (blackmail, account compromise, stalking evidence) are
  handled by ALYO's incident flow; they do **not** by themselves warrant
  contacting police, and ALYO says so.
- **Physical** emergencies: ALYO displays the configured number
  (`ALYO_EMERGENCY_NUMBER`, default **112** for India) and never places calls
  automatically — `auto_contact_enabled = false` is hard-coded default and
  there is no code path that dials anything.
- **Escalation contacts**: internal numbers must be configured by an admin —
  unconfigured contacts are honestly marked `configured: false`. Only official
  public channels (national cybercrime reporting) ship as reference data.
  ALYO never guesses a "nearest" station or person.

## Data protection

- No secrets in `.env.example`; real values live in `.env` (git-ignored).
- Memory store rejects passwords/keys/tokens and audits the rejection.
- Data export excludes password hashes, session tokens and API keys.
- Retention engine (`retention.py`) auto-deletes expired conversations, audit
  rows, resolved security events, expired sessions and temp files; **OPEN
  security events are under hold** and never age out; audit keeps the newest 24h
  so a mis-set policy cannot empty the trail.

## Known limitations (Milestone 1)

- Single-process in-memory rate limiting and denial tracking (restart resets
  counters; audit trail persists).
- SQLite file permissions are the storage boundary; no at-rest encryption yet.
- No HTTPS termination in-app — deploy behind a TLS reverse proxy
  (see `docs/DEPLOYMENT.md`).
