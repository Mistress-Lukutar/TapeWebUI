"""
File:   test_drive_ops.py
Brief:  Unit tests for manual drive operations on the fake transport.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

import pytest

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.drive_ops import ALL_OPS, DriveOpError, run_drive_op


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
