"""
File:   drive_service.py
Brief:  Drive/media status via ITDT with polling and SSE publication.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.0
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import structlog
from pydantic import BaseModel

from tapevaultui.infrastructure.itdt_parse import (
    ItDtParseError,
    parse_getparms,
    parse_qrypos,
    parse_vpd,
)
from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtTransport,
    ItDtTransportError,
)
from tapevaultui.services.event_bus import EventBus

logger = structlog.get_logger(__name__)

DRIVE_TOPIC = "drive"


class DriveStatus(BaseModel):
    """Snapshot of drive and media state, safe to serialize to the UI."""

    reachable: bool
    manufacturer: str | None = None
    product_id: str | None = None
    revision: str | None = None
    partition: int | None = None
    block_id: int | None = None
    at_beginning_of_partition: bool | None = None
    compression: bool | None = None
    parms_block_size: int | None = None
    buffered_mode: bool | None = None
    error: str | None = None
    updated_at: datetime

    def significant_state(self) -> dict[str, object]:
        """Return the state fields compared across polls (excludes timestamp).

        Returns:
            Dict of all fields except ``updated_at``.
        """
        data = self.model_dump()
        data.pop("updated_at")
        return data


class DriveService:
    """Queries drive status through ITDT and publishes changes on the bus.

    Status polling runs as one background task; the exclusive drive is
    touched only by sequential ITDT calls (vpd, qrypos, getparms).
    """

    def __init__(
        self,
        transport: ItDtTransport | FakeItDtTransport,
        event_bus: EventBus,
        poll_interval_s: float = 15.0,
    ) -> None:
        """Initialize the service.

        Args:
            transport: ITDT transport (real or fake).
            event_bus: Bus for ``drive`` topic publications.
            poll_interval_s: Seconds between background status refreshes.
        """
        self._transport = transport
        self._event_bus = event_bus
        self._poll_interval_s = poll_interval_s
        self._last_status: DriveStatus | None = None
        self._previous_significant: dict[str, object] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def last_status(self) -> DriveStatus | None:
        """Most recent status snapshot, or None before the first query."""
        return self._last_status

    async def query_status(self) -> DriveStatus:
        """Run a full status pass: vpd, qrypos, getparms.

        All three commands are queried sequentially; a failure of any one
        marks the drive unreachable for this snapshot (partial data of
        successful commands is still filled in).

        Returns:
            The fresh DriveStatus snapshot.
        """
        now = datetime.now(UTC)
        status = DriveStatus(reachable=True, updated_at=now)

        try:
            vpd_result = await self._transport.run(["vpd"])
            if vpd_result.exit_code == 0:
                info = parse_vpd(vpd_result.stdout)
                status.manufacturer = info.manufacturer
                status.product_id = info.product_id
                status.revision = info.revision
            else:
                status.reachable = False
                status.error = vpd_result.stdout.strip() or "vpd failed"
        except (ItDtTransportError, ItDtParseError) as exc:
            status.reachable = False
            status.error = str(exc)

        try:
            qrypos_result = await self._transport.run(["qrypos"])
            if qrypos_result.exit_code == 0:
                position = parse_qrypos(qrypos_result.stdout)
                status.partition = position.logical.partition
                status.block_id = position.logical.block_id
                status.at_beginning_of_partition = (
                    position.logical.at_beginning_of_partition
                )
            elif status.reachable:
                status.reachable = False
                status.error = qrypos_result.stdout.strip() or "qrypos failed"
        except (ItDtTransportError, ItDtParseError) as exc:
            status.reachable = False
            status.error = str(exc)

        try:
            parms_result = await self._transport.run(["getparms"])
            if parms_result.exit_code == 0:
                parms = parse_getparms(parms_result.stdout)
                status.compression = parms.compression
                status.parms_block_size = parms.block_size
                status.buffered_mode = parms.buffered_mode
            elif status.reachable:
                status.reachable = False
                status.error = parms_result.stdout.strip() or "getparms failed"
        except (ItDtTransportError, ItDtParseError) as exc:
            status.reachable = False
            status.error = str(exc)

        self._last_status = status
        return status

    async def refresh_and_publish(self) -> DriveStatus:
        """Query status and publish it if anything changed.

        Returns:
            The fresh DriveStatus snapshot.
        """
        status = await self.query_status()
        previous = self._previous_significant
        current = status.significant_state()
        if current != previous:
            self._previous_significant = current
            await self._event_bus.publish(DRIVE_TOPIC, current)
            logger.info(
                "drive_status_changed",
                reachable=status.reachable,
                error=status.error,
            )
        return status

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._poll_task is not None:
            return
        self._stopping.clear()
        await self.refresh_and_publish()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("drive_polling_started", interval_s=self._poll_interval_s)

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._stopping.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        logger.info("drive_polling_stopped")

    async def _poll_loop(self) -> None:
        """Periodically refresh status until stopped."""
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self._poll_interval_s)
                await self.refresh_and_publish()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("drive_poll_failed")
