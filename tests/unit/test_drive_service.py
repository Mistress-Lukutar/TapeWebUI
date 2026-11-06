"""
File:   test_drive_service.py
Brief:  DriveService unit tests on the fake transport.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtResult,
    ItDtTransportError,
)
from tapevaultui.services.drive_service import DriveService
from tapevaultui.services.event_bus import EventBus
from tests.conftest import load_fixture


async def test_query_status_full_snapshot() -> None:
    """A full query on healthy fixtures yields a reachable status."""
    fake = FakeItDtTransport(
        scenarios={
            "vpd": (load_fixture("fixture_vpd.txt"), 0),
            "qrypos": (load_fixture("fixture_qrypos.txt"), 0),
            "getparms": (load_fixture("fixture_getparms.txt"), 0),
        }
    )
    service = DriveService(fake, EventBus(), poll_interval_s=60.0)
    status = await service.query_status()

    assert status.reachable is True
    assert status.error is None
    assert status.manufacturer == "IBM"
    assert status.product_id == "ULTRIUM-TD3"
    assert status.revision == "93G0"
    assert status.partition == 0
    assert status.block_id == 0
    assert status.at_beginning_of_partition is True
    assert status.compression is True
    assert status.parms_block_size == 65536
    assert fake.commands == [("vpd",), ("qrypos",), ("getparms",)]


async def test_query_status_unreachable_on_transport_error() -> None:
    """If a command cannot run, the drive is marked unreachable."""

    class ExplodingFake(FakeItDtTransport):
        async def run(
            self, args: list[str], timeout_s: int | None = None
        ) -> ItDtResult:
            raise ItDtTransportError("ITDT executable not found")

    service = DriveService(ExplodingFake(), EventBus())
    status = await service.query_status()

    assert status.reachable is False
    assert status.error is not None
    assert "ITDT" in status.error


async def test_polling_publishes_on_change() -> None:
    """The poll loop publishes to the drive topic when state changes."""
    events: list[dict[str, object]] = []

    async def collect(payload: dict[str, object]) -> None:
        events.append(payload)

    bus = EventBus()
    bus.subscribe("drive", collect)
    fake = FakeItDtTransport(
        scenarios={
            "vpd": (load_fixture("fixture_vpd.txt"), 0),
            "qrypos": (load_fixture("fixture_qrypos.txt"), 0),
            "getparms": (load_fixture("fixture_getparms.txt"), 0),
        }
    )
    service = DriveService(fake, bus, poll_interval_s=0.05)

    await service.start()
    await service.stop()

    assert len(events) == 1  # only the initial change (state stays stable)
    assert events[0]["reachable"] is True


async def test_polling_publishes_state_flip() -> None:
    """A state change between polls is published again."""
    events: list[dict[str, object]] = []

    async def collect(payload: dict[str, object]) -> None:
        events.append(payload)

    bus = EventBus()
    bus.subscribe("drive", collect)
    fake = FakeItDtTransport(
        scenarios={
            "vpd": (load_fixture("fixture_vpd.txt"), 0),
            "qrypos": (load_fixture("fixture_qrypos.txt"), 0),
            "getparms": (load_fixture("fixture_getparms.txt"), 0),
        }
    )
    service = DriveService(fake, bus, poll_interval_s=0.05)

    await service.refresh_and_publish()
    fake.scenarios["vpd"] = (
        "Open failed: The system cannot find the file specified\n",
        2,
    )
    await service.refresh_and_publish()

    assert len(events) == 2
    assert events[1]["reachable"] is False
