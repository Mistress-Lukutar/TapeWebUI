"""
File:   jobs.py
Brief:  Jobs API: YAML CRUD, run/cancel, runtime queue status.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tapevaultui.dependencies import JobDefStoreDep, JobRuntimeDep
from tapevaultui.services.job_defs import JobDefinition, JobDefsError
from tapevaultui.services.job_runtime import QueueEntry

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobYamlPayload(BaseModel):
    """Request/response wrapper carrying a job definition as YAML text."""

    content: str = Field(description="Job definition YAML document")


class JobOut(BaseModel):
    """Job list item."""

    name: str
    enabled: bool = True
    schedule_kind: str = "manual"
    yaml: str = Field(description="Job definition YAML document")


class RunOut(BaseModel):
    """Run acceptance response."""

    accepted: bool
    queue_position: int
    run_id: int


@router.get("")
async def list_jobs(store: JobDefStoreDep) -> list[JobOut]:
    """List all job definitions.

    Args:
        store: Job definition store.

    Returns:
        Jobs with their YAML content.
    """
    jobs: list[JobOut] = []
    for definition in store.list_jobs():
        yaml_text = (store.jobs_dir / f"{definition.name}.yaml").read_text("utf-8")
        jobs.append(
            JobOut(
                name=definition.name,
                enabled=True,
                schedule_kind=definition.schedule.kind,
                yaml=yaml_text,
            )
        )
    return jobs


@router.post("", status_code=201)
async def create_job(payload: JobYamlPayload, store: JobDefStoreDep) -> JobDefinition:
    """Create a job from a YAML document.

    Args:
        payload: YAML content.
        store: Job definition store.

    Returns:
        The parsed and stored definition.

    Raises:
        HTTPException: 422 on validation errors, 409 if the job exists.
    """
    try:
        definition = store.parse_yaml(payload.content)
    except JobDefsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if (store.jobs_dir / f"{definition.name}.yaml").exists():
        raise HTTPException(status_code=409, detail=f"Job exists: {definition.name}")
    store.save(definition)
    return definition


@router.get("/{name}")
async def get_job(name: str, store: JobDefStoreDep) -> JobOut:
    """Return one job with its YAML content.

    Args:
        name: Job name.
        store: Job definition store.

    Returns:
        The job definition.

    Raises:
        HTTPException: 404 if the job does not exist.
    """
    try:
        definition = store.load(name)
    except JobDefsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    yaml_text = (store.jobs_dir / f"{name}.yaml").read_text("utf-8")
    return JobOut(
        name=definition.name,
        enabled=True,
        schedule_kind=definition.schedule.kind,
        yaml=yaml_text,
    )


@router.put("/{name}")
async def update_job(
    name: str, payload: JobYamlPayload, store: JobDefStoreDep
) -> JobDefinition:
    """Validate and save an updated job definition.

    Args:
        name: Existing job name.
        payload: YAML content (name inside must match).
        store: Job definition store.

    Returns:
        The stored definition.

    Raises:
        HTTPException: 404 if missing, 422 on validation errors.
    """
    try:
        store.load(name)  # existence check
        definition = store.parse_yaml(payload.content, expected_name=name)
    except JobDefsError as exc:
        code = 404 if "not found" in str(exc) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    store.save(definition)
    return definition


@router.delete("/{name}")
async def delete_job(name: str, store: JobDefStoreDep) -> dict[str, bool]:
    """Delete a job definition.

    Args:
        name: Job name.
        store: Job definition store.

    Returns:
        Deletion confirmation.

    Raises:
        HTTPException: 404 if missing.
    """
    try:
        store.delete(name)
    except JobDefsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True}


@router.post("/{name}/run")
async def run_job(name: str, store: JobDefStoreDep, runtime: JobRuntimeDep) -> RunOut:
    """Queue a job for execution.

    Args:
        name: Job name.
        store: Job definition store.
        runtime: Job runtime.

    Returns:
        Run acceptance with queue position.

    Raises:
        HTTPException: 404 if missing.
    """
    try:
        definition = store.load(name)
    except JobDefsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    entry: QueueEntry = runtime.enqueue(definition)
    return RunOut(
        accepted=True, queue_position=runtime.queue_length(), run_id=entry.run_id
    )


@router.post("/cancel")
async def cancel_job(runtime: JobRuntimeDep) -> dict[str, bool]:
    """Cancel the active job and drop queued ones.

    Args:
        runtime: Job runtime.

    Returns:
        Whether anything was cancelled.
    """
    return {"cancelled": runtime.request_cancel()}


@router.get("/runtime/status")
async def runtime_status(runtime: JobRuntimeDep) -> dict[str, object]:
    """Return the runtime queue status.

    Args:
        runtime: Job runtime.

    Returns:
        Active job progress and queued jobs.
    """
    return runtime.status()
