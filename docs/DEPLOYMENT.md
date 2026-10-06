# Deployment

Milestone 1 targets a single machine / small team behind a reverse proxy.
Follow this order — security-relevant steps first.

## 1. Prepare

```powershell
# production-ish install
Copy-Item .env.example .env
# edit .env:
#   ALYO_ENVIRONMENT=production
#   ALYO_APP_SECRET_KEY=<openssl rand -hex 32>
#   ALYO_LLM_PROVIDER=openai (only if you have a key)
#   ALYO_RATE_LIMIT_* tightened if exposed publicly
```

Never commit `.env`. `.gitignore` already excludes it, `data/`, `logs/`, `.venv/`.

## 2. Run as a service

**Windows (Task Scheduler / NSSM):**
```powershell
nssm install Alyo "C:\path\to\ALYO\.venv\Scripts\python.exe" "C:\path\to\ALYO\main.py"
nssm set Alyo AppDirectory "C:\path\to\ALYO"
nssm start Alyo
```

**Linux (systemd):**
```ini
# /etc/systemd/system/alyo.service
[Service]
WorkingDirectory=/opt/ALYO
ExecStart=/opt/ALYO/.venv/bin/python main.py
Restart=on-failure
User=alyo
[Install]
WantedBy=multi-user.target
```

## 3. TLS / reverse proxy

The app binds `127.0.0.1` by default — do **not** expose it directly.
Put nginx/Caddy in front with TLS:

```nginx
location / { proxy_pass http://127.0.0.1:8760; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; }
```

If you must change the bind, set `ALYO_APP_HOST`/`ALYO_APP_PORT` and firewall
the port. CORS allowlist (`api/app.py`) must match your real origin.

## 4. Data & backups

- Database: `data/alyo.db` (SQLite, WAL). Back it up with
  `sqlite3 data/alyo.db ".backup backup.db"` while running — do not copy the
  file mid-write.
- Audit log is append-only; retention sweeps are audited, so backups reflect
  exactly what policy allowed to exist.
- Logs: `logs/` — rotate with your OS log rotation.

## 5. Hardening checklist

- [ ] `ALYO_APP_SECRET_KEY` set to a random value
- [ ] `.env` permissions restricted (not world-readable)
- [ ] TLS terminated at the proxy; HTTP redirected
- [ ] `ALYO_ALLOW_OPEN_REGISTRATION=false` if accounts are invite-only
- [ ] First (SUPER_ADMIN) account created, then verify no second admin appeared
- [ ] `ALYO_AUTO_CONTAINMENT_ENABLED=true` (default) unless you have a reason
- [ ] Emergency contacts configured in-app if you rely on escalation
- [ ] `pytest -q` and `ruff check .` pass on the deployed tree
- [ ] `GET /api/v1/system/health` reports `ONLINE` from outside the proxy

## 6. Upgrades

1. Stop service → `git pull` → `pip install -r requirements.txt`
2. Run `pytest -q` **before** restarting.
3. Start service; check `/api/v1/system/health` and `logs/`.

Database migrations are not yet versioned (Milestone 1 creates schema on
startup). Rehearse upgrades on a copy of the database first.
