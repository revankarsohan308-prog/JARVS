"""Built-in tools: system health, memory, tasks, time.

Each handler returns ``{"verified": ..., ...}`` describing what actually
happened. Unavailable integrations are not registered as working tools —
they are registered disabled with the exact configuration required
(spec §2 / §51).
"""

from __future__ import annotations

import platform
import shutil
from datetime import datetime, timezone
from typing import Any

import psutil
from sqlalchemy.orm import Session

from config.settings import settings
from core.agent.registry import PermissionLevel, ToolRegistry, ToolSpec
from core.memory import service as memory
from core.permissions.rbac import Permission
from core.tasks import service as tasks


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------

def _disk_percent(disk) -> float:
    return round((disk.used / disk.total) * 100, 1) if disk.total else 0.0


def _system_status(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    cpu = psutil.cpu_percent(interval=0.1)
    vm = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    disk_pct = _disk_percent(disk)
    battery = psutil.sensors_battery()
    net = psutil.net_io_counters() or None
    alerts: list[str] = []
    if disk_pct >= 90:
        alerts.append(f"Storage is critically low: {disk_pct}% used.")
    if vm.percent >= 90:
        alerts.append(f"Memory usage is very high: {vm.percent}%.")
    if battery and not battery.power_plugged and battery.percent <= 15:
        alerts.append(f"Battery critically low: {battery.percent}%.")
    return {
        "verified": True,
        "cpu_percent": cpu,
        "cpu_cores": psutil.cpu_count(),
        "ram_percent": vm.percent,
        "ram_used_gb": round(vm.used / 1e9, 1),
        "ram_total_gb": round(vm.total / 1e9, 1),
        "disk_percent": disk_pct,
        "disk_used_gb": round(disk.used / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
        "battery_percent": battery.percent if battery else None,
        "battery_plugged": battery.power_plugged if battery else None,
        "net_sent_mb": round(net.bytes_sent / 1e6, 1) if net else None,
        "net_recv_mb": round(net.bytes_recv / 1e6, 1) if net else None,
        "platform": platform.platform(),
        "alerts": alerts,
        "message": alerts[0] if alerts else "All systems nominal.",
    }


def _current_time(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    local = datetime.now().astimezone()
    return {
        "verified": True,
        "utc": now.isoformat(),
        "local": local.isoformat(),
        "timezone": str(local.tzinfo),
        "message": f"Local time is {local.strftime('%Y-%m-%d %H:%M %Z')}.",
    }


def _memory_store(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    row = memory.store(
        db,
        user_id,
        str(args.get("key", "")),
        str(args.get("content", "")),
        category=str(args.get("category", "IMPORTANT_FACTS")),
        actor=f"user_id:{user_id}",
    )
    return {"verified": True, "id": row.id, "key": row.key, "message": f"Remembered '{row.key}'."}


def _memory_search(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    rows = memory.search(db, user_id, str(args.get("query", "")))
    return {
        "verified": True,
        "results": [
            {"id": r.id, "key": r.key, "content": r.content, "category": r.category}
            for r in rows
        ],
        "message": f"Found {len(rows)} matching memories.",
    }


def format_due(dt: datetime) -> str:
    """Human-readable local due time.

    Storage is naive UTC (see timeutil); chat text shows local time because
    that is the timezone the user typed their deadline in.
    """
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _task_create(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    task = tasks.create(
        db,
        user_id,
        str(args.get("title", "")),
        notes=str(args.get("notes", "")),
        priority=str(args.get("priority", "normal")),
        due_at=args.get("due_at") or None,
        recurrence=str(args.get("recurrence", "")),
        reminder_at=args.get("reminder_at") or None,
        actor=f"user_id:{user_id}",
    )
    due = format_due(task.due_at) if task.due_at else "no due date"
    return {
        "verified": True,
        "id": task.id,
        "title": task.title,
        "due_at": due,
        "message": f"Created task #{task.id}: {task.title} (due {due}).",
    }


def _task_list(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    rows = tasks.list_tasks(db, user_id, include_completed=bool(args.get("include_completed")))
    return {
        "verified": True,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "due_at": format_due(t.due_at) if t.due_at else None,
            }
            for t in rows
        ],
        "message": f"You have {len(rows)} task(s).",
    }


def _task_complete(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    task_id = int(args.get("task_id", 0))
    task = tasks.complete(db, user_id, task_id, actor=f"user_id:{user_id}")
    if task is None:
        return {"verified": False, "ok": False, "message": f"No task #{task_id} in your list."}
    return {"verified": True, "id": task.id, "message": f"Completed task #{task.id}: {task.title}."}


def _task_delete(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    task_id = int(args.get("task_id", 0))
    removed = tasks.delete(db, user_id, task_id, actor=f"user_id:{user_id}")
    return {
        "verified": removed,
        "ok": removed,
        "message": f"Deleted task #{task_id}." if removed else f"No task #{task_id} in your list.",
    }


def _health(db: Session, args: dict[str, Any], *, user_id: int, role: str) -> dict[str, Any]:
    """Core service health: verifies the database responds right now."""
    from sqlalchemy import text

    db.execute(text("SELECT 1"))
    return {
        "verified": True,
        "database": "ONLINE",
        "llm_provider": settings.llm_provider,
        "llm_configured": bool(
            settings.openai_api_key if settings.llm_provider == "openai" else True
        ),
        "message": "ALYO core and database are online.",
    }


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

_TASK_PROPS = {
    "title": {"type": "string", "description": "Task title"},
    "notes": {"type": "string", "description": "Optional details"},
    "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
    "due_at": {"type": "string", "description": "ISO date or timestamp"},
    "recurrence": {"type": "string", "enum": ["", "daily", "weekly", "monthly"]},
    "reminder_at": {"type": "string", "description": "ISO timestamp for the reminder"},
}


def register_all_tools(registry: ToolRegistry) -> None:
    if registry.get("system.status"):
        return  # idempotent

    registry.register(
        ToolSpec(
            name="system.status",
            description="Live CPU, RAM, disk, battery and network usage with alerts.",
            parameters={"type": "object", "properties": {}},
            handler=_system_status,
            required_permission=Permission.SYSTEM_READ,
        )
    )
    registry.register(
        ToolSpec(
            name="system.time",
            description="Current date and time in UTC and local timezone.",
            parameters={"type": "object", "properties": {}},
            handler=_current_time,
            required_permission=Permission.SYSTEM_READ,
        )
    )
    registry.register(
        ToolSpec(
            name="system.health",
            description="ALYO core health: database connectivity and provider status.",
            parameters={"type": "object", "properties": {}},
            handler=_health,
            required_permission=Permission.SYSTEM_READ,
        )
    )
    registry.register(
        ToolSpec(
            name="memory.store",
            description="Save a fact to long-term memory (secrets are refused).",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short label, e.g. 'course'"},
                    "content": {"type": "string", "description": "The fact to remember"},
                    "category": {"type": "string", "enum": list(memory.CATEGORIES)},
                },
                "required": ["key", "content"],
            },
            handler=_memory_store,
            required_permission=Permission.MEMORY_WRITE,
            risk_level="MEDIUM",
        )
    )
    registry.register(
        ToolSpec(
            name="memory.search",
            description="Search this user's long-term memories.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=_memory_search,
            required_permission=Permission.MEMORY_READ,
        )
    )
    registry.register(
        ToolSpec(
            name="task.create",
            description="Create a task with optional due date, priority and reminder.",
            parameters={
                "type": "object",
                "properties": _TASK_PROPS,
                "required": ["title"],
            },
            handler=_task_create,
            required_permission=Permission.TASK_WRITE,
            risk_level="MEDIUM",
        )
    )
    registry.register(
        ToolSpec(
            name="task.list",
            description="List the user's pending tasks.",
            parameters={
                "type": "object",
                "properties": {"include_completed": {"type": "boolean"}},
            },
            handler=_task_list,
            required_permission=Permission.TASK_READ,
        )
    )
    registry.register(
        ToolSpec(
            name="task.complete",
            description="Mark a task done. Recurring tasks spawn the next occurrence.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
            handler=_task_complete,
            required_permission=Permission.TASK_WRITE,
            risk_level="MEDIUM",
        )
    )
    registry.register(
        ToolSpec(
            name="task.delete",
            description="Permanently delete a task. Requires human confirmation.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
            handler=_task_delete,
            permission_level=PermissionLevel.CONFIRMATION_REQUIRED,
            required_permission=Permission.TASK_DELETE,
            risk_level="HIGH",
            irreversible=True,
        )
    )
