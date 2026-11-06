"""
File:   catalog.py
Brief:  Catalog explorer API: directory listings and subtree selections.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from tapevaultui.dependencies import CatalogDep
from tapevaultui.services.catalog_browser import (
    CatalogListing,
    SelectionSummary,
    session_listing,
    session_selection,
    tape_listing,
    tape_selection,
)

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/list", response_model=CatalogListing)
async def list_directory(
    catalog: CatalogDep,
    session_id: int | None = Query(default=None),
    tape_id: int | None = Query(default=None),
    dir_path: str = Query(default="", alias="dir"),
) -> CatalogListing:
    """List the children of a catalog directory.

    Exactly one of ``session_id`` (single-session mode) or ``tape_id``
    (merged mode across the tape's live sessions) must be given.

    Args:
        catalog: Catalog repository.
        session_id: BackupSession primary key (session mode).
        tape_id: Tape primary key (merged mode).
        dir: Directory prefix ("" lists the roots).

    Returns:
        The directory listing.

    Raises:
        HTTPException: 422 when the scope is ambiguous or missing, 404
            when the session/tape does not exist.
    """
    _validate_scope(session_id, tape_id)
    if session_id is not None:
        if catalog.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session_listing(catalog, session_id, dir_path)
    assert tape_id is not None
    if catalog.get_tape(tape_id) is None:
        raise HTTPException(status_code=404, detail="Tape not found")
    return tape_listing(catalog, tape_id, dir_path)


@router.get("/subtree", response_model=SelectionSummary)
async def subtree_selection(
    catalog: CatalogDep,
    session_id: int | None = Query(default=None),
    tape_id: int | None = Query(default=None),
    dir_path: str = Query(default="", alias="dir"),
    latest_only: bool = Query(default=True),
) -> SelectionSummary:
    """Expand a subtree into selectable catalog file ids.

    Args:
        catalog: Catalog repository.
        session_id: BackupSession primary key (session mode).
        tape_id: Tape primary key (merged mode; ``latest_only`` picks
            the newest version of each path).
        dir: Directory prefix ("" selects the whole scope).
        latest_only: Only meaningful in merged mode.

    Returns:
        The selection summary.

    Raises:
        HTTPException: 422 when the scope is ambiguous or missing, 404
            when the session/tape does not exist.
    """
    _validate_scope(session_id, tape_id)
    if session_id is not None:
        if catalog.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session_selection(catalog, session_id, dir_path)
    assert tape_id is not None
    if catalog.get_tape(tape_id) is None:
        raise HTTPException(status_code=404, detail="Tape not found")
    return tape_selection(catalog, tape_id, dir_path, latest_only=latest_only)


def _validate_scope(session_id: int | None, tape_id: int | None) -> None:
    """Ensure exactly one listing scope is given.

    Args:
        session_id: Session scope argument.
        tape_id: Tape scope argument.

    Raises:
        HTTPException: 422 when the scope is ambiguous or missing.
    """
    if (session_id is None) == (tape_id is None):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of session_id or tape_id",
        )
