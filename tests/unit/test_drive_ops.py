"""
File:   test_drive_ops.py
Brief:  Unit tests for manual drive operations on the fake transport.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

import pytest

from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtResult,
)
from tapevaultui.services.drive_ops import (
    ALL_OPS,
    DriveOpError,
    DriveOpsState,
    run_drive_op,
)
from tests.conftest import load_fixture


@pytest.mark.unit
async def test_simple_ops_issue_itdt_commands() -> None:
    """Simple ops map to the expected ITDT subcommands."""
    fake = FakeItDtTransport()
    for op, command in [
        ("tur", "tur"),
        ("load", "load"),
        ("unload", "unload"),
        ("lock", "prevent"),
        ("unlock", "allow"),
        ("reserve", "reserve"),
        ("release", "release"),
    ]:
        await run_drive_op(fake, op)
        assert (command,) == fake.commands[-1]


@pytest.mark.unit
async def test_composite_ops() -> None:
    """Rewind/seod/erase ops reuse the session_io command sequences."""
    fake = FakeItDtTransport()
    output = await run_drive_op(fake, "rewind")
    assert fake.commands == [("rewind",)]
    assert "BOP" in output

    await run_drive_op(fake, "seod")
    assert fake.commands[-1] == ("seod",)

    output = await run_drive_op(fake, "erase_short")
    assert ("erase", "-short") in fake.commands
    assert "Short erase" in output

    output = await run_drive_op(fake, "erase_long")
    assert ("erase", "-long") in fake.commands
    assert "Long erase" in output


@pytest.mark.unit
async def test_unknown_op_raises() -> None:
    """Unknown operation names are rejected."""
    fake = FakeItDtTransport()
    with pytest.raises(DriveOpError, match="Unknown"):
        await run_drive_op(fake, "format")
    assert fake.commands == []


@pytest.mark.unit
async def test_failing_command_raises() -> None:
    """A non-zero ITDT exit code raises DriveOpError with output."""
    fake = FakeItDtTransport(scenarios={"load": ("Open failed: busy", 2)})
    with pytest.raises(DriveOpError, match="load failed"):
        await run_drive_op(fake, "load")


class _LockedUnloadFake(FakeItDtTransport):
    """Fake whose first unload fails with 53/02, later calls succeed."""

    def __init__(self) -> None:
        super().__init__()
        self.unload_calls = 0

    async def run(self, args: list[str], timeout_s: int | None = None) -> ItDtResult:
        self.commands.append(tuple(args))
        if args[0] == "unload":
            self.unload_calls += 1
            if self.unload_calls == 1:
                return ItDtResult(
                    args=tuple(self.build_args(args)),
                    exit_code=2,
                    stdout=load_fixture("fixture_unload_prevented.txt"),
                    stderr="",
                    duration_s=0.0,
                )
            return ItDtResult(
                args=tuple(self.build_args(args)),
                exit_code=0,
                stdout="Unloading tape... done\n",
                stderr="",
                duration_s=0.0,
            )
        return ItDtResult(
            args=tuple(self.build_args(args)),
            exit_code=0,
            stdout="",
            stderr="",
            duration_s=0.0,
        )


@pytest.mark.unit
async def test_unlocked_unload_does_not_send_allow() -> None:
    """A healthy unload never issues the allow-removal workaround."""
    fake = FakeItDtTransport()
    await run_drive_op(fake, "unload")
    assert fake.commands == [("unload",)]


@pytest.mark.unit
async def test_unload_retries_after_allow_when_locked() -> None:
    """Unload rejected with 53/02 unlocks via allow, then ejects."""
    fake = _LockedUnloadFake()
    output = await run_drive_op(fake, "unload")

    assert fake.commands == [("unload",), ("allow",), ("unload",)]
    assert "done" in output


@pytest.mark.unit
async def test_unload_still_locked_raises_friendly_error() -> None:
    """If the cartridge stays locked, a clear message is raised."""
    fake = FakeItDtTransport(
        scenarios={
            "unload": (load_fixture("fixture_unload_prevented.txt"), 2),
            "allow": ("", 0),
        }
    )
    with pytest.raises(DriveOpError, match="locked"):
        await run_drive_op(fake, "unload")
    assert fake.commands == [("unload",), ("allow",), ("unload",)]


@pytest.mark.unit
async def test_paired_ops_track_toggle_state() -> None:
    """Successful paired ops flip the tracked toggle state."""
    fake = FakeItDtTransport()
    state = DriveOpsState()

    await run_drive_op(fake, "lock", state)
    assert state.door_locked is True
    await run_drive_op(fake, "unlock", state)
    assert state.door_locked is False

    await run_drive_op(fake, "reserve", state)
    assert state.unit_reserved is True
    await run_drive_op(fake, "release", state)
    assert state.unit_reserved is False

    # A successful unload leaves no media (and no lock) behind.
    await run_drive_op(fake, "lock", state)
    assert state.door_locked is True
    await run_drive_op(fake, "unload", state)
    assert state.door_locked is False

    # Failed ops do not flip the state.
    fake.scenarios["release"] = ("boom", 2)
    state.unit_reserved = True
    with pytest.raises(DriveOpError):
        await run_drive_op(fake, "release", state)
    assert state.unit_reserved is True


@pytest.mark.unit
def test_all_ops_consistent() -> None:
    """Composite ops are part of ALL_OPS and do not overlap SIMPLE_OPS."""
    assert {
        "tur",
        "load",
        "unload",
        "lock",
        "unlock",
        "reserve",
        "release",
        "rewind",
        "seod",
        "erase_short",
        "erase_long",
    } == ALL_OPS
