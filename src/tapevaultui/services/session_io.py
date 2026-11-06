"""
File:   session_io.py
Brief:  Session header (t0) read/write helpers on top of the ITDT transport.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.2.0
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


class SessionIoError(RuntimeError):
    """Raised when an ITDT session operation fails."""


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
        TapeFormatError: If the tape holds an incompatible future header.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    temp_file = work_dir / f"t0_{uuid.uuid4().hex[:8]}.bin"

    await _rewind(transport)
    result = await transport.run(["read", "-d", str(temp_file)])
    if result.exit_code != 0:
        stdout = result.stdout.strip()
        await _rewind(transport)
        if temp_file.exists():
            temp_file.unlink()
        if "filemark" in stdout.lower() or "end of data" in stdout.lower():
            return None  # blank / foreign tape: nothing readable at BOP
        raise SessionIoError(f"ITDT read of session header failed: {stdout}")

    try:
        raw = temp_file.read_bytes()
    finally:
        if temp_file.exists():
            temp_file.unlink()

    await _rewind(transport)
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


async def _rewind(transport: ItDtTransport | FakeItDtTransport) -> None:
    """Rewind the tape to BOP.

    Args:
        transport: ITDT transport (real or fake).

    Raises:
        SessionIoError: If the rewind command fails.
    """
    result = await transport.run(["rewind"])
    if result.exit_code != 0:
        raise SessionIoError(f"ITDT rewind failed: {result.stdout.strip()}")


async def erase_tape_short(
    transport: ItDtTransport | FakeItDtTransport,
) -> None:
    """Perform a short erase from the beginning of partition.

    Args:
        transport: ITDT transport (real or fake).

    Raises:
        SessionIoError: If the rewind or erase command fails.
    """
    await _rewind(transport)
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
