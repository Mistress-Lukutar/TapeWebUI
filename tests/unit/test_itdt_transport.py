"""
File:   test_itdt_transport.py
Brief:  Unit tests for the real and fake ITDT transports.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.1.1
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtTimeoutError,
    ItDtTransport,
    ItDtTransportError,
)


def test_build_args_includes_generic_dd_and_device() -> None:
    """Command lines always carry -force-generic-dd and -f <device>."""
    transport = ItDtTransport(
        itdt_exe=Path("C:/Program Files/ITDT/itdt.exe"),
        device_name="\\\\.\\tape0",
    )
    argv = transport.build_args(["vpd"])
    assert argv == [
        "C:\\Program Files\\ITDT\\itdt.exe",
        "-force-generic-dd",
        "-f",
        "\\\\.\\tape0",
        "vpd",
    ]


def test_build_args_without_generic_dd() -> None:
    """-force-generic-dd can be disabled for non-tape ITDT invocations."""
    transport = ItDtTransport(
        itdt_exe=Path("itdt.exe"),
        device_name="\\\\.\\scsi1:",
        force_generic_dd=False,
    )
    argv = transport.build_args(["scan"])
    assert argv == ["itdt.exe", "-f", "\\\\.\\scsi1:", "scan"]


async def test_fake_transport_records_and_serves_scenarios() -> None:
    """The fake records commands and serves the scenario table."""
    fake = FakeItDtTransport(scenarios={"vpd": ("Manufacturer .......... IBM", 0)})
    result = await fake.run(["vpd"])
    assert result.exit_code == 0
    assert "IBM" in result.stdout
    assert fake.commands == [("vpd",)]


async def test_fake_transport_default_response() -> None:
    """Unknown subcommands fall back to the default response."""
    fake = FakeItDtTransport(default_response=("Exit with code: 0", 0))
    result = await fake.run(["rewind"])
    assert result.exit_code == 0
    assert fake.commands == [("rewind",)]


async def test_missing_executable_raises() -> None:
    """A missing executable raises ItDtTransportError."""
    transport = ItDtTransport(
        itdt_exe=Path("Z:/definitely/missing/itdt.exe"),
        device_name="\\\\.\\tape0",
        open_retry_s=0.0,
        open_retries=0,
    )
    with pytest.raises(ItDtTransportError, match="not found"):
        await transport.run(["vpd"])


async def test_run_once_timeout_raises() -> None:
    """A hung command hits the time budget and raises ItDtTimeoutError."""
    transport = ItDtTransport(
        itdt_exe=Path("ping.exe"),
        device_name="\\\\.\\tape0",
        open_retry_s=0.0,
        open_retries=0,
    )
    with pytest.raises(ItDtTimeoutError):
        await transport._run_once(
            ["ping", "-n", "30", "127.0.0.1"],
            timeout_s=1.0,
        )
