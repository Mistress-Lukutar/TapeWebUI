"""
File:   restore.py
Brief:  Restore API: plan, run, cancel, status.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from tapevaultui.dependencies import CatalogDep, RestoreEngineDep
from tapevaultui.services.restore_engine import (
    RestoreEngineError,
    RestorePlan,
    RestoreRequest,
)

router = APIRouter(prefix="/api/restore", tags=["restore"])


@router.post("/plan", response_model=RestorePlan)
async def build_plan(
    payload: RestoreRequest, engine: RestoreEngineDep, catalog: CatalogDep
) -> RestorePlan:
    """Build a restore plan from file ids and subtree selections.

    Args:
        payload: File selection.
        engine: Restore engine.
        catalog: Catalog repository (unused plan input, reserved).

    Returns:
        The ordered restore plan (one tape group per tape).

    Raises:
        HTTPException: 422 on invalid selection.
    """
    try:
        return engine.build_plan(payload.file_ids, payload.selections)
    except RestoreEngineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/run")
async def run_restore(
    payload: RestoreRequest, engine: RestoreEngineDep, catalog: CatalogDep
) -> dict[str, object]:
    """Start a restore run in the background.

    Args:
        payload: Selection, destination and collision options.
        engine: Restore engine.
        catalog: Catalog repository.

    Returns:
        Run id and accepted flag.

    Raises:
        HTTPException: 409 if a restore is already running, 422 on
            invalid selection.
    """
    if engine.is_running():
        raise HTTPException(status_code=409, detail="Restore already running")
    try:
        plan = engine.build_plan(payload.file_ids, payload.selections)
    except RestoreEngineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    run = catalog.create_job_run()
    first_session = next(
        (item.session_id for group in plan.tapes for item in group.items), None
    )
    catalog.update_job_run(
        run.id,
        status="running",
        plan_json=plan.model_dump(),
        session_id=first_session,
    )
    engine.start_run(payload, plan, run.id)
    return {"accepted": True, "run_id": run.id}


@router.post("/cancel")
async def cancel_restore(engine: RestoreEngineDep) -> dict[str, bool]:
    """Cancel the running restore (cooperative).

    Args:
        engine: Restore engine.

    Returns:
        Whether a run was cancelled.
    """
    return {"cancelled": engine.request_cancel()}


@router.get("/status")
async def restore_status(engine: RestoreEngineDep) -> dict[str, object]:
    """Return the current restore status.

    Args:
        engine: Restore engine.

    Returns:
        Restore progress or idle marker.
    """
    status: dict[str, object] | None = engine.status()
    return {"active": status}
