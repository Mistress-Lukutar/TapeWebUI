"""
File:   session_io.py
Brief:  Session header (t0) read/write helpers on top of the ITDT transport.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog

from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtTransport,
    ItDtTransportError,
)
from tapevaultui.services.tape_format import (
    SessionHeader,
    TapeFormatError,
    parse_header,
    serialize_header,
)

logger = structlog.get_logger(__name__)

# ITDT stdout fragments identifying a read attempt on a never-written
# (blank) tape: the drive raises blank-check sense (key 0x08), which ITDT
# surfaces as errno 1117 with zero records read.
_BLANK_READ_MARKERS = ("blank check", "blank tape")
# ITDT stdout fragments identifying a drive without a loaded medium.
_NO_MEDIA_MARKERS = ("medium not present", "no media", "not ready")


class SessionIoError(RuntimeError):
    """Raised when an ITDT session operation fails."""


class NoMediaError(SessionIoError):
    """Raised when the drive reports no loaded medium.

    Observed as a rewind failure with errno 2 / sense key 0x02 (not
    ready, ASC 0x04 0x02 "initializer command required") when the
    cartridge is ejected but loadable. Callers use it to enter the
    waiting_media state instead of failing the job.
    """


def new_session_id() -> str:
    """Generate a fresh session UUID.

    Returns:
        UUID4 string.
    """
    return str(uuid.uuid4())


async def write_session_header(
    transport: ItDtTransport | FakeItDtTransport,
    header: SessionHeader,
    work_dir: Path,
) -> None:
    """Write the t0 session header at the current tape position.

    The caller is responsible for positioning (seod for append, BOP after
    erase). Writes go to a temp file first, then one ``write -raw`` call.

    Args:
        transport: ITDT transport (real or fake).
        header: The header to write.
        work_dir: Directory for the temp header file.

    Raises:
        SessionIoError: If ITDT reports a failure.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    temp_file = work_dir / f"header_{header.session_id[:8]}.json"
    temp_file.write_bytes(serialize_header(header))

    result = await transport.run(["write", "-raw", "-s", str(temp_file)])
    if result.exit_code != 0:
        raise SessionIoError(
            f"ITDT write of session header failed: {result.stdout.strip()}"
        )
    logger.info("session_header_written", session_id=header.session_id)
    await write_filemarks(transport)


async def write_filemarks(
    transport: ItDtTransport | FakeItDtTransport,
    count: int = 1,
) -> None:
    """Write filemark(s) at the current tape position.

    Filemarks delimit tape files; without one after the session header
    (and each payload file) a ``read`` at BOP scans the whole data area
    until timeout, and ``fsf``-based positioning cannot find files.

    Args:
        transport: ITDT transport (real or fake).
        count: Number of filemarks to write (ITDT ``weof``).

    Raises:
        SessionIoError: If ITDT reports a failure.
    """
    result = await transport.run(["weof", str(count)])
    if result.exit_code != 0:
        raise SessionIoError(f"ITDT weof failed: {result.stdout.strip()}")
    logger.info("filemark_written", count=count)


def _looks_like_blank_read(stdout: str) -> bool:
    """Check whether ITDT output indicates a read on a blank tape.

    Args:
        stdout: ITDT output text.

    Returns:
        True if the output suggests blank-check media (nothing readable
        at BOP on a never-written cartridge).
    """
    lowered = stdout.lower()
    if any(marker in lowered for marker in _BLANK_READ_MARKERS):
        return True
    return "errno 1117" in lowered and "0 records" in lowered


def _looks_like_no_media(stdout: str) -> bool:
    """Check whether ITDT output indicates no loaded medium.

    Args:
        stdout: ITDT output text.

    Returns:
        True if the output suggests the drive has no (loaded) cartridge.
    """
    lowered = stdout.lower()
    if any(marker in lowered for marker in _NO_MEDIA_MARKERS):
        return True
    # errno 2 on a tape device is ITDT's mapping of SCSI not-ready with
    # ASC/ASCQ 04/02 (initializer command required): cartridge loadable
    # but not loaded. The message conjunction guards against other
    # errno-2 sources.
    return "errno 2 " in lowered and "cannot find the file specified" in lowered


