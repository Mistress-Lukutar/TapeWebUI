"""
File:   tape_device.py
Brief:  Direct \\.\tape0 streaming reader via kernel32 ReadFile + SetTapeParameters.
Author: Mistress-Lukutar
Date:   2026-11-09
Version: v0.11.1
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import sys
import time
from collections.abc import Callable
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: 1 MiB read buffer for each ReadFile call.
READ_BUFFER_BYTES = 1024 * 1024

#: WinError code returned by ReadFile when a filemark is encountered.
ERROR_FILEMARK_DETECTED = 1101

#: WinError code for ERROR_SHARING_VIOLATION on CreateFileW. The tape
#: driver is exclusive-open; the error is transient when another process
#: (e.g. a concurrently finishing ITDT command) still holds the device.
ERROR_SHARING_VIOLATION = 32

#: SetTapeParameters option value to set BlockSize (TAPE_SET_MEDIA_PARAMETERS).
TAPE_SET_MEDIA_PARAMS = 0

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TapeDeviceError(RuntimeError):
    """Base exception for direct tape device read failures."""


class DirectReadUnavailableError(TapeDeviceError):
    """Raised when the device cannot be opened or configured for direct reads.

    Covers: device missing, sharing violation, SetTapeParameters failure,
    and non-Windows platform.
    """


# ---------------------------------------------------------------------------
# Protocol — structural interface the backup engine depends on
# ---------------------------------------------------------------------------


class DirectTapeReader(Protocol):
    """Structural interface for reading one tape file with SHA-256 hashing."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def read_file_hashed(
        self, progress_cb: Callable[[int], None] | None = None
    ) -> tuple[int, str]:
        """Read the current tape file and return (bytes_read, sha256_hex).

        ``progress_cb`` is called with the cumulative byte count after each
        chunk read.
        """


# ---------------------------------------------------------------------------
# ctypes helpers — lazy so importing on non-Windows never crashes
# ---------------------------------------------------------------------------


