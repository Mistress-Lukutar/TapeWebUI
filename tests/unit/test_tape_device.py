"""
File:   test_tape_device.py
Brief:  TapeDeviceReader open tests on a scripted fake kernel32.
Author: Mistress-Lukutar
Date:   2026-11-09
Version: v0.11.1
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

import pytest

from tapevaultui.infrastructure import tape_device
from tapevaultui.infrastructure.tape_device import (
    DirectReadUnavailableError,
    TapeDeviceReader,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="tape_device binds the Win32 tape API",
)


class FakeKernel32:
    """Scripted CreateFileW/SetTapeParameters/CloseHandle stand-in.

    Args:
        open_errors: Outcome per CreateFileW attempt in order; a WinError
            code fails that attempt, None succeeds. The last entry
            repeats for any further attempts.
        set_params_rc: Value SetTapeParameters returns (0 = NO_ERROR).
    """

    def __init__(
        self,
        open_errors: list[int | None],
        set_params_rc: int = 0,
    ) -> None:
        """Initialize the fake with its attempt script.

        Args:
            open_errors: Per-attempt CreateFileW outcomes.
            set_params_rc: SetTapeParameters return value.
        """
        self.open_errors = open_errors
        self.set_params_rc = set_params_rc
        self.open_calls = 0
        self.set_params_calls = 0
        self.closed: list[int] = []

    def CreateFileW(self, *_args: Any) -> int | None:
        """Return the scripted outcome for this attempt.

        Args:
            *_args: Ignored CreateFileW parameters.

        Returns:
            A fake valid handle, or INVALID_HANDLE_VALUE on failure.
        """
        outcome = self.open_errors[min(self.open_calls, len(self.open_errors) - 1)]
        self.open_calls += 1
        if outcome is not None:
            ctypes.set_last_error(outcome)
            return ctypes.c_void_p(-1).value
        return 0x1234

    def SetTapeParameters(self, *_args: Any) -> int:
        """Return the scripted media-parameter result.

        Args:
            *_args: Ignored SetTapeParameters parameters.

        Returns:
            The scripted return code.
        """
        self.set_params_calls += 1
        return self.set_params_rc

    def CloseHandle(self, handle: int) -> int:
        """Record the closed handle.

        Args:
            handle: The handle being closed.

        Returns:
            Success indicator.
        """
        self.closed.append(handle)
        return 1


def _patch_kernel32(monkeypatch: pytest.MonkeyPatch, fake: FakeKernel32) -> None:
    """Point tape_device._get_kernel32 at the fake.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        fake: The FakeKernel32 to hand out.
    """
    monkeypatch.setattr(tape_device, "_get_kernel32", lambda: fake)


def test_open_retries_sharing_violation_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient sharing violation is retried; the second attempt opens."""
    fake = FakeKernel32(open_errors=[32, None])
    _patch_kernel32(monkeypatch, fake)
    reader = TapeDeviceReader("\\\\.\\tape0", open_retry_s=0.0)

    reader.open()

    assert fake.open_calls == 2
    assert fake.set_params_calls == 1
    reader.close()


def test_open_raises_after_exhausted_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent sharing violations raise after 1 + open_retries attempts."""
    fake = FakeKernel32(open_errors=[32])
    _patch_kernel32(monkeypatch, fake)
    reader = TapeDeviceReader("\\\\.\\tape0", open_retries=2, open_retry_s=0.0)

    with pytest.raises(DirectReadUnavailableError, match="WinError 32"):
        reader.open()

    assert fake.open_calls == 3  # first attempt + 2 retries
    assert fake.set_params_calls == 0


def test_open_does_not_retry_non_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors other than sharing violations fail on the first attempt."""
    fake = FakeKernel32(open_errors=[2])  # ERROR_FILE_NOT_FOUND
    _patch_kernel32(monkeypatch, fake)
    reader = TapeDeviceReader("\\\\.\\tape0", open_retry_s=0.0)

    with pytest.raises(DirectReadUnavailableError, match="WinError 2"):
        reader.open()

    assert fake.open_calls == 1


def test_open_succeeds_when_set_tape_parameters_returns_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SetTapeParameters rc=0 (NO_ERROR) is success, not failure.

    Regression: the check used to treat rc==0 as a failure, which made
    every healthy open raise DirectReadUnavailableError and silently
    demoted verify to the ITDT fallback.
    """
    fake = FakeKernel32(open_errors=[None], set_params_rc=0)
    _patch_kernel32(monkeypatch, fake)
    reader = TapeDeviceReader("\\\\.\\tape0")

    reader.open()  # must not raise

    assert fake.set_params_calls == 1
    reader.close()


def test_open_raises_when_set_tape_parameters_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonzero SetTapeParameters code raises and closes the handle."""
    fake = FakeKernel32(open_errors=[None], set_params_rc=21)  # ERROR_NOT_READY
    _patch_kernel32(monkeypatch, fake)
    reader = TapeDeviceReader("\\\\.\\tape0")

    with pytest.raises(DirectReadUnavailableError, match="SetTapeParameters"):
        reader.open()

    assert fake.closed == [0x1234]
