"""
File:   schedules.py
Brief:  Schedules overview and per-job schedule update API.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tapevaultui.dependencies import SchedulerDep
from tapevaultui.services.job_defs import JobDefsError, ScheduleSpec

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class ScheduleOverview(BaseModel):
    """One job's schedule overview row."""

    name: str
    kind: str
    days: list[str]
    time: str
    catch_up: str
    last_run_at: str | None
    next_due: str | None
    overdue: bool


class ScheduleUpdate(BaseModel):
    """Schedule section update payload."""

    kind: str = Field(pattern="^(manual|daily|weekly)$")
    days: list[str] = Field(default_factory=list)
    time: str = "03:00"
    catch_up: str = Field(default="once", pattern="^(once|skip)$")


@router.get("", response_model=list[ScheduleOverview])
async def list_schedules(scheduler: SchedulerDep) -> list[ScheduleOverview]:
    """Return the schedule overview for all jobs.

    Args:
        scheduler: Scheduler service.

    Returns:
        Per-job schedule rows.
    """
    return [
        ScheduleOverview.model_validate(row) for row in scheduler.schedules_overview()
    ]


@router.put("/{name}", response_model=ScheduleOverview)
async def update_schedule(
    name: str, payload: ScheduleUpdate, scheduler: SchedulerDep
) -> ScheduleOverview:
    """Update the schedule section of one job's YAML definition.

    Args:
        name: Job name.
        payload: New schedule values.
        scheduler: Scheduler service.

    Returns:
        The refreshed overview row.

    Raises:
        HTTPException: 404 if the job is missing, 422 on validation
            errors.
    """
    store = scheduler.job_defs
    try:
        definition = store.load(name)
        definition.schedule = ScheduleSpec.model_validate(payload.model_dump())
        store.save(definition)
    except JobDefsError as exc:
        code = 404 if "not found" in str(exc) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    rows = [row for row in scheduler.schedules_overview() if row["name"] == name]
    return ScheduleOverview.model_validate(rows[0])
