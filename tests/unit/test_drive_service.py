"""
File:   test_drive_service.py
Brief:  DriveService unit tests on the fake transport.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
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


async def test_query_status_no_medium() -> None:
    """A qrypos medium-not-present failure keeps the drive reachable.

    The drive answers vpd normally; only the position query fails with
    the "no cartridge" sense data, so this is not an unreachable state
    and the raw sense dump must not land in ``error``.
    """
    fake = FakeItDtTransport(
        scenarios={
            "vpd": (load_fixture("fixture_vpd.txt"), 0),
            "qrypos": (load_fixture("fixture_qrypos_no_medium.txt"), 2),
            "getparms": (load_fixture("fixture_getparms.txt"), 0),
        }
    )
    service = DriveService(fake, EventBus(), poll_interval_s=60.0)
    status = await service.query_status()

    assert status.reachable is True
    assert status.no_medium is True
    assert status.error is None
    assert status.manufacturer == "IBM"
    assert status.block_id is None
    assert status.compression is True


async def test_query_status_no_medium_initializing_variant() -> None:
    """The 04/02 initializing sense variant is classified the same way."""
    fake = FakeItDtTransport(
        scenarios={
            "vpd": (load_fixture("fixture_vpd.txt"), 0),
            "qrypos": (load_fixture("fixture_qrypos_no_medium_initializing.txt"), 2),
            "getparms": (load_fixture("fixture_getparms.txt"), 0),
        }
    )
    service = DriveService(fake, EventBus(), poll_interval_s=60.0)
    status = await service.query_status()

    assert status.reachable is True
    assert status.no_medium is True
    assert status.error is None


async def test_polling_publishes_no_medium_flip() -> None:
    """Losing the cartridge between polls publishes the no_medium state."""
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
    service = DriveService(fake, bus, poll_interval_s=60.0)

    await service.refresh_and_publish()
    fake.scenarios["qrypos"] = (load_fixture("fixture_qrypos_no_medium.txt"), 2)
    await service.refresh_and_publish()

    assert len(events) == 2
    assert events[1]["reachable"] is True
    assert events[1]["no_medium"] is True
    assert events[1]["error"] is None


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


async def test_published_payload_includes_updated_at() -> None:
    """SSE payloads carry updated_at (the UI renders it directly)."""
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
    service = DriveService(fake, bus, poll_interval_s=60.0)

    await service.refresh_and_publish()

    assert len(events) == 1
    # Regression: significant_state() alone omits updated_at, which made
    # the dashboard crash on "undefined.endsWith".
    assert "updated_at" in events[0]