async def read_session_header(
    transport: ItDtTransport | FakeItDtTransport,
    work_dir: Path,
) -> SessionHeader | None:
    """Read and parse tape file t0 at the current position.

    Reads the current tape file via ``read -d``, parses the header. A
    foreign/unreadable tape yields None; a header of a newer format
    raises TapeFormatError. The tape is rewound before and after.

    Args:
        transport: ITDT transport (real or fake).
        work_dir: Directory for the temp download file.

    Returns:
        Parsed SessionHeader, or None for a foreign/blank tape.

    Raises:
        SessionIoError: If ITDT read/rewind commands fail.
        NoMediaError: If the drive has no loaded medium.
        TapeFormatError: If the tape holds an incompatible future header.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    temp_file = work_dir / f"t0_{uuid.uuid4().hex[:8]}.bin"

    await rewind_tape(transport)
    result = await transport.run(["read", "-d", str(temp_file)])
    if result.exit_code != 0:
        stdout = result.stdout.strip()
        await rewind_tape(transport)
        if temp_file.exists():
            temp_file.unlink()
        lowered = stdout.lower()
        if (
            "filemark" in lowered
            or "end of data" in lowered
            or _looks_like_blank_read(stdout)
        ):
            return None  # blank / foreign tape: nothing readable at BOP
        raise SessionIoError(f"ITDT read of session header failed: {stdout}")

    try:
        raw = temp_file.read_bytes()
    finally:
        if temp_file.exists():
            temp_file.unlink()

    await rewind_tape(transport)
    header = parse_header(raw)
    if header is None:
        logger.info("foreign_tape_detected")
    return header


async def identify_tape(
    transport: ItDtTransport | FakeItDtTransport,
    work_dir: Path,
) -> SessionHeader | None:
    """Identify the inserted tape by reading its header at BOP.

    Convenience wrapper: rewind to BOP first, then read t0.

    Args:
        transport: ITDT transport (real or fake).
        work_dir: Directory for the temp download file.

    Returns:
        Parsed SessionHeader of the first session, or None for a
        foreign/blank tape.

    Raises:
        SessionIoError: If ITDT commands fail.
        TapeFormatError: If the tape holds an incompatible future header.
    """
    try:
        return await read_session_header(transport, work_dir)
    except TapeFormatError:
        raise
    except ItDtTransportError as exc:
        raise SessionIoError(str(exc)) from exc


async def rewind_tape(transport: ItDtTransport | FakeItDtTransport) -> None:
    """Rewind the tape to BOP.

    Args:
        transport: ITDT transport (real or fake).

    Raises:
        SessionIoError: If the rewind command fails.
        NoMediaError: If the drive reports no loaded medium.
    """
    result = await transport.run(["rewind"])
    if result.exit_code != 0:
        stdout = result.stdout.strip()
        if _looks_like_no_media(stdout):
            raise NoMediaError(f"ITDT rewind failed, no loaded medium: {stdout}")
        raise SessionIoError(f"ITDT rewind failed: {stdout}")


async def erase_tape_short(
    transport: ItDtTransport | FakeItDtTransport,
) -> None:
    """Perform a short erase from the beginning of partition.

    Args:
        transport: ITDT transport (real or fake).

    Raises:
        SessionIoError: If the rewind or erase command fails.
    """
    await rewind_tape(transport)
    result = await transport.run(["erase", "-short"])
    if result.exit_code != 0:
        raise SessionIoError(f"ITDT short erase failed: {result.stdout.strip()}")
    logger.info("tape_short_erase_done")


async def space_to_end_of_data(
    transport: ItDtTransport | FakeItDtTransport,
) -> None:
    """Position the tape at end-of-data (append point).

    Args:
        transport: ITDT transport (real or fake).

    Raises:
        SessionIoError: If the seod command fails.
    """
    result = await transport.run(["seod"])
    if result.exit_code != 0:
        raise SessionIoError(f"ITDT seod failed: {result.stdout.strip()}")


async def space_forward_files(
    transport: ItDtTransport | FakeItDtTransport,
    count: int,
) -> None:
    """Space forward over tape files.

    Args:
        transport: ITDT transport (real or fake).
        count: Number of tape files to skip.

    Raises:
        SessionIoError: If the fsf command fails.
    """
    result = await transport.run(["fsf", str(count)])
    if result.exit_code != 0:
        raise SessionIoError(f"ITDT fsf {count} failed: {result.stdout.strip()}")
