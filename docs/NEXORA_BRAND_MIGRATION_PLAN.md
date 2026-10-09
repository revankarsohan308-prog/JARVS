# NEXORA BRAND MIGRATION PLAN

## Strategy
We will perform careful, context-aware migration. Tests and core functionality must not break.

### High-priority files (product identity)
1. README.md - J.A.R.V.S/ALYO → NEXORA, owner Sohan Revankar
2. config/settings.py - app_name, env prefix, defaults
3. .env.example - ALYO_ → NEXORA_ (also preserve comments meaningfully)
4. api/app.py - docstrings/comments mentioning ALYO
5. main.py - docstrings
6. backend/voice/config.py - env prefix
7. backend/voice/stt.py, tts.py - references if any
8. Web UI (apps/web/app.js, styles.css) - branding
9. API docs (docs/API.md, ARCHITECTURE.md, etc.)

### Approach
- Classify: product brand (rename), code identifiers (careful), historical (keep or note), third-party (never)
- Update user-facing text
- Update env prefixes in settings + env files
- Update assistant identity (NEXORA, wake phrase "Hey NEXORA" vs "Hey Jarvis")
- Wake word in tests: test_brain.py says "Hey Jarvis" - this is expected behavior; do we change wake phrase to NEXORA? The spec says "Hey NEXORA" for NEXORA (wake phrase "Hey Jarvis" for JARVS). Since migrating to NEXORA identity, update wake phrase consistently.
- But tests validate behavior - need to update tests to match new expected wake phrase.

Key changes:
- Wake phrase: "Hey Jarvis" → "Hey NEXORA" (matches NEXORA spec)
- App name: "J.A.R.V.S" → "NEXORA"
- Env prefix: ALYO_ → NEXORA_ (also support both? Better add support + migrate. But existing code reads ALYO_ - breaking change to env vars. Need to handle migration carefully.)

But we must preserve working functionality. Let us start with non-breaking changes first (docs, comments) and then handle config/env carefully. Also tests depend on wake word behavior.

Wake phrase is in brain/orchestrator? Let us check.
