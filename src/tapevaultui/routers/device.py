"""
File:   device.py
Brief:  Drive status API endpoints.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

from fastapi import APIRouter

from tapevaultui.dependencies import DriveServiceDep
from tapevaultui.services.drive_service import DriveStatus

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
