# Setup

## Requirements

- Python 3.11+ (developed on Windows; macOS/Linux work the same)
- No API keys, databases, or external services needed for Milestone 1

## Install

```powershell
cd C:\Users\User\Projects\ALYO
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

macOS/Linux: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

## Configure (optional)

```powershell
Copy-Item .env.example .env
```

Everything works with defaults. Values you may want to change:

| `.env` key | Why |
| --- | --- |
| `ALYO_APP_SECRET_KEY` | Set to a random 32+ char string before any real deployment |
| `ALYO_OPENAI_API_KEY` | Enables real LLM replies (`ALYO_LLM_PROVIDER=openai`) |
| `ALYO_EMERGENCY_REGION` / `ALYO_EMERGENCY_NUMBER` | Emergency display info (default India / 112) |
| `ALYO_DATABASE_URL` | Absolute path or another SQLAlchemy URL if you outgrow SQLite |

## Social sign-in (Google / LinkedIn) — optional

Social login is a real OAuth 2.0 flow; it works only after you register OAuth
clients with the providers (ALYO never ships shared credentials):

1. **Google** — [Google Cloud Console → Credentials → Create OAuth client (Web)](https://console.cloud.google.com/apis/credentials).
   Authorized redirect URI: `http://127.0.0.1:8760/api/v1/auth/oauth/google/callback`
2. **LinkedIn** — [LinkedIn Developer Portal → Create app](https://developer.linkedin.com/) →
   add the *Sign In with LinkedIn using OpenID Connect* product. Redirect URI:
   `http://127.0.0.1:8760/api/v1/auth/oauth/linkedin/callback`

Put the client id/secret in `.env` (see `.env.example`) and restart. Until then
the sign-in page marks the buttons **not configured** and clicking one shows the
exact variables required — it never pretends to work.

Behind a reverse proxy set `ALYO_PUBLIC_BASE_URL=https://your-domain` so the
redirect URI matches what you registered with the provider.

The endpoints themselves are overridable (`ALYO_GOOGLE_AUTHORIZATION_URL`,
`ALYO_GOOGLE_TOKEN_URL`, `ALYO_GOOGLE_USERINFO_URL`, same for LinkedIn), so
sign-in can point at any OIDC-compliant server — Keycloak, Authentik, Azure AD,
or an internal verification server — without touching code.

## Run

```powershell
.\.venv\Scripts\python main.py
# → ALYO on http://127.0.0.1:8760  (UI at /, API docs at /docs)
```

First run creates `data/alyo.db` and seeds nothing —
**register the first account to become SUPER_ADMIN**.

## Verify the installation

```powershell
.\.venv\Scripts\python -m pytest -q   # expect: 95 passed
.\.venv\Scripts\python -m ruff check . # expect: All checks passed!
Invoke-RestMethod http://127.0.0.1:8760/api/v1/system/health
```

`checks.database.status` must be `ONLINE`.
