"""
File:   tape_sync.py
Brief:  Catalog-to-tape synchronization: verify, erase attribution, deep scan.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport, ItDtTransport
from tapevaultui.services.catalog import SESSION_HEADER_PATH, CatalogRepository
from tapevaultui.services.session_io import (
    NoMediaError,
    SessionIoError,
    identify_tape,
    read_header_at_current_position,
    rewind_tape,
)
from tapevaultui.services.tape_format import SessionHeader, TapeFormatError

logger = structlog.get_logger(__name__)

# Setting-table key holding the unattributed-erase notice payload.
STALE_NOTICE_KEY = "catalog_stale_notice"

# Guard against an endless deep-scan loop on a malformed tape.
MAX_SCAN_SESSIONS = 10000

# Verify result statuses.
VERIFY_MATCH = "match"
VERIFY_BLANK = "blank"
VERIFY_FOREIGN = "foreign"
VERIFY_NO_MEDIUM = "no_medium"
VERIFY_ERROR = "error"

# Deep-scan diff kinds.
DIFF_MATCH = "match"
DIFF_CATALOG_ONLY = "catalog_only"
DIFF_TAPE_ONLY = "tape_only"
DIFF_MISMATCH = "mismatch"


class TapeSyncError(RuntimeError):
    """Raised when a tape sync operation fails on the ITDT level."""


@dataclass
class VerifyResult:
    """Outcome of a quick (header) verification of the loaded tape."""

    status: str
    tape_id: int | None = None
    tape_uuid: str | None = None
    tape_label: str = ""
    detail: str = ""


@dataclass
class OnTapeFile:
    """One source file recorded in an on-tape session header."""

    path: str
    size: int


@dataclass
class OnTapeSession:
    """One write session discovered on the physical tape."""

    file_base: int  # tape-absolute position of the t0 header (fsf from BOP)
    session_id: str
    job_name: str
    started_at: str
    files: list[OnTapeFile] = field(default_factory=list)


@dataclass
class SessionDiff:
    """One compared session of a deep-scan report."""

    kind: str
    file_base: int
    session_id: str = ""
    job_name: str = ""
    catalog_session_id: int | None = None
    catalog_status: str = ""
    detail: str = ""


@dataclass
class DeepScanReport:
    """Full comparison of on-tape contents against the catalog."""

    tape_id: int | None = None
    tape_uuid: str | None = None
    tape_label: str = ""
    on_tape_count: int = 0
    catalog_count: int = 0
    diffs: list[SessionDiff] = field(default_factory=list)


async def verify_loaded_tape(
    transport: ItDtTransport | FakeItDtTransport,
    work_dir: Path,
    catalog: CatalogRepository,
) -> VerifyResult:
    """Identify the loaded cartridge and reconcile it with the catalog.

    Reads the t0 header at BOP; a match stamps the catalog tape as
    verified.

    Args:
        transport: ITDT transport (real or fake).
        work_dir: Directory for temp download files.
        catalog: Catalog repository.

    Returns:
        The verification result (match | blank | foreign | no_medium |
        error).
    """
    try:
        header = await identify_tape(transport, work_dir)
    except NoMediaError as exc:
        return VerifyResult(status=VERIFY_NO_MEDIUM, detail=str(exc))
    except (SessionIoError, TapeFormatError) as exc:
        return VerifyResult(status=VERIFY_ERROR, detail=str(exc))
    if header is None:
        return VerifyResult(
            status=VERIFY_BLANK,
            detail=(
                "No readable session header at BOP: the loaded cartridge "
                "is blank or erased."
            ),
        )
    tape = await asyncio.to_thread(catalog.get_tape_by_uuid, header.tape_id)
    if tape is None:
        return VerifyResult(
            status=VERIFY_FOREIGN,
            tape_uuid=header.tape_id,
            tape_label=header.tape_label,
            detail=(
                f"Tape header UUID {header.tape_id} is not registered in the catalog."
            ),
        )
    await asyncio.to_thread(catalog.set_tape_verified, tape.id)
    logger.info("tape_verified", tape_id=tape.id, uuid=tape.uuid)
    return VerifyResult(
        status=VERIFY_MATCH,
        tape_id=tape.id,
        tape_uuid=tape.uuid,
        tape_label=tape.label,
        detail="Loaded cartridge matches the catalog entry.",
    )


async def attribute_erase(
    transport: ItDtTransport | FakeItDtTransport,
    work_dir: Path,
    catalog: CatalogRepository,
) -> int | None:
    """Identify the loaded tape before an erase, for catalog attribution.

    Must be called BEFORE the erase command: the header is destroyed by
    the erase, so afterwards the cartridge can no longer be attributed.

    Args:
        transport: ITDT transport (real or fake).
        work_dir: Directory for temp download files.
        catalog: Catalog repository.

    Returns:
        The catalog tape id of the loaded cartridge, or None when the
        tape is blank, unreadable, foreign or absent.
    """
    try:
        header = await identify_tape(transport, work_dir)
    except (NoMediaError, SessionIoError, TapeFormatError) as exc:
        logger.info("erase_attribution_failed", error=str(exc))
        return None
    if header is None:
        return None
    tape = await asyncio.to_thread(catalog.get_tape_by_uuid, header.tape_id)
    if tape is None:
        return None
    logger.info("erase_attributed", tape_id=tape.id, uuid=tape.uuid)
    return tape.id


async def scan_tape_contents(
    transport: ItDtTransport | FakeItDtTransport,
    work_dir: Path,
) -> list[OnTapeSession]:
    """Walk the whole tape and collect its sessions from the t0 headers.

    Uses absolute positioning (rewind + fsf) before every header read,
    matching the restore engine's convention: relative spacing drifts
    whenever a read leaves the head in an unexpected place.

    Args:
        transport: ITDT transport (real or fake).
        work_dir: Directory for temp download files.

    Returns:
        Sessions in tape order.

    Raises:
        TapeSyncError: On an ITDT failure or a malformed (endless) tape.
    """
    sessions: list[OnTapeSession] = []
    await _walk_from_bop(transport, work_dir, sessions)
    if len(sessions) >= MAX_SCAN_SESSIONS:
        raise TapeSyncError(
            f"Tape scan exceeded {MAX_SCAN_SESSIONS} sessions; aborting."
        )
    logger.info("tape_scanned", sessions=len(sessions))
    return sessions


async def _walk_from_bop(
    transport: ItDtTransport | FakeItDtTransport,
    work_dir: Path,
    sessions: list[OnTapeSession],
) -> None:
    """Walk the tape from BOP, appending discovered sessions.

    Args:
        transport: ITDT transport (real or fake).
        work_dir: Directory for temp download files.
        sessions: Output accumulator.

    Raises:
        TapeSyncError: On an ITDT failure.
    """
    base = 0
    while len(sessions) < MAX_SCAN_SESSIONS:
        try:
            header = await _read_header_at(transport, work_dir, base)
        except (SessionIoError, TapeFormatError) as exc:
            raise TapeSyncError(str(exc)) from exc
        if header is None:
            break  # end of data: last session walked past EOD
        sessions.append(_session_from_header(base, header))
        base += 1 + len(header.files)


def compare_with_catalog(
    on_tape: list[OnTapeSession],
    catalog: CatalogRepository,
    tape_id: int,
) -> DeepScanReport:
    """Compare scanned on-tape sessions against the catalog entries.

    Matching is by ``file_base`` (the tape-absolute header position).
    Erased catalog sessions are excluded: their data is known to be
    physically gone. Catalog sessions missing on the tape are reported
    as ``catalog_only`` (candidates for erase-marking).

    Args:
        on_tape: Sessions collected by :func:`scan_tape_contents`.
        catalog: Catalog repository.
        tape_id: Catalog tape primary key.

    Returns:
        The deep-scan report.
    """
    tape = catalog.get_tape(tape_id)
    report = DeepScanReport(
        tape_id=tape_id,
        tape_uuid=tape.uuid if tape else None,
        tape_label=tape.label if tape else "",
        on_tape_count=len(on_tape),
    )

    catalog_sessions = {
        session.file_base: session
        for session in catalog.list_sessions(tape_id)
        if session.status != "erased"
    }
    report.catalog_count = len(catalog_sessions)

    for tape_session in on_tape:
        cat_session = catalog_sessions.pop(tape_session.file_base, None)
        if cat_session is None:
            report.diffs.append(
                SessionDiff(
                    kind=DIFF_TAPE_ONLY,
                    file_base=tape_session.file_base,
                    session_id=tape_session.session_id,
                    job_name=tape_session.job_name,
                    detail="Session exists on tape but has no catalog entry.",
                )
            )
            continue
        tape_files = {(f.path, f.size) for f in tape_session.files}
        cat_files = {
            (entry.path, entry.size)
            for entry in catalog.list_files(cat_session.id)
            if entry.path != SESSION_HEADER_PATH
        }
        if tape_files == cat_files:
            report.diffs.append(
                SessionDiff(
                    kind=DIFF_MATCH,
                    file_base=tape_session.file_base,
                    session_id=tape_session.session_id,
                    job_name=tape_session.job_name,
                    catalog_session_id=cat_session.id,
                    catalog_status=cat_session.status,
                )
            )
        else:
            only_tape = tape_files - cat_files
            only_cat = cat_files - tape_files
            report.diffs.append(
                SessionDiff(
                    kind=DIFF_MISMATCH,
                    file_base=tape_session.file_base,
                    session_id=tape_session.session_id,
                    job_name=tape_session.job_name,
                    catalog_session_id=cat_session.id,
                    catalog_status=cat_session.status,
                    detail=(
                        f"Files only on tape: {len(only_tape)}; "
                        f"files only in catalog: {len(only_cat)}."
                    ),
                )
            )

    for cat_session in sorted(catalog_sessions.values(), key=lambda s: s.file_base):
        report.diffs.append(
            SessionDiff(
                kind=DIFF_CATALOG_ONLY,
                file_base=cat_session.file_base,
                job_name=cat_session.job_name,
                catalog_session_id=cat_session.id,
                catalog_status=cat_session.status,
                detail=(
                    "Session is in the catalog but not on the tape "
                    "(erased or overwritten); safe to erase-mark."
                ),
            )
        )
    return report


def apply_erase_attribution(catalog: CatalogRepository, tape_id: int | None) -> str:
    """Sync the catalog after a successful erase, based on attribution.

    Args:
        catalog: Catalog repository.
        tape_id: Catalog tape id attributed to the erased cartridge
            (from :func:`attribute_erase`), or None when the tape could
            not be identified.

    Returns:
        A human-readable message for the operation log / response.
    """
    if tape_id is not None:
        tape = catalog.get_tape(tape_id)
        if tape is not None:
            catalog.mark_tape_erased(tape_id)
            name = tape.label or tape.uuid
            return f"Catalog synced: all sessions of tape '{name}' were marked erased."
    set_stale_notice(catalog, "unattributed_erase")
    return (
        "Erased tape could not be attributed to a catalog entry "
        "(blank/foreign/unreadable header); the catalog may be stale - "
        "use Verify tape on the Tapes page."
    )


def set_stale_notice(catalog: CatalogRepository, reason: str) -> None:
    """Record that a catalog divergence may exist and cannot be attributed.

    Args:
        catalog: Catalog repository.
        reason: Machine-readable reason (e.g. ``unattributed_erase``).
    """
    catalog.set_setting(
        STALE_NOTICE_KEY,
        {"active": True, "reason": reason, "at": datetime.now(UTC).isoformat()},
    )
    logger.info("catalog_stale_notice_set", reason=reason)


def get_stale_notice(catalog: CatalogRepository) -> dict[str, object] | None:
    """Return the active stale notice, if any.

    Args:
        catalog: Catalog repository.

    Returns:
        The notice payload, or None when inactive or absent.
    """
    payload = catalog.get_setting(STALE_NOTICE_KEY)
    if payload and payload.get("active"):
        return payload
    return None


def clear_stale_notice(catalog: CatalogRepository) -> None:
    """Dismiss the stale notice.

    Args:
        catalog: Catalog repository.
    """
    catalog.set_setting(STALE_NOTICE_KEY, {"active": False})
    logger.info("catalog_stale_notice_cleared")


async def _read_header_at(
    transport: ItDtTransport | FakeItDtTransport,
    work_dir: Path,
    file_base: int,
) -> SessionHeader | None:
    """Read a session header at an absolute tape position.

    Rewinds, spaces to ``file_base`` and reads the current file.

    Args:
        transport: ITDT transport (real or fake).
        work_dir: Directory for temp download files.
        file_base: Tape-absolute position (fsf steps from BOP).

    Returns:
        Parsed header, or None when nothing readable is there (EOD).
    """
    await rewind_tape(transport)
    if file_base > 0:
        result = await transport.run(["fsf", str(file_base)])
        if result.exit_code != 0:
            stdout = result.stdout.strip()
            if "end of data" in stdout.lower() or "filemark" in stdout.lower():
                return None
            raise SessionIoError(f"ITDT fsf {file_base} failed: {stdout}")
    return await read_header_at_current_position(transport, work_dir)


def _session_from_header(base: int, header: SessionHeader) -> OnTapeSession:
    """Convert a parsed on-tape header into a scan record.

    Args:
        base: Tape-absolute position of the header.
        header: Parsed session header.

    Returns:
        The OnTapeSession record.
    """
    return OnTapeSession(
        file_base=base,
        session_id=header.session_id,
        job_name=header.job_name,
        started_at=header.started_at,
        files=[OnTapeFile(path=f.path, size=f.size) for f in header.files],
    )
