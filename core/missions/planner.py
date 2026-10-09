"""Mission planning (Reliability Addendum Module 2).

The planner turns an objective into a *structured, inspectable* plan of tool
steps. It is deliberately deterministic and honest:

- It only plans steps it can actually execute through registered tools.
- If an objective cannot be mapped to real tools, it refuses to invent steps
  and returns ``PlanNotPossible`` with the reason. The mission then stays in
  PLANNING with an honest message instead of pretending to have a plan.

An LLM may later *propose* a plan, but that plan still has to pass through
this module's validation (known tools, permission-checkable args, bounded
step count) and the permission engine before any step runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from config.settings import settings
from core.agent.registry import registry, risk_rank


class PlanNotPossible(Exception):
    """Raised when an objective cannot be mapped to real, safe steps."""


@dataclass
class PlannedStep:
    name: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)  # positions (0-based)


@dataclass
class Plan:
    steps: list[PlannedStep]
    source: str = "rules"  # rules | explicit
    notes: str = ""


# Objective fragments -> (tool, arg builder). Order matters: first match wins.
_REMEMBER_RE = re.compile(r"remember\s+(.+?)\s*[=:]\s*(.+)$", re.IGNORECASE)
_TASK_RE = re.compile(
    r"(?:create|add)\s+(?:a\s+)?task(?:\s+to)?\s*[:\-]?\s*(?P<title>.+?)"
    r"(?:\s+on\s+(?P<due>\d{4}-\d{2}-\d{2}))?$",
    re.IGNORECASE,
)
_DELETE_TASK_RE = re.compile(r"(?:delete|remove)\s+(?:task\s*)?#?(\d+)$", re.IGNORECASE)
_SEARCH_MEMORY_RE = re.compile(r"(?:recall|what do you know about|search memory for)\s+(.+?)\s*[?]?$", re.IGNORECASE)


def _split_compound(text: str) -> list[str]:
    """Split "do A and then do B" objectives into ordered fragments."""
    parts = re.split(r"\s*(?:,\s*then\s+|\s+then\s+|\s+and\s+then\s+|\s*;\s*)\s*", text.strip(), flags=re.IGNORECASE)
    if len(parts) == 1:
        # Plain " and " is only a separator when both sides look like commands.
        halves = re.split(r"\s+and\s+", text.strip(), flags=re.IGNORECASE)
        if len(halves) == 2 and _fragment_plannable(halves[0]) and _fragment_plannable(halves[1]):
            return halves
    return [p for p in (x.strip() for x in parts) if p]


def _fragment_plannable(fragment: str) -> bool:
    fragment = fragment.strip()
    return bool(
        _REMEMBER_RE.match(fragment)
        or _TASK_RE.match(fragment)
        or _DELETE_TASK_RE.match(fragment)
        or _SEARCH_MEMORY_RE.match(fragment)
        or fragment.lower() in ("system status", "status", "system health")
    )


def _plan_fragment(fragment: str) -> PlannedStep | None:
    fragment = fragment.strip()
    lowered = fragment.lower()

    remember = _REMEMBER_RE.match(fragment)
    if remember:
        return PlannedStep(
            name=f"Remember '{remember.group(1).strip()}'",
            tool_name="memory.store",
            args={"key": remember.group(1).strip(), "content": remember.group(2).strip()},
        )

    delete = _DELETE_TASK_RE.match(fragment)
    if delete:
        return PlannedStep(
            name=f"Delete task #{delete.group(1)}",
            tool_name="task.delete",
            args={"task_id": int(delete.group(1))},
        )

    task = _TASK_RE.match(fragment)
    if task and task.group("title"):
        args: dict[str, Any] = {"title": task.group("title").strip()}
        if task.group("due"):
            args["due_at"] = f"{task.group('due')}T09:00:00"
        return PlannedStep(name=f"Create task '{args['title']}'", tool_name="task.create", args=args)

    search = _SEARCH_MEMORY_RE.match(fragment)
    if search:
        return PlannedStep(
            name=f"Recall memory '{search.group(1).strip()}'",
            tool_name="memory.search",
            args={"query": search.group(1).strip()},
        )

    if lowered in ("system status", "status", "system health"):
        return PlannedStep(name="Check system status", tool_name="system.status", args={})

    return None


def plan_objective(objective: str) -> Plan:
    """Deterministically plan an objective, or raise ``PlanNotPossible``."""
    objective = (objective or "").strip()
    if not objective:
        raise PlanNotPossible("The mission objective is empty.")

    fragments = _split_compound(objective)
    steps: list[PlannedStep] = []
    previous: int | None = None
    for fragment in fragments:
        step = _plan_fragment(fragment)
        if step is None:
            raise PlanNotPossible(
                f"I can't yet turn '{fragment}' into a real tool step. "
                "Rephrase it, or provide an explicit step list."
            )
        if previous is not None:
            step.depends_on = [previous]
        steps.append(step)
        previous = len(steps) - 1

    if not steps:
        raise PlanNotPossible("No executable steps could be derived from the objective.")

    return validate_plan(Plan(steps=steps, source="rules"))


def plan_from_explicit(steps: list[dict[str, Any]]) -> Plan:
    """Build a plan from an explicit, caller-supplied step list."""
    if not steps:
        raise PlanNotPossible("An explicit plan must contain at least one step.")
    planned: list[PlannedStep] = []
    for raw in steps:
        tool_name = str(raw.get("tool") or raw.get("tool_name") or "").strip()
        if not tool_name:
            raise PlanNotPossible("Every step needs a 'tool' name.")
        planned.append(
            PlannedStep(
                name=str(raw.get("name") or tool_name),
                tool_name=tool_name,
                args=dict(raw.get("args") or {}),
                depends_on=list(raw.get("depends_on") or []),
            )
        )
    return validate_plan(Plan(steps=planned, source="explicit"))


def validate_plan(plan: Plan) -> Plan:
    """Reject plans referencing unknown tools or exceeding the step budget."""
    if len(plan.steps) > settings.mission_max_steps:
        raise PlanNotPossible(
            f"Plan has {len(plan.steps)} steps; the limit is {settings.mission_max_steps}."
        )
    for step in plan.steps:
        if registry.resolve_name(step.tool_name) is None:
            raise PlanNotPossible(f"Unknown tool in plan: {step.tool_name}.")
        for dep in step.depends_on:
            if not isinstance(dep, int) or dep < 0 or dep >= len(plan.steps):
                raise PlanNotPossible(f"Step dependency {dep!r} is out of range.")
    return plan


def plan_hash(plan: Plan) -> str:
    """Stable hash binding an approval to the exact plan it approved."""
    import hashlib

    canonical = json.dumps(
        [
            {"name": s.name, "tool": s.tool_name, "args": s.args, "deps": s.depends_on}
            for s in plan.steps
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_risk(plan: Plan) -> str:
    """The maximum risk across the plan's real tool specs."""
    level = "LOW"
    for step in plan.steps:
        spec = registry.get(registry.resolve_name(step.tool_name) or "")
        if spec is not None and risk_rank(spec.risk_level) > risk_rank(level):
            level = spec.risk_level
    return level


def step_requires_approval(plan: Plan, index: int) -> bool:
    from core.agent.registry import tool_requires_approval

    spec = registry.get(registry.resolve_name(plan.steps[index].tool_name) or "")
    return bool(spec and tool_requires_approval(spec))
