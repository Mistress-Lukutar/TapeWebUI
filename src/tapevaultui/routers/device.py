"""
File:   device.py
Brief:  Drive status and maintenance API endpoints.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.3
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from tapevaultui.dependencies import DriveServiceDep, JobRuntimeDep, TransportDep
from tapevaultui.services.drive_service import DriveStatus
from tapevaultui.services.session_io import erase_tape_short

router = APIRouter(prefix="/api/drive", tags=["drive"])


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


@router.post("/erase")
async def erase_tape(
    transport: TransportDep, runtime: JobRuntimeDep, drive: DriveServiceDep
) -> DriveStatus:
    """Short-erase the loaded tape (rewind + erase from BOP).

    Destroys all data on the current partition; used to reclaim foreign
    or damaged tapes for TapeWebUI sessions.

    Args:
        transport: ITDT transport dependency.
        runtime: Job runtime, to refuse erasing while a job is running.
        drive: Drive service dependency for the post-erase refresh.

    Returns:
        The refreshed DriveStatus after the erase.

    Raises:
        HTTPException: 409 if a job is active or the drive refuses the
            erase (no medium / write-protected).
    """
    if runtime.active is not None:
        raise HTTPException(
            status_code=409, detail="A job is running; cancel it before erasing"
        )
    try:
        await erase_tape_short(transport)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await drive.refresh_and_publish()
