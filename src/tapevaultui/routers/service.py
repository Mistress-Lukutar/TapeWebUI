"""
File:   service.py
Brief:  Manual drive operations and device statistics API (Service page).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tapevaultui.dependencies import DriveServiceDep, JobRuntimeDep, TransportDep
from tapevaultui.infrastructure.itdt_parse import (
    ItDtParseError,
    parse_device_statistics,
)
from tapevaultui.services.drive_ops import ALL_OPS, DriveOpError, run_drive_op
from tapevaultui.services.drive_service import DriveStatus

router = APIRouter(prefix="/api/service", tags=["service"])


class DeviceStatisticsResponse(BaseModel):
    """Drive identity plus lifecycle statistics from ``devicestatistics``."""

    reachable: bool
    manufacturer: str | None = None
    product_id: str | None = None
    revision: str | None = None
    serial: str | None = None
    statistics: dict[str, str]
    updated_at: datetime


class DriveOpResponse(BaseModel):
    """Result of one manual drive operation."""

    op: str
    output: str
    status: DriveStatus


@router.get("/statistics", response_model=DeviceStatisticsResponse)
async def get_statistics(
    transport: TransportDep, drive: DriveServiceDep
) -> DeviceStatisticsResponse:
    """Collect lifecycle statistics (``devicestatistics``) and identity.

    Args:
        transport: ITDT transport dependency.
        drive: Drive service dependency (identity of the last snapshot).

    Returns:
        Statistics payload with serial number and drive identity.

    Raises:
        HTTPException: 502 if ITDT fails or the output cannot be parsed.
    """
    result = await transport.run(["devicestatistics"])
    if result.exit_code != 0:
        raise HTTPException(
            status_code=502,
            detail=result.stdout.strip() or "devicestatistics failed",
        )
    try:
        stats = parse_device_statistics(result.stdout)
    except ItDtParseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    identity = drive.last_status
    return DeviceStatisticsResponse(
        reachable=identity.reachable if identity else True,
        manufacturer=identity.manufacturer if identity else None,
        product_id=identity.product_id if identity else None,
        revision=identity.revision if identity else None,
        serial=stats.get("drive_sn") or stats.get("drive_mfg_sn"),
        statistics=stats,
        updated_at=datetime.now(UTC),
    )


@router.post("/ops/{op}", response_model=DriveOpResponse)
async def run_op(
    op: str, transport: TransportDep, runtime: JobRuntimeDep, drive: DriveServiceDep
) -> DriveOpResponse:
    """Run one manual drive/media operation.

    Mirrors the interactive ITDT-SE operations that make sense for a
    single LTO drive: tur, load, unload, lock/unlock, reserve/release,
    rewind, seod, erase (short/long).

    Args:
        op: Operation name.
        transport: ITDT transport dependency.
        runtime: Job runtime, to refuse operations while a job is active.
        drive: Drive service dependency for the post-op refresh.

    Returns:
        DriveOpResponse with raw ITDT output and the refreshed status.

    Raises:
        HTTPException: 404 for unknown ops, 409 while a job is active or
            when the drive refuses the operation.
    """
    if op not in ALL_OPS:
        raise HTTPException(status_code=404, detail=f"Unknown operation: {op}")
    if runtime.active is not None:
        raise HTTPException(
            status_code=409,
            detail="A job is running; cancel it before manual drive operations",
        )
    try:
        output = await run_drive_op(transport, op)
    except DriveOpError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    status = await drive.refresh_and_publish()
    return DriveOpResponse(op=op, output=output, status=status)
