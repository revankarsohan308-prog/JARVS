"""ALYO Brain orchestrator: the single entry point for a user message.

Pipeline (spec §4):
    text -> wake strip -> context load -> intent -> permission check ->
    tool execution -> verification -> response -> memory update -> audit

Two paths:
- **Rule path** — deterministic intents handled without any model. Always
  available, works offline.
- **LLM path** — when a cloud provider is configured, it may propose tool
  calls; every call still goes through the same registry and permission gate.

The orchestrator never reports success for an unverified action (spec §49).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from core.agent import catalog
from core.agent.registry import ExecutionResult, execute_tool, registry, resolve_confirmation
from core.brain.providers import get_provider, provider_status
from core.memory import service as memory
from core.permissions.rbac import has_permission
from core.tasks import service as tasks
from database.models import ConversationTurn, User
from security.audit import audit
from security.ratelimit import limiter

register_all = catalog.register_all_tools


@dataclass
class BrainResponse:
    text: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    needs_confirmation: bool = False
    confirmation_id: str = ""
    confirmation_summary: str = ""
    source: str = "rules"  # rules | llm
    context_used: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tools": self.tools,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_id": self.confirmation_id,
            "confirmation_summary": self.confirmation_summary,
            "source": self.source,
            "context_used": self.context_used,
        }


# ----------------------------------------------------------------------
# Wake word and light parsing
# ----------------------------------------------------------------------

_WAKE_RE = re.compile(
    r"^\s*(?:hey|ok|okay|hello|hi)?\s*(?:jarvis|alyo)[,!\s]*",
    re.IGNORECASE,
)


def strip_wake(text: str) -> str:
    stripped = _WAKE_RE.sub("", text.strip(), count=1).strip()
    return stripped or text.strip()


_REMIND_RE = re.compile(
    r"^(?:remind me (?:to |about )?|create (?:a )?task(?: to)?[:: ]+)\s*(?P<title>.+?)"
    r"(?:\s+(?:on|by|at)\s+(?P<due>\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)\s*)?$",
    re.IGNORECASE,
)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_due(due: str | None) -> str | None:
    if not due:
        return None
    due = due.strip()
    if _DATE_ONLY_RE.match(due):
        return f"{due}T09:00:00"
    return due.replace(" ", "T")


# ----------------------------------------------------------------------
# Conversation context
# ----------------------------------------------------------------------

def save_turn(db: Session, user: User, role: str, content: str, language: str = "en") -> None:
    # History disabled means no conversation content is retained at all.
    if not user.conversation_history_enabled:
        return
    db.add(ConversationTurn(user_id=user.id, role=role, content=content, language=language))
    db.commit()


def recent_turns(db: Session, user: User, limit: int | None = None) -> list[ConversationTurn]:
    limit = limit or settings.history_window
    if not user.conversation_history_enabled:
        return []
    return db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.user_id == user.id)
        .order_by(ConversationTurn.id.desc())
        .limit(limit)
    ).all()[::-1]


def clear_history(db: Session, user: User, actor: str) -> int:
    rows = db.scalars(select(ConversationTurn).where(ConversationTurn.user_id == user.id)).all()
    count = len(rows)
    for row in rows:
        db.delete(row)
    db.commit()
    if count:
        audit(db, "conversation_cleared", f"count={count}", actor=actor, component="memory")
    return count


# ----------------------------------------------------------------------
# Rule engine (works with no LLM configured)
# ----------------------------------------------------------------------

def _run_tool(
    db: Session,
    user: User,
    name: str,
    args: dict[str, Any],
    response: BrainResponse,
    *,
    confirmed: bool = False,
) -> ExecutionResult | None:
    resolved = registry.resolve_name(name)
    if resolved is None:
        return None
    spec = registry.get(resolved)
    assert spec is not None
    result = execute_tool(
        db,
        spec,
        args,
        user_id=user.id,
        role=user.role,
        settings_obj=settings,
        actor=f"user:{user.username}",
        confirmed=confirmed,
    )
    response.tools.append({"name": spec.name, **result.as_dict()})
    if result.needs_confirmation:
        response.needs_confirmation = True
        response.confirmation_id = result.confirmation_id
        response.confirmation_summary = result.message
    return result


def _rule_engine(db: Session, user: User, command: str, response: BrainResponse) -> bool:
    """Handle deterministic intents. Returns True when one matched."""
    text = command.strip()
    lowered = text.lower()

    # --- confirmation answers -------------------------------------------
    if re.fullmatch(r"(yes|yeah|yep|confirm|approve|do it|go ahead|sure)[.!]?", lowered):
        return _handle_confirmation(db, user, True, response)
    if re.fullmatch(r"(no|nope|cancel|deny|stop|don'?t)[.!]?", lowered):
        return _handle_confirmation(db, user, False, response)

    # --- greetings / help ------------------------------------------------
    if re.fullmatch(r"(hi|hello|hey|good (morning|afternoon|evening))[.!]?", lowered):
        response.text = (
            "Hello. I can manage your tasks, remember facts you tell me, "
            "report system health and run authorized tools. What would you like?"
        )
        return True
    if lowered in ("help", "what can you do", "commands"):
        response.text = (
            "You can ask me to: create or list tasks (\"remind me to submit "
            "assignment on 2026-10-14\"), remember or recall facts (\"remember "
            "my course is BBA\"), check system status, or run any authorized "
            "tool. Start with 'help' was a good move — try 'system status'."
        )
        return True

    # --- system ------------------------------------------------------------
    if lowered in ("system status", "status", "system health", "how is my system"):
        result = _run_tool(db, user, "system.status", {}, response)
        if result and result.ok:
            d = result.data
            response.text = (
                f"CPU {d['cpu_percent']}% · RAM {d['ram_percent']}% · "
                f"Disk {d['disk_percent']}%"
                + (f" · Battery {d['battery_percent']}%" if d["battery_percent"] is not None else "")
                + (f"\n⚠ {d['message']}" if d.get("alerts") else "")
            )
            return True
    if lowered in ("time", "what time is it", "current time", "date"):
        result = _run_tool(db, user, "system.time", {}, response)
        if result and result.ok:
            response.text = str(result.data.get("message", ""))
            return True
    if lowered in ("health", "alyo health", "core health"):
        result = _run_tool(db, user, "system.health", {}, response)
        if result and result.ok:
            response.text = str(result.data.get("message", ""))
            return True

    # --- memory: recall BEFORE store ("what do you remember about x" also
    #     contains the word "remember") -------------------------------------
    recall = re.match(r"^(?:what do you (?:know|remember) about|recall|what is my)\s+(.+?)\s*[?]?$", lowered)
    if recall:
        rows = memory.search(db, user.id, recall.group(1))
        if rows:
            response.text = "; ".join(f"{r.key}: {r.content}" for r in rows[:3])
        else:
            response.text = (
                f"I couldn't find anything in your memory about '{recall.group(1)}'. "
                "You can store it with: remember <key> = <content>."
            )
        return True

    remember = re.match(r"^remember\s+(.+?)\s*[=:]\s*(.+)$", text, re.IGNORECASE)
    if remember:
        try:
            row = memory.store(
                db, user.id, remember.group(1), remember.group(2), actor=f"user:{user.username}"
            )
            response.text = f"Remembered '{row.key}'."
        except memory.MemoryRejected as exc:
            response.text = str(exc)
        return True

    forget = re.match(r"^(?:forget|delete memory)\s+(?:id\s+)?(\d+)$", lowered)
    if forget:
        removed = memory.delete(db, user.id, int(forget.group(1)), actor=f"user:{user.username}")
        response.text = (
            f"Deleted memory #{forget.group(1)}." if removed else "You don't have that memory."
        )
        return True

    # --- tasks -------------------------------------------------------------
    remind = _REMIND_RE.match(text)
    if remind and remind.group("title"):
        # Route through the tool registry so the execution is permission-checked
        # and recorded in response.tools like every other tool run.
        args: dict[str, Any] = {"title": remind.group("title")}
        due_iso = _parse_due(remind.group("due"))
        if due_iso:
            args["due_at"] = due_iso
        result = _run_tool(db, user, "task.create", args, response)
        if result is None:  # pragma: no cover - registry always knows the tool
            response.text = "The task tool is not registered."
        elif result.ok:
            response.text = str(result.data.get("message", "Task created."))
        else:
            response.text = str(result.data.get("message", result.message))
        return True

    if lowered in ("my tasks", "list tasks", "tasks", "what are my tasks", "show tasks"):
        rows = tasks.list_tasks(db, user.id)
        if not rows:
            response.text = "You have no pending tasks."
        else:
            lines = [
                f"#{t.id} [{t.priority}] {t.title}"
                + (f" — due {catalog.format_due(t.due_at)}" if t.due_at else "")
                for t in rows[:15]
            ]
            response.text = "Your tasks:\n" + "\n".join(lines)
        return True

    done = re.match(r"^(?:complete|done|finish)(?:\s+task)?\s*#?(\d+)$", lowered)
    if done:
        task = tasks.complete(db, user.id, int(done.group(1)), actor=f"user:{user.username}")
        response.text = (
            f"Completed task #{task.id}: {task.title}." if task else "You don't have that task."
        )
        return True

    del_task = re.match(r"^(?:delete|remove)\s+(?:task\s*)?#?(\d+)$", lowered)
    if del_task:
        # Deletion is destructive -> goes through the confirmation gate.
        result = _run_tool(db, user, "task.delete", {"task_id": int(del_task.group(1))}, response)
        if result and result.needs_confirmation:
            response.text = (
                f"{result.message} Reply 'yes' to confirm deletion or 'no' to cancel."
            )
            return True
        if result:
            response.text = str(result.data.get("message", result.message))
            return True

    # --- status of ALYO itself ----------------------------------------------
    if lowered in ("are you there", "you awake", "wake up"):
        response.text = "Yes, I'm here."
        return True

    return False


def _handle_confirmation(db: Session, user: User, approve: bool, response: BrainResponse) -> bool:
    from database.models import PendingConfirmation

    row = db.scalar(
        select(PendingConfirmation)
        .where(PendingConfirmation.user_id == user.id, PendingConfirmation.resolved.is_(False))
        .order_by(PendingConfirmation.id.desc())
        .limit(1)
    )
    if row is None:
        response.text = "There's nothing waiting for confirmation."
        return True
    found, tool_name, args, resolution = resolve_confirmation(
        db, user.id, row.id, approve, actor=f"user:{user.username}"
    )
    if not found or resolution != "approved":
        response.text = f"Declined: {tool_name or row.tool_name} will not run."
        return True
    result = _run_tool(db, user, tool_name, args, response, confirmed=True)
    if result is None:
        response.text = f"That tool ({tool_name}) is no longer available."
    elif result.ok:
        response.text = str(result.data.get("message", "Done."))
    else:
        response.text = result.message or "The action did not complete."
    return True


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------

def handle_message(db: Session, user: User, raw_text: str, source: str = "text") -> BrainResponse:
    register_all(registry)
    text = (raw_text or "").strip()
    response = BrainResponse(text="")

    if not text:
        response.text = "I didn't catch any text. Type your request."
        return response

    # Rate limit (baseline security). Counted per user.
    allowed, retry_after = limiter.check(
        f"chat:{user.id}", settings.chat_rate_limit, settings.chat_rate_window
    )
    if not allowed:
        audit(db, "chat_rate_limited", f"user={user.username} retry_after={retry_after}",
              actor=f"user:{user.username}", component="brain")
        response.text = f"Too many messages right now. Try again in {retry_after}s."
        return response

    save_turn(db, user, "user", text)
    turns = recent_turns(db, user)
    response.context_used = max(len(turns) - 1, 0)  # excludes the turn just saved

    command = strip_wake(text)

    if _rule_engine(db, user, command, response):
        response.source = "rules"
        save_turn(db, user, "assistant", response.text)
        return response

    # --- LLM path ---------------------------------------------------------
    provider = get_provider()
    if provider.name == "local":
        response.text = _fallback_reply(command)
        response.source = "rules"
        save_turn(db, user, "assistant", response.text)
        return response

    history = [
        {"role": t.role, "content": t.content}
        for t in turns[-settings.history_window :]
    ]
    if not any(m["role"] == "system" for m in history):
        history.insert(
            0,
            {
                "role": "system",
                "content": (
                    "You are ALYO, a personal assistant. You act through tools; never "
                    "claim an action succeeded without a tool result confirming it. If "
                    "a tool is unavailable, say so plainly. Be concise and honest."
                ),
            },
        )
    history.append({"role": "user", "content": command})

    completion = provider.complete(history, registry.openai_tools())
    if not completion.ok:
        audit(db, "llm_failure", f"provider={provider.name} error={completion.error}",
              actor=f"user:{user.username}", component="brain")
        response.text = (
            f"The AI provider failed ({completion.error}). Falling back to local rules: "
            + _fallback_reply(command)
        )
        response.source = "rules"
        save_turn(db, user, "assistant", response.text)
        return response

    # Execute up to 4 rounds of tool calls, feeding results back.
    for _round in range(4):
        if not completion.tool_calls:
            break
        for call in completion.tool_calls:
            result = _run_tool(db, user, call.name, call.arguments, response)
            if result is None:
                response.tools.append(
                    {"name": call.name, "ok": False, "verified": False,
                     "message": "Unknown tool."}
                )
        if response.needs_confirmation:
            response.text = (
                f"{response.confirmation_summary} Reply 'yes' to confirm or 'no' to cancel."
            )
            save_turn(db, user, "assistant", response.text)
            return response
        history.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    [{"name": t["name"], "ok": t["ok"], "data": t.get("data", {})}
                     for t in response.tools]
                ),
            }
        )
        completion = provider.complete(history, registry.openai_tools())

    if completion.text.strip():
        response.text = completion.text.strip()
        response.source = "llm"
    elif response.tools:
        # Model returned nothing usable; report the verified tool outcome.
        last = response.tools[-1]
        response.text = str(last.get("message") or last.get("data", {}).get("message", "Done."))
        response.source = "llm"
    else:
        response.text = _fallback_reply(command)
        response.source = "rules"

    save_turn(db, user, "assistant", response.text)
    return response


def _fallback_reply(command: str) -> str:
    """Honest reply when no LLM is configured — lists what actually works."""
    return (
        "I understood you as a general request, but no AI model is configured yet, "
        "so I can only run my built-in commands right now. Things that work without "
        "a model: 'system status', 'my tasks', 'remind me to <task> on YYYY-MM-DD', "
        "'remember <key> = <content>', 'what do you remember about <key>', 'time', "
        "and 'help'. To enable full AI conversation, set ALYO_LLM_PROVIDER=openai "
        "and ALYO_OPENAI_API_KEY in .env."
    )


def brain_status(db: Session, user: User | None = None) -> dict[str, Any]:
    register_all(registry)
    return {
        "provider": provider_status(),
        "wake_word": settings.wake_word,
        "personality": settings.personality_mode,
        "tools_total": len(registry.all_specs()),
        "tools_enabled": sum(1 for t in registry.all_specs() if t.enabled),
        "history_enabled": user.conversation_history_enabled if user else settings.conversation_history_enabled,
        "memory_enabled": user.long_term_memory_enabled if user else settings.long_term_memory_enabled,
        "user_role": user.role if user else None,
        "can_confirm_tools": has_permission(user.role, "tool:confirm") if user else False,
    }
