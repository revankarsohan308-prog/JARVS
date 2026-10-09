"""Mission Center API (Reliability Addendum Module 2).

Every route is owner-scoped and RBAC-gated. Running a mission still passes
each step through the tool permission engine; the API never executes a tool
directly, and never treats an ordinary message as approval (approval is an
explicit, plan-bound POST).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import DbDep, require_permission
from core.missions import planner
from core.missions import service as missions
from core.permissions.rbac import Permission
from database.models import User

router = APIRouter(prefix="/missions", tags=["missions"])

MissionReader = Annotated[User, Depends(require_permission(Permission.MISSION_READ))]
MissionWriter = Annotated[User, Depends(require_permission(Permission.MISSION_WRITE))]


class StepIn(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)


class MissionCreateIn(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)
    steps: list[StepIn] | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


@router.post("", status_code=201)
def create_mission(payload: MissionCreateIn, user: MissionWriter, db: DbDep):
    steps = (
        [s.model_dump() for s in payload.steps] if payload.steps is not None else None
    )
    try:
        mission = missions.create_mission(
            db,
            user.id,
            payload.objective,
            steps=steps,
            idempotency_key=payload.idempotency_key,
            actor=f"user:{user.username}",
        )
    except planner.PlanNotPossible as exc:
        # Honest: no fake plan is invented.
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return missions.serialize_mission(db, mission)


@router.get("")
def list_missions(user: MissionReader, db: DbDep):
    rows = missions.list_missions(db, user.id)
    return {
        "missions": [
            missions.serialize_mission(db, m, include_history=False) for m in rows
        ],
        "counts": missions.mission_counts(db, user.id),
    }


@router.get("/{mission_id}")
def get_mission(mission_id: str, user: MissionReader, db: DbDep):
    try:
        mission = missions.get_owned_mission(db, user.id, mission_id)
    except missions.MissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return missions.serialize_mission(db, mission)


class ApprovalIn(BaseModel):
    approve: bool


@router.post("/{mission_id}/approve")
def approve_mission(mission_id: str, payload: ApprovalIn, user: MissionWriter, db: DbDep):
    try:
        mission = missions.approve_mission(
            db, user.id, mission_id, payload.approve, actor=f"user:{user.username}"
        )
    except missions.MissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return missions.serialize_mission(db, mission, include_history=False)


@router.post("/{mission_id}/run")
def run_mission(mission_id: str, user: MissionWriter, db: DbDep):
    try:
        mission = missions.run_mission(
            db, user.id, mission_id, role=user.role, actor=f"user:{user.username}"
        )
    except missions.MissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return missions.serialize_mission(db, mission)


@router.post("/{mission_id}/pause")
def pause_mission(mission_id: str, user: MissionWriter, db: DbDep):
    try:
        mission = missions.pause_mission(
            db, user.id, mission_id, actor=f"user:{user.username}", reason="Paused by the owner."
        )
    except missions.MissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return missions.serialize_mission(db, mission, include_history=False)


@router.post("/{mission_id}/resume")
def resume_mission(mission_id: str, user: MissionWriter, db: DbDep):
    try:
        mission = missions.resume_mission(
            db, user.id, mission_id, role=user.role, actor=f"user:{user.username}"
        )
    except missions.MissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return missions.serialize_mission(db, mission)


@router.post("/{mission_id}/cancel")
def cancel_mission(mission_id: str, user: MissionWriter, db: DbDep):
    try:
        mission = missions.cancel_mission(db, user.id, mission_id, actor=f"user:{user.username}")
    except missions.MissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return missions.serialize_mission(db, mission, include_history=False)


@router.get("/{mission_id}/report")
def mission_report(mission_id: str, user: MissionReader, db: DbDep):
    import json

    try:
        mission = missions.get_owned_mission(db, user.id, mission_id)
    except missions.MissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {
        "id": mission.id,
        "state": mission.state,
        "progress": mission.progress,
        "verification": json.loads(mission.verification_json or "{}"),
        "final_report": mission.final_report or "This mission has not finished yet.",
    }
