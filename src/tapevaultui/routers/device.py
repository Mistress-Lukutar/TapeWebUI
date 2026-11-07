"""
File:   device.py
Brief:  Drive status, maintenance and tape-verification API endpoints.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from tapevaultui.dependencies import (
    CatalogDep,
    DriveServiceDep,
    JobRuntimeDep,
    SettingsDep,
    TransportDep,
)
from tapevaultui.services.drive_service import DriveStatus
from tapevaultui.services.session_io import erase_tape_short
from tapevaultui.services.tape_sync import (
    VERIFY_MATCH,
    DeepScanReport,
    TapeSyncError,
    apply_erase_attribution,
    attribute_erase,
    compare_with_catalog,
    scan_tape_contents,
    verify_loaded_tape,
)

router = APIRouter(prefix="/api/drive", tags=["drive"])


class EraseResponse(BaseModel):
    """Result of a manual erase with catalog attribution."""

    status: DriveStatus
    catalog_message: str


class VerifyTapeResponse(BaseModel):
    """Outcome of a quick or deep verification of the loaded tape."""

    status: str
    tape_id: int | None = None
    tape_uuid: str | None = None
    tape_label: str = ""
    detail: str = ""
    report: DeepScanReport | None = None


@router.get("/status", response_model=DriveStatus)
async def get_status(drive: DriveServiceDep) -> DriveStatus:
    """Return the current drive/media status snapshot.

    Args:
        drive: Drive service dependency.

    Returns:
        The latest DriveStatus (refreshed in the background poll loop).
    """
    if drive.last_status is None:
        return await drive.query_status()
    return drive.last_status


@router.post("/refresh", response_model=DriveStatus)
async def refresh_status(drive: DriveServiceDep) -> DriveStatus:
    """Force an immediate status refresh.

    Args:
        drive: Drive service dependency.

    Returns:
        The freshly queried DriveStatus.
    """
    return await drive.refresh_and_publish()


@router.post("/erase", response_model=EraseResponse)
async def erase_tape(
    transport: TransportDep,
    runtime: JobRuntimeDep,
    drive: DriveServiceDep,
    catalog: CatalogDep,
    settings: SettingsDep,
) -> EraseResponse:
    """Short-erase the loaded tape (rewind + erase from BOP).

    Destroys all data on the current partition; used to reclaim foreign
    or damaged tapes for TapeWebUI sessions. Before the erase the
    cartridge is identified by its t0 header; when it matches a catalog
    tape, all of that tape's sessions are automatically marked erased
    after the erase succeeds. Otherwise a stale-catalog notice is
    recorded.

    Args:
        transport: ITDT transport dependency.
        runtime: Job runtime, to refuse erasing while a job is running.
        drive: Drive service dependency for the post-erase refresh.
        catalog: Catalog repository for the attribution sync.
        settings: Application settings (work dir for header reads).

    Returns:
        The refreshed DriveStatus plus the catalog sync message.

    Raises:
        HTTPException: 409 if a job is active or the drive refuses the
            erase (no medium / write-protected).
    """
    if runtime.active is not None:
        raise HTTPException(
            status_code=409, detail="A job is running; cancel it before erasing"
        )
    attributed_tape_id = await attribute_erase(
        transport, settings.data_dir / "tmp", catalog
    )
    try:
        await erase_tape_short(transport)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    message = apply_erase_attribution(catalog, attributed_tape_id)
    return EraseResponse(
        status=await drive.refresh_and_publish(), catalog_message=message
    )


@router.post("/verify-tape", response_model=VerifyTapeResponse)
async def verify_tape(
    transport: TransportDep,
    runtime: JobRuntimeDep,
    catalog: CatalogDep,
    settings: SettingsDep,
    deep: bool = Query(default=False),
) -> VerifyTapeResponse:
    """Verify the loaded cartridge against the catalog.

    Quick mode reads the t0 header only (match / blank / foreign). Deep
    mode additionally walks the whole tape, reading every session header
    and comparing the on-tape sessions with the catalog.

    Args:
        transport: ITDT transport dependency.
        runtime: Job runtime, to refuse verification while a job runs.
        catalog: Catalog repository.
        settings: Application settings (work dir for header reads).
        deep: Run the full content scan instead of the header check.

    Returns:
        The verification result; deep mode includes the comparison
        report.

    Raises:
        HTTPException: 409 if a job is active.
    """
    if runtime.active is not None:
        raise HTTPException(
            status_code=409,
            detail="A job is running; cancel it before verifying the tape",
        )
    work_dir = settings.data_dir / "tmp"
    result = await verify_loaded_tape(transport, work_dir, catalog)
    response = VerifyTapeResponse(
        status=result.status,
        tape_id=result.tape_id,
        tape_uuid=result.tape_uuid,
        tape_label=result.tape_label,
        detail=result.detail,
    )
    if not deep or result.status != VERIFY_MATCH or result.tape_id is None:
        return response
    try:
        on_tape = await scan_tape_contents(transport, work_dir)
    except TapeSyncError as exc:
        response.status = "error"
        response.detail = str(exc)
        return response
    response.report = compare_with_catalog(on_tape, catalog, result.tape_id)
    return response
