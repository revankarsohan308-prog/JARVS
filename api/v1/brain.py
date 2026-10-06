"""Chat with the ALYO Brain, plus conversation history and confirmations."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbDep
from config.settings import settings
from core.agent.registry import registry, resolve_confirmation
from core.brain import orchestrator

router = APIRouter(prefix="/brain", tags=["brain"])


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="text", max_length=16)


class ConfirmationIn(BaseModel):
    confirmation_id: str = Field(min_length=1, max_length=64)
    approve: bool


@router.post("/chat")
def chat(body: ChatIn, user: CurrentUser, db: DbDep):
    response = orchestrator.handle_message(db, user, body.message, source=body.source)
    return response.as_dict()


@router.get("/history")
def history(user: CurrentUser, db: DbDep, limit: int = 50):
    turns = orchestrator.recent_turns(db, user, limit=min(limit, 200))
    return {
        "turns": [
            {
                "role": t.role,
                "content": t.content,
                "language": t.language,
                "created_at": t.created_at.isoformat() + "Z",
            }
            for t in turns
        ],
        "history_enabled": user.conversation_history_enabled,
    }


@router.delete("/history")
def clear_history(user: CurrentUser, db: DbDep):
    count = orchestrator.clear_history(db, user, actor=f"user:{user.username}")
    return {"ok": True, "deleted_turns": count}


@router.post("/confirmations/{confirmation_id}")
def resolve(confirmation_id: str, body: ConfirmationIn, user: CurrentUser, db: DbDep):
    if body.confirmation_id != confirmation_id:
        raise HTTPException(status_code=400, detail="Confirmation id mismatch.")
    found, tool_name, args, resolution = resolve_confirmation(
        db, user.id, confirmation_id, body.approve, actor=f"user:{user.username}"
    )
    if not found:
        raise HTTPException(status_code=404, detail="Confirmation request not found or not yours.")
    if resolution != "approved":
        return {"ok": False, "resolution": resolution, "tool": tool_name, "text": "Declined. Nothing ran."}

    response = orchestrator.BrainResponse(text="")
    result = orchestrator._run_tool(db, user, tool_name, args, response, confirmed=True)
    if result is None:
        return {"ok": False, "text": f"Tool {tool_name} is no longer available.", "tools": []}
    text = str(result.data.get("message", "")) if result.ok else (result.message or "The action did not complete.")
    return {
        "ok": result.ok,
        "verified": result.verified,
        "text": text,
        "tools": response.tools,
        "resolution": resolution,
    }


@router.get("/tools")
def tools(user: CurrentUser, db: DbDep):
    """Tool catalog with permission semantics (transparency, spec §15.1)."""
    orchestrator.register_all(registry)
    out = []
    for spec in registry.all_specs():
        available, reason = registry.availability(spec, settings)
        allowed, needs_confirmation, denial = registry.check_permission(
            spec, user.role, settings
        )
        out.append(
            {
                "name": spec.name,
                "description": spec.description,
                "permission_level": spec.permission_level.value,
                "required_permission": spec.required_permission.value,
                "available": available,
                "unavailable_reason": reason,
                "your_role_allowed": allowed,
                "needs_confirmation": needs_confirmation,
                "denied_reason": denial,
            }
        )
    return {"tools": out}
