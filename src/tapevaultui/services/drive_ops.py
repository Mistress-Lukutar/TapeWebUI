"""
File:   drive_ops.py
Brief:  Manual drive/media operations (ITDT-SE service commands) for the Service page.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

from typing import Final

import structlog

from tapevaultui.infrastructure.itdt_parse import is_medium_removal_prevented
from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtTransport,
)
from tapevaultui.services.session_io import (
    erase_tape_short,
    rewind_tape,
    space_to_end_of_data,
)

logger = structlog.get_logger(__name__)

# Simple (single-command) drive operations mirrored from the ITDT-SE
# command set; op name -> ITDT subcommand arguments.
SIMPLE_OPS: Final[dict[str, tuple[str, ...]]] = {
    "tur": ("tur",),  # test unit ready
    "load": ("load",),  # load the cartridge into the drive
    "unload": ("unload",),  # unload and eject the cartridge
    "lock": ("prevent",),  # prevent medium removal (lock door)
    "unlock": ("allow",),  # allow medium removal (unlock door)
    "reserve": ("reserve",),  # SCSI reserve unit
    "release": ("release",),  # SCSI release unit
}

# Composite ops implemented through session_io helpers.
COMPOSITE_OPS: Final[frozenset[str]] = frozenset(
    {"rewind", "seod", "erase_short", "erase_long"}
)

ALL_OPS: Final[frozenset[str]] = frozenset(SIMPLE_OPS) | COMPOSITE_OPS


class DriveOpError(RuntimeError):
    """Raised when a manual drive operation fails."""


async def run_drive_op(
    transport: ItDtTransport | FakeItDtTransport,
    op: str,
) -> str:
    """Execute one manual drive operation and return the ITDT output.

    Args:
        transport: ITDT transport (real or fake).
        op: Operation name (see ``ALL_OPS``).

    Returns:
        Raw ITDT stdout (or a human summary for composite ops).

    Raises:
        DriveOpError: If the operation is unknown or ITDT reports a
            failure.
    """
    if op == "rewind":
        await rewind_tape(transport)
        return "Rewound to beginning of partition (BOP)."
    if op == "seod":
        await space_to_end_of_data(transport)
        return "Spaced to end of data (append point)."
    if op == "erase_short":
        await erase_tape_short(transport)
        return "Short erase from BOP completed."

    if op == "erase_long":
        await rewind_tape(transport)
        result = await transport.run(["erase", "-long"])
        if result.exit_code != 0:
            raise DriveOpError(f"ITDT long erase failed: {result.stdout.strip()}")
        logger.info("tape_long_erase_done")
        return "Long erase from BOP completed."

    args = SIMPLE_OPS.get(op)
    if args is None:
        raise DriveOpError(f"Unknown drive operation: {op}")

    result = await transport.run(list(args))
    if (
        op == "unload"
        and result.exit_code != 0
        and is_medium_removal_prevented(result.stdout)
    ):
        # Windows (RSM/ltotape) latches PREVENT MEDIUM REMOVAL on freshly
        # inserted media; eject is rejected with sense 53/02 until removal
        # is allowed again. Unlock and retry once.
        logger.info("drive_op_medium_locked_retry", op=op)
        await transport.run(["allow"])
        result = await transport.run(["unload"])
    if result.exit_code != 0:
        if op == "unload" and is_medium_removal_prevented(result.stdout):
            raise DriveOpError(
                "The cartridge stays locked by the system (medium removal "
                "prevented). Run the 'unlock' operation, then unload again."
            )
        raise DriveOpError(f"ITDT {args[0]} failed: {result.stdout.strip()}")
    logger.info("drive_op_done", op=op)
    return result.stdout.strip() or f"ITDT {args[0]} completed."
