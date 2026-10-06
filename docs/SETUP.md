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

## Run

```powershell
.\.venv\Scripts\python main.py
# → ALYO on http://127.0.0.1:8760  (UI at /, API docs at /docs)
```

First run creates `data/alyo.db` and seeds nothing —
**register the first account to become SUPER_ADMIN**.

## Verify the installation

```powershell
.\.venv\Scripts\python -m pytest -q   # expect: 78 passed
.\.venv\Scripts\python -m ruff check . # expect: All checks passed!
Invoke-RestMethod http://127.0.0.1:8760/api/v1/system/health
```

`checks.database.status` must be `ONLINE`.
