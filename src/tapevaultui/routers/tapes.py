"""
File:   tapes.py
Brief:  Tapes, catalog search and history API endpoints.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from tapevaultui.db.models import BackupSession, FileEntry
from tapevaultui.dependencies import CatalogDep

router = APIRouter(prefix="/api", tags=["tapes"])


class TapeOut(BaseModel):
    """Tape list item."""

    id: int
    uuid: str
    label: str
    status: str
    bytes_used: int
    created_at: str
    notes: str


class TapeUpdate(BaseModel):
    """Mutable tape fields."""

    label: str | None = None
    status: str | None = None
    notes: str | None = None


class SessionOut(BaseModel):
    """Session summary."""

    id: int
    tape_seq: int
    job_name: str
    started_at: str
    finished_at: str | None
    status: str
    files_count: int
    bytes_total: int


class FileOut(BaseModel):
    """File entry summary."""

    id: int
    session_id: int
    tape_file_index: int
    path: str
    size: int
    sha256: str
    status: str


class CatalogSearchResult(BaseModel):
    """A file search hit with tape context."""

    file: FileOut
    session: SessionOut
    tape_id: int
    tape_uuid: str
    tape_label: str


class TapeNotesUpdateResponse(BaseModel):
    """Erased-session count response."""

    erased_sessions: int


@router.get("/tapes", response_model=list[TapeOut])
async def list_tapes(catalog: CatalogDep) -> list[TapeOut]:
    """List all known tapes.

    Args:
        catalog: Catalog repository.

    Returns:
        Tapes ordered by creation time.
    """
    return [
        TapeOut(
            id=tape.id,
            uuid=tape.uuid,
            label=tape.label,
            status=tape.status,
            bytes_used=tape.bytes_used,
            created_at=tape.created_at.isoformat(),
            notes=tape.notes,
        )
        for tape in catalog.list_tapes()
    ]


@router.get("/tapes/{tape_id}")
async def get_tape(tape_id: int, catalog: CatalogDep) -> dict[str, object]:
    """Tape details with its sessions.

    Args:
        tape_id: Tape primary key.
        catalog: Catalog repository.

    Returns:
        Tape fields plus session list.

    Raises:
        HTTPException: 404 if the tape does not exist.
    """
    tape = catalog.get_tape(tape_id)
    if tape is None:
        raise HTTPException(status_code=404, detail=f"Tape not found: {tape_id}")
    sessions = [
        SessionOut(
            id=session.id,
            tape_seq=session.tape_seq,
            job_name=session.job_name,
            started_at=session.started_at.isoformat(),
            finished_at=session.finished_at.isoformat()
            if session.finished_at
            else None,
            status=session.status,
            files_count=session.files_count,
            bytes_total=session.bytes_total,
        )
        for session in catalog.list_sessions(tape_id)
    ]
    return {
        "id": tape.id,
        "uuid": tape.uuid,
        "label": tape.label,
        "status": tape.status,
        "bytes_used": tape.bytes_used,
        "created_at": tape.created_at.isoformat(),
        "notes": tape.notes,
        "sessions": [session.model_dump() for session in sessions],
    }


@router.put("/tapes/{tape_id}", response_model=TapeOut)
async def update_tape(
    tape_id: int, payload: TapeUpdate, catalog: CatalogDep
) -> TapeOut:
    """Update tape label/status/notes.

    Args:
        tape_id: Tape primary key.
        payload: Fields to change.
        catalog: Catalog repository.

    Returns:
        The updated tape.

    Raises:
        HTTPException: 404 or 422 on invalid input.
    """
    try:
        tape = catalog.update_tape(
            tape_id, label=payload.label, status=payload.status, notes=payload.notes
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if tape is None:
        raise HTTPException(status_code=404, detail=f"Tape not found: {tape_id}")
    return TapeOut(
        id=tape.id,
        uuid=tape.uuid,
        label=tape.label,
        status=tape.status,
        bytes_used=tape.bytes_used,
        created_at=tape.created_at.isoformat(),
        notes=tape.notes,
    )


@router.post("/tapes/{tape_id}/erase-mark")
async def erase_mark_tape(tape_id: int, catalog: CatalogDep) -> TapeNotesUpdateResponse:
    """Mark all tape sessions erased (after a physical erase).

    Args:
        tape_id: Tape primary key.
        catalog: Catalog repository.

    Returns:
        Number of sessions marked erased.

    Raises:
        HTTPException: 404 if the tape does not exist.
    """
    if catalog.get_tape(tape_id) is None:
        raise HTTPException(status_code=404, detail=f"Tape not found: {tape_id}")
    count = catalog.mark_tape_erased(tape_id)
    return TapeNotesUpdateResponse(erased_sessions=count)


@router.get("/tapes/{tape_id}/files", response_model=list[FileOut])
async def tape_files(
    tape_id: int,
    catalog: CatalogDep,
    session_id: int | None = Query(default=None),
    exclude_erased: bool = Query(default=True),
) -> list[FileOut]:
    """List files stored on a tape, optionally filtered by session.

    Args:
        tape_id: Tape primary key.
        catalog: Catalog repository.
        session_id: Optional session filter.
        exclude_erased: Skip sessions marked erased (default).

    Returns:
        File entries in tape order.

    Raises:
        HTTPException: 404 if the tape does not exist.
    """
    if catalog.get_tape(tape_id) is None:
        raise HTTPException(status_code=404, detail=f"Tape not found: {tape_id}")
    results: list[FileOut] = []
    for session in catalog.list_sessions(tape_id):
        if session_id is not None and session.id != session_id:
            continue
        if exclude_erased and session.status == "erased":
            continue
        for entry in catalog.list_files(session.id):
            results.append(_file_out(entry))
    return results


@router.get("/catalog/search", response_model=list[CatalogSearchResult])
async def catalog_search(
    catalog: CatalogDep,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[CatalogSearchResult]:
    """Search files by path substring across non-erased sessions.

    Args:
        catalog: Catalog repository.
        q: Case-insensitive path substring.
        limit: Maximum number of hits.

    Returns:
        Matching files with session and tape context.
    """
    hits: list[CatalogSearchResult] = []
    lowered = q.lower()
    for tape in catalog.list_tapes():
        for session in catalog.list_sessions(tape.id):
            if session.status == "erased":
                continue
            for entry in catalog.list_files(session.id):
                if lowered in entry.path.lower():
                    hits.append(
                        CatalogSearchResult(
                            file=_file_out(entry),
                            session=_session_out(session),
                            tape_id=tape.id,
                            tape_uuid=tape.uuid,
                            tape_label=tape.label,
                        )
                    )
                    if len(hits) >= limit:
                        return hits
    return hits


@router.get("/history")
async def history(
    catalog: CatalogDep,
    job_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, object]]:
    """List job runs (backups and restores), newest first.

    Args:
        catalog: Catalog repository.
        job_id: Optional job filter.
        limit: Maximum number of rows.

    Returns:
        Job run dicts.
    """
    runs = catalog.list_job_runs(job_id=job_id)[:limit]
    return [
        {
            "id": run.id,
            "job_id": run.job_id,
            "session_id": run.session_id,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "status": run.status,
            "error": run.error,
            "stats": run.stats_json,
        }
        for run in runs
    ]


def _file_out(entry: FileEntry) -> FileOut:
    """Convert a FileEntry row to the API model.

    Args:
        entry: Catalog file row.

    Returns:
        FileOut payload.
    """
    return FileOut(
        id=entry.id,
        session_id=entry.session_id,
        tape_file_index=entry.tape_file_index,
        path=entry.path,
        size=entry.size,
        sha256=entry.sha256,
        status=entry.status,
    )


def _session_out(session: BackupSession) -> SessionOut:
    """Convert a BackupSession row to the API model.

    Args:
        session: Catalog session row.

    Returns:
        SessionOut payload.
    """
    return SessionOut(
        id=session.id,
        tape_seq=session.tape_seq,
        job_name=session.job_name,
        started_at=session.started_at.isoformat(),
        finished_at=session.finished_at.isoformat() if session.finished_at else None,
        status=session.status,
        files_count=session.files_count,
        bytes_total=session.bytes_total,
    )