def _get_kernel32() -> ctypes.WinDLL:
    """Lazily bind kernel32 with the correct argument types.

    Returns:
        Bound WinDLL handle for kernel32.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.LPVOID,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    kernel32.ReadFile.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.LPVOID,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
        ctypes.wintypes.LPVOID,
    ]
    kernel32.SetTapeParameters.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.LPVOID,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    return kernel32


class _TAPE_SET_MEDIA_PARAMETERS(ctypes.Structure):
    """ctypes Structure matching the Win32 TAPE_SET_MEDIA_PARAMETERS struct."""

    _fields_ = [("BlockSize", ctypes.wintypes.DWORD)]


# ---------------------------------------------------------------------------
# Real implementation
# ---------------------------------------------------------------------------


class TapeDeviceReader:
    """Direct tape device reader via kernel32 CreateFileW + ReadFile.

    Opens ``\\.\tape0`` (or another device) exclusively, configures it with
    SetTapeParameters(BlockSize=0), then streams one tape file at a time via
    ReadFile in a loop, accumulating a SHA-256 hash.

    This class is blocking by design — callers should run ``read_file_hashed``
    inside ``asyncio.to_thread``.
    """

    def __init__(
        self,
        device_name: str,
        *,
        open_retries: int = 3,
        open_retry_s: float = 2.0,
    ) -> None:
        """Initialize the reader without touching hardware.

        Args:
            device_name: Windows device path, e.g. ``\\\\.\\tape0``.
            open_retries: Extra CreateFileW attempts after a sharing
                violation (the drive poller holds the device ~2 s every
                poll cycle; the ITDT transport retries the same way).
            open_retry_s: Delay between open attempts, in seconds.
        """
        self._device_name = device_name
        self._open_retries = open_retries
        self._open_retry_s = open_retry_s
        self._handle: ctypes.wintypes.HANDLE | None = None
        self._kernel32: ctypes.WinDLL | None = None

    def _create_exclusive(self, kernel32: ctypes.WinDLL) -> ctypes.wintypes.HANDLE:
        """Create the device handle with sharing-violation retries.

        The tape driver is exclusive-open; a background poller or a
        just-finished ITDT command may hold the device for a moment, so
        a sharing violation is transient and retried. Any other error
        fails immediately.

        Args:
            kernel32: Bound kernel32 DLL.

        Returns:
            A valid device handle.

        Raises:
            DirectReadUnavailableError: If the device cannot be opened
                after all retries (or the failure is not transient).
        """
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        attempts = self._open_retries + 1
        for attempt in range(1, attempts + 1):
            handle = kernel32.CreateFileW(
                self._device_name,
                GENERIC_READ | GENERIC_WRITE,
                0,  # no sharing — exclusive access
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if handle != INVALID_HANDLE_VALUE and handle is not None:
                return handle  # type: ignore[no-any-return]
            err = ctypes.get_last_error()
            if err != ERROR_SHARING_VIOLATION or attempt == attempts:
                raise DirectReadUnavailableError(
                    f"CreateFileW failed for {self._device_name}: WinError {err}"
                )
            logger.warning(
                "direct_reader_open_retry",
                device_name=self._device_name,
                attempt=attempt,
                retry_in_s=self._open_retry_s,
            )
            time.sleep(self._open_retry_s)
        raise DirectReadUnavailableError(  # pragma: no cover
            f"CreateFileW failed for {self._device_name}: exhausted retries"
        )

    def open(self) -> None:
        """Open the tape device and configure block size to variable.

        Sharing violations (WinError 32) are retried with a short delay;
        see ``_create_exclusive``. Blocking — run inside
        ``asyncio.to_thread`` when a retry delay is acceptable.

        Raises:
            DirectReadUnavailableError: If the device cannot be opened,
                SetTapeParameters fails, or this is not a Windows platform.
        """
        if sys.platform != "win32":
            raise DirectReadUnavailableError("Direct tape reader requires win32")

        kernel32 = _get_kernel32()
        handle = self._create_exclusive(kernel32)

        # Set BlockSize=0 (variable block mode) — REQUIRED before ReadFile.
        # Win32 DWORD error code: 0 = NO_ERROR (success), nonzero fails.
        params = _TAPE_SET_MEDIA_PARAMETERS(0)
        rc = kernel32.SetTapeParameters(
            handle, TAPE_SET_MEDIA_PARAMS, ctypes.byref(params)
        )
        if rc != 0:
            err = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise DirectReadUnavailableError(
                f"SetTapeParameters failed for {self._device_name}: WinError {err}"
            )

        self._handle = handle
        self._kernel32 = kernel32
        logger.info("direct_reader_opened", device_name=self._device_name)

    def read_file_hashed(
        self, progress_cb: Callable[[int], None] | None = None
    ) -> tuple[int, str]:
        """Read the current tape file, hashing each chunk with SHA-256.

        The caller must have called ``open()`` first.  This method loops on
        ReadFile until end-of-file (got == 0) or a filemark is encountered
        (WinError 1101).  Partial transfers before a filemark are still hashed.

        Args:
            progress_cb: Optional callback invoked with cumulative byte count
                after each successful read.

        Returns:
            A ``(total_bytes, sha256_hex)`` tuple for the current tape file.

        Raises:
            TapeDeviceError: On any ReadFile failure that is not a filemark.
                The message includes the Win32 error number.
        """
        hasher = hashlib.sha256()
        total = 0

        while True:
            buf = ctypes.create_string_buffer(READ_BUFFER_BYTES)
            got = ctypes.wintypes.DWORD(0)
            ok = self._kernel32.ReadFile(  # type: ignore[union-attr]
                self._handle,
                buf,
                READ_BUFFER_BYTES,
                ctypes.byref(got),
                None,
            )
            err = ctypes.get_last_error()

            if ok:
                # Successful read — may be partial (got > 0).
                data = buf.raw[: got.value]
                hasher.update(data)
                total += got.value
                if progress_cb is not None:
                    progress_cb(total)
                if got.value == 0:
                    # Clean end-of-file.
                    break
            else:
                # ReadFile failed.
                if got.value > 0:
                    # Hash the partial bytes before deciding.
                    data = buf.raw[: got.value]
                    hasher.update(data)
                    total += got.value
                    if progress_cb is not None:
                        progress_cb(total)

                if err == ERROR_FILEMARK_DETECTED:
                    # Expected end of tape file — stop cleanly.
                    break
                else:
                    raise TapeDeviceError(f"ReadFile failed: WinError {err}")

        return (total, hasher.hexdigest())

    def close(self) -> None:
        """Close the device handle.  Safe to call multiple times."""
        if self._handle is None:
            return
        try:
            self._kernel32.CloseHandle(self._handle)  # type: ignore[union-attr]
        except Exception:
            logger.warning("direct_reader_close_failed", device_name=self._device_name)
        finally:
            self._handle = None


# ---------------------------------------------------------------------------
# Factory — returns a factory function on Windows, None otherwise
# ---------------------------------------------------------------------------


def windows_direct_reader_factory() -> Callable[[str], TapeDeviceReader] | None:
    """Return a factory for TapeDeviceReader on Windows, else None.

    Tests and non-Windows hosts must never touch hardware; the caller
    checks for None before constructing a real reader.

    Returns:
        A callable accepting a device_name string, or None on non-Windows.
    """
    if sys.platform != "win32":
        return None

    def factory(device_name: str) -> TapeDeviceReader:
        return TapeDeviceReader(device_name)

    return factory
