# Roadmap

Order matters: each milestone only starts when the previous one is stable,
tested and documented.

## ✅ Milestone 1 — ALYO Core + baseline security (current)

- [x] Service starts (`python main.py`), versioned API `/api/v1`, docs at `/docs`
- [x] User registration/login/logout/sessions; first account = SUPER_ADMIN,
      later accounts cannot self-escalate
- [x] Text commands through the brain (wake word "Hey Jarvis", rule engine,
      honest local fallback, optional OpenAI provider)
- [x] Conversation context with per-user on/off and deletion
- [x] Long-term memory (store/search/update/delete, secret rejection)
- [x] Tasks (create/list/complete/delete, due dates, recurrence, reminders)
- [x] Controlled tools: permission gates + human confirmation + `verified` flags
- [x] System health & live metrics (psutil)
- [x] Security: audit (append-only, redacted), rate limits, threat/bug events
      with 0–100 risk scores, containment, incident lifecycle, escalation &
      emergency honesty (§15)
- [x] Privacy: retention policy, export, truthful deletion statuses, retention
      worker (§16)
- [x] Clean web interface (chat, tasks, memory, system, security, privacy)
- [x] 94 tests passing, Ruff clean, full documentation set

## Milestone 2 — Conversation depth & reliability (proposed)

- Streaming replies and interruption
- Multi-turn intent handling beyond the rule engine; structured tool calling
  via the OpenAI provider (function calling)
- Background reminder scheduler with real notifications (desktop/email
  integration, honestly reported as configured/unconfigured)
- Migration framework for schema versioning

## Milestone 3 — Voice & wake word

- Real on-device wake word detection ("Hey Jarvis")
- Speech-to-text / text-to-speech providers behind the same honesty contract
- Voice-specific confirmation UX (destructive actions still need explicit yes)

## Milestone 4 — Knowledge & memory expansion

- Document ingestion with per-user isolation
- Memory recall quality (embedding search behind a provider interface)
- Import/export in standard formats

## Milestone 5+ — Multi-user platform (per spec phases)

- Organization/college hierarchy (COLLEGE_ADMIN → HOD → FACULTY/STAFF/STUDENT)
- Admin console, delegated RBAC management
- Billing & plans (₹699 HIGH SECURITY tier with configurable billing period —
  payment provider integration, never simulated)
- Expanded security: endpoint integrations, physical-zone awareness (indoor)
- Mobile companion apps

## Cross-cutting rules (apply to every milestone)

1. **REAL > DEMO** — if it can't really run, it reports
   "Integration not configured" plus requirements.
2. **SAFE > UNCONTROLLED** — destructive actions need human confirmation;
   threats are scored, contained and escalated, never silently ignored.
3. Small working slice → tests → docs → next slice.
4. Security/privacy regressions block the milestone, not just bugs.
