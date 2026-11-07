"""
File:   sync.py
Brief:  Catalog sync-state API (stale-catalog notice).
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from fastapi import APIRouter

from tapevaultui.dependencies import CatalogDep
from tapevaultui.services.tape_sync import clear_stale_notice, get_stale_notice

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/stale-notice")
async def read_stale_notice(catalog: CatalogDep) -> dict[str, object] | None:
    """Return the active stale-catalog notice, if any.

    Args:
        catalog: Catalog repository.

    Returns:
        The notice payload (reason, timestamp) or None.
    """
    return get_stale_notice(catalog)


@router.post("/stale-notice/dismiss")
async def dismiss_stale_notice(catalog: CatalogDep) -> dict[str, bool]:
    """Dismiss the stale-catalog notice.

    Args:
        catalog: Catalog repository.

    Returns:
        Confirmation payload.
    """
    clear_stale_notice(catalog)
    return {"dismissed": True}
