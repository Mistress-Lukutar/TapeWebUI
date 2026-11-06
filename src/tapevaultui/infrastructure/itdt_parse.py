"""
File:   itdt_parse.py
Brief:  Pure parsers for ITDT-SE CLI output (vpd, qrypos, getparms, inqj, devstat).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ITDT prints "label ..... value" lines with dot leaders.
_KEY_VALUE_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z0-9 _/-]*?)\s*\.{2,}\s*(?P<value>.*?)\s*$"
)

_HEX_BLOCK_ID_RE = re.compile(
    r"^Current Block ID\s*\.{2,}\s*(\d+)\s+0x[0-9A-Fa-f]+\s*$"
)

_YES_NO = {"yes": True, "no": False}


class ItDtParseError(ValueError):
    """Raised when ITDT output does not match the expected format."""


@dataclass(frozen=True)
class VpdInfo:
    """Drive identity from the `vpd` command."""

    manufacturer: str
    product_id: str
    revision: str


@dataclass(frozen=True)
class TapePosition:
    """Tape position as reported by one section of `qrypos` output."""

    partition: int
    block_id: int
    at_beginning_of_partition: bool
    at_leading_end_of_partition: bool


@dataclass(frozen=True)
class QryPosInfo:
    """Full `qrypos` result: logical and physical positions."""

    logical: TapePosition
    physical: TapePosition


@dataclass(frozen=True)
class DriveParms:
    """Drive/medium parameters from the `getparms` command."""

    compression: bool
    block_size: int
    buffered_mode: bool


@dataclass(frozen=True)
class DeviceInfo:
    """Device information from the `devinfo` command."""

    devtype: str
    tapetype: str
    block_size: int


@dataclass(frozen=True)
class InquiryInfo:
    """Standard SCSI inquiry data from the `inqj` (JSON) command."""

    vendor: str
    product: str
    revision: str


def _parse_kv_lines(section: str) -> dict[str, str]:
    """Extract a label -> value dict from dot-leader lines of a section."""
    result: dict[str, str] = {}
    for line in section.splitlines():
        match = _KEY_VALUE_RE.match(line.strip())
        if match:
            result[match.group("key").strip().lower()] = match.group("value").strip()
    return result


def _yes_no(value: str, label: str) -> bool:
    """Convert a Yes/No ITDT value to bool."""
    flag = _YES_NO.get(value.strip().lower())
    if flag is None:
        raise ItDtParseError(f"Unexpected Yes/No value for {label!r}: {value!r}")
    return flag


def _parse_position_section(section: str, label: str) -> TapePosition:
    """Parse one "Logical/Physical Tape position" section of qrypos output."""
    kv = _parse_kv_lines(section)
    try:
        partition = int(kv["partition number"])
        at_bop = _yes_no(kv["beginning of partition"], "Beginning of Partition")
        at_leop = _yes_no(kv["leading end of partition"], "Leading End of Partition")
        block_id = _parse_block_id(section)
    except KeyError as exc:
        raise ItDtParseError(f"Missing field in {label} section: {exc}") from exc
    return TapePosition(
        partition=partition,
        block_id=block_id,
        at_beginning_of_partition=at_bop,
        at_leading_end_of_partition=at_leop,
    )


def _parse_block_id(section: str) -> int:
    """Parse the "Current Block ID ... <decimal> 0x<hex>" line."""
    for line in section.splitlines():
        match = _HEX_BLOCK_ID_RE.match(line.strip())
        if match:
            return int(match.group(1))
    raise ItDtParseError("Current Block ID line not found")


def parse_vpd(text: str) -> VpdInfo:
    """Parse `vpd` output into drive identity.

    Args:
        text: Full stdout of the ITDT `vpd` command.

    Returns:
        Parsed VpdInfo.

    Raises:
        ItDtParseError: If expected VPD fields are missing.
    """
    kv = _parse_kv_lines(text)
    try:
        return VpdInfo(
            manufacturer=kv["manufacturer"],
            product_id=kv["product identification"],
            revision=kv["revision level"],
        )
    except KeyError as exc:
        raise ItDtParseError(f"Missing VPD field: {exc}") from exc


def parse_qrypos(text: str) -> QryPosInfo:
    """Parse `qrypos` output into logical and physical positions.

    Args:
        text: Full stdout of the ITDT `qrypos` command.

    Returns:
        Parsed QryPosInfo.

    Raises:
        ItDtParseError: If either position section cannot be parsed.
    """
    logical_match = re.search(
        r"Logical Tape position:(.*?)(?=Physical Tape position:|$)", text, re.DOTALL
    )
    physical_match = re.search(r"Physical Tape position:(.*?)$", text, re.DOTALL)
    if not logical_match or not physical_match:
        raise ItDtParseError("qrypos output misses Logical/Physical position sections")
    return QryPosInfo(
        logical=_parse_position_section(logical_match.group(1), "logical"),
        physical=_parse_position_section(physical_match.group(1), "physical"),
    )


def parse_getparms(text: str) -> DriveParms:
    """Parse `getparms` output into drive parameters.

    Args:
        text: Full stdout of the ITDT `getparms` command.

    Returns:
        Parsed DriveParms.

    Raises:
        ItDtParseError: If expected parameter fields are missing.
    """
    kv = _parse_kv_lines(text)
    try:
        return DriveParms(
            compression=_yes_no(kv["compression"], "Compression"),
            block_size=int(kv["block size"]),
            buffered_mode=_yes_no(kv["buffered mode"], "Buffered Mode"),
        )
    except KeyError as exc:
        raise ItDtParseError(f"Missing getparms field: {exc}") from exc


def parse_devinfo(text: str) -> DeviceInfo:
    """Parse `devinfo` output into device information.

    `devinfo` uses a "key = value" format (not dot leaders).

    Args:
        text: Full stdout of the ITDT `devinfo` command.

    Returns:
        Parsed DeviceInfo.

    Raises:
        ItDtParseError: If expected device fields are missing.
    """
    eq_re = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9 _/-]*?)\s*=\s*(?P<value>.*?)\s*$")
    kv: dict[str, str] = {}
    for line in text.splitlines():
        match = eq_re.match(line.strip())
        if match:
            kv[match.group("key").strip().lower()] = match.group("value").strip()
    try:
        return DeviceInfo(
            devtype=kv["devtype"],
            tapetype=kv["tapetype"],
            block_size=int(kv["block size"]),
        )
    except KeyError as exc:
        raise ItDtParseError(f"Missing devinfo field: {exc}") from exc


def parse_inquiry_json(text: str) -> InquiryInfo:
    """Parse `inqj` (JSON inquiry) output.

    ITDT prints the JSON document to stdout; leading/trailing non-JSON
    lines are tolerated by scanning for the first '{'.

    Args:
        text: Full stdout of the ITDT `inqj` command.

    Returns:
        Parsed InquiryInfo.

    Raises:
        ItDtParseError: If no valid JSON document with identity fields found.
    """
    start = text.find("{")
    if start == -1:
        raise ItDtParseError("No JSON document found in inqj output")
    try:
        document = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise ItDtParseError(f"Invalid JSON in inqj output: {exc}") from exc

    try:
        data = document["data"]
        return InquiryInfo(
            vendor=str(data["vendorIdentification"]).strip(),
            product=str(data["productIdentification"]).strip(),
            revision=str(data["productRevisionLevel"]).strip(),
        )
    except (KeyError, TypeError) as exc:
        raise ItDtParseError(f"Missing inquiry identity fields: {exc}") from exc


def parse_device_statistics(text: str) -> dict[str, str]:
    """Parse `devicestatistics` output into a normalized label -> value map.

    The command prints a two-column table ("Label  <spaces> value
    Label  <spaces> value") after the "Device Statistics:" header. Keys
    are normalized to lowercase snake_case; values are kept as strings.

    Args:
        text: Full stdout of the ITDT ``devicestatistics`` command.

    Returns:
        Dict of statistic name -> raw string value.

    Raises:
        ItDtParseError: If no Device Statistics table is found.
    """
    stats: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        if not in_section:
            if "Device Statistics:" in line:
                in_section = True
            continue
        segments = [
            seg.strip() for seg in re.split(r"\s{2,}", line.strip()) if seg.strip()
        ]
        if not segments:
            continue
        if len(segments) % 2 != 0:
            break  # footer line (e.g. "Exit with code: 0")
        for i in range(0, len(segments), 2):
            key = re.sub(r"[^a-z0-9]+", "_", segments[i].lower()).strip("_")
            if key:
                stats[key] = segments[i + 1]
    if not stats:
        raise ItDtParseError("Device Statistics table not found in output")
    return stats


_SENSE_ROW_RE = re.compile(
    r"^\s*(?P<offset>[0-9A-Fa-f]{4})\s+-\s+(?P<hex>.+?)\s+\[", re.MULTILINE
)


def _sense_bytes_from_dump(text: str) -> bytes:
    """Extract bytes from an ITDT sense-data hex dump.

    The dump looks like::

        0000 - 7000 0200 0000 0058 0000 0000 3A00 3100  [p......X....:.1.]

    Only rows whose offset is a 4-digit hex number are considered; the
    trailing ASCII-art column (``[...]``) is ignored.

    Args:
        text: ITDT output containing (or not containing) a sense dump.

    Returns:
        The concatenated dump bytes (empty if no dump rows found).
    """
    data = bytearray()
    for match in _SENSE_ROW_RE.finditer(text):
        for token in match.group("hex").split():
            if re.fullmatch(r"[0-9A-Fa-f]{4}", token):
                data.extend(bytes.fromhex(token))
    return bytes(data)


def is_medium_not_present(text: str) -> bool:
    """Check whether ITDT output reports an empty drive (no cartridge).

    Detects either an explicit "medium not present" report or a SCSI
    fixed-format sense dump whose ASC/ASCQ signals an absent medium:
    ``3A/xx`` (MEDIUM NOT PRESENT), or ``04/00``-style NOT READY /
    initializing states — on this drive (ULTRIUM-TD3, fw 93G0) an empty
    drive alternates between ``3A/00`` and ``04/02`` for ``qrypos``.

    Args:
        text: ITDT command output (e.g. a failed ``qrypos``).

    Returns:
        True if the output indicates that no cartridge is loaded.
    """
    if "medium not present" in text.lower():
        return True
    sense = _sense_bytes_from_dump(text)
    if len(sense) < 14 or sense[0] & 0x7F not in (0x70, 0x71):
        return False
    asc, ascq = sense[12], sense[13]
    if asc == 0x3A:  # MEDIUM NOT PRESENT (any ASCQ)
        return True
    # NOT READY: initializing required / cause not reportable — observed
    # on the empty drive right after unloading a cartridge.
    return sense[2] == 0x02 and asc == 0x04 and ascq in (0x00, 0x02)


def is_medium_removal_prevented(text: str) -> bool:
    """Check whether ITDT output reports a locked cartridge (sense 53/02).

    Windows (RSM / ltotape.sys) latches PREVENT MEDIUM REMOVAL on freshly
    inserted media, so ``unload``/eject fails with ILLEGAL REQUEST sense
    ASC/ASCQ ``53/02`` (MEDIUM REMOVAL PREVENTED) until an ALLOW MEDIUM
    REMOVAL command is issued.

    Args:
        text: ITDT command output (e.g. a failed ``unload``).

    Returns:
        True if the output indicates medium removal is prevented.
    """
    if "medium removal prevented" in text.lower():
        return True
    sense = _sense_bytes_from_dump(text)
    return (
        len(sense) >= 14
        and sense[0] & 0x7F in (0x70, 0x71)
        and sense[2] == 0x05  # ILLEGAL REQUEST
        and sense[12] == 0x53
        and sense[13] == 0x02
    )


def is_open_failure(text: str) -> bool:
    """Check whether ITDT output indicates the device could not be opened.

    This happens transiently on this machine (see .agents/AGENTS.md); the
    transport retries such failures.

    Args:
        text: Combined ITDT output.

    Returns:
        True if output contains an open-failure report.
    """
    return "Open failed:" in text
