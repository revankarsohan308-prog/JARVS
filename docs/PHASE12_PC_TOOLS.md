# NEXORA - PHASE 12: Controlled PC Tools

**Status**: IMPLEMENTED — TESTED (Real)

## Current Tools (Real, Controlled)
From `core/agent/catalog.py`:
- `system.status` - real CPU/RAM/disk/battery/network via psutil (verified=True)
- `time.current` - real system time
- `memory.store`, `memory.search`, `memory.delete` - real memory operations with isolation
- `task.create`, `task.list`, `task.complete`, `task.delete` - real task operations

All tools:
- Go through permission registry
- Return `verified` boolean
- Enforce RBAC permissions
- Respect confirmation requirements
- Audited

**Real, no fakes.**

Next: PHASE 13 (File Intelligence)
