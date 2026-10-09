"""NEXORA service-health & recovery subsystem (Reliability Addendum Module 1).

Public surface:
- ``run_all`` / ``run_component``: real probes with persistent status.
- ``record_worker_heartbeat``: background workers report liveness.
- ``recovery_history``: audit-friendly recovery episode history.
- ``run_bounded`` / ``RetryPolicy``: bounded, classified retries for reuse.
"""
