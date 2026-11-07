"""
File:   test_tape_sync_api.py
Brief:  API tests for tape verification, erase attribution and stale notice.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.session_io import new_session_id
from tapevaultui.services.tape_format import (
    HeaderFileEntry,
    build_session_header,
    serialize_header,
)

TAPE_UUID = "11111111-1111-1111-1111-111111111111"


def _header_bytes(files: list[tuple[str, int]] | None = None) -> bytes:
    """Serialize a session header for the simulated tape.

    Args:
        files: (path, size) pairs recorded in the header.

    Returns:
        Serialized header bytes.
    """
    header = build_session_header(
        tape_id=TAPE_UUID,
        session_id=new_session_id(),
        job_name="photos",
        started_at=datetime.now(UTC),
        files=[
            HeaderFileEntry(
                seq=seq,
                path=path,
                size=size,
                mtime=0.0,
                sha256="0" * 64,
                tape_file_index=seq + 1,
            )
            for seq, (path, size) in enumerate(files or [])
        ],
    )
    return serialize_header(header)


def _register_tape_with_session(
    catalog: CatalogRepository, *, tmp_files: list[tuple[str, int]] | None = None
) -> int:
    """Register the test tape with one live session.

    Args:
        catalog: Catalog repository from the app state.
        tmp_files: File entries for the session (empty when None).

    Returns:
        The tape primary key.
    """
    tape = catalog.register_tape(TAPE_UUID, label="Vault 1")
    session = catalog.create_session(tape.id, job_name="photos")
    for seq, (path, size) in enumerate(tmp_files or []):
        catalog.add_file(
            session.id,
            tape_file_index=seq + 1,
            path=path,
            size=size,
            mtime=datetime.now(UTC),
            sha256="0" * 64,
        )
    catalog.finish_session(session.id, status="ok")
    return tape.id


def test_verify_tape_blank_by_default(client: TestClient) -> None:
    """With no readable header the verify reports a blank cartridge."""
    response = client.post("/api/drive/verify-tape")
    assert response.status_code == 200
    assert response.json()["status"] == "blank"


def test_verify_tape_match(client: TestClient) -> None:
    """A matching header stamps the tape verified and returns match."""
    transport = client.app.state.transport  # type: ignore[attr-defined]
    catalog: CatalogRepository = client.app.state.catalog  # type: ignore[attr-defined]
    tape_id = _register_tape_with_session(catalog)
    transport.tape_files = [_header_bytes()]
    response = client.post("/api/drive/verify-tape")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "match"
    assert body["tape_id"] == tape_id
    assert body["report"] is None
    tapes = client.get("/api/tapes").json()
    assert tapes[0]["last_verified_at"] is not None


def test_verify_tape_deep_report(client: TestClient) -> None:
    """Deep verify returns a session-level comparison report."""
    transport = client.app.state.transport  # type: ignore[attr-defined]
    catalog: CatalogRepository = client.app.state.catalog  # type: ignore[attr-defined]
    _register_tape_with_session(catalog, tmp_files=[("a.txt", 10)])
    # Tape holds only the header session (data erased from tape).
    transport.tape_files = [_header_bytes(files=[("a.txt", 10)])]
    response = client.post("/api/drive/verify-tape?deep=true")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "match"
    report = body["report"]
    assert report is not None
    assert report["on_tape_count"] == 1
    assert report["catalog_count"] == 1
    assert report["diffs"][0]["kind"] == "match"


def test_erase_marks_attributed_tape_erased(client: TestClient) -> None:
    """Erase with an identified tape auto-syncs the catalog."""
    transport = client.app.state.transport  # type: ignore[attr-defined]
    catalog: CatalogRepository = client.app.state.catalog  # type: ignore[attr-defined]
    tape_id = _register_tape_with_session(catalog)
    transport.tape_files = [_header_bytes()]
    response = client.post("/api/drive/erase")
    assert response.status_code == 200
    assert "Catalog synced" in response.json()["catalog_message"]
    sessions = catalog.list_sessions(tape_id)
    assert all(s.status == "erased" for s in sessions)
    assert client.get("/api/sync/stale-notice").json() is None


def test_service_erase_op_outputs_attribution(client: TestClient) -> None:
    """The Service-page short erase appends the catalog sync message."""
    transport = client.app.state.transport  # type: ignore[attr-defined]
    catalog: CatalogRepository = client.app.state.catalog  # type: ignore[attr-defined]
    _register_tape_with_session(catalog)
    transport.tape_files = [_header_bytes()]
    response = client.post("/api/service/ops/erase_short")
    assert response.status_code == 200
    assert "Catalog synced" in response.json()["output"]


def test_stale_notice_roundtrip(client: TestClient) -> None:
    """The stale notice appears after an unattributed erase and dismisses."""
    assert client.get("/api/sync/stale-notice").json() is None
    client.post("/api/drive/erase")  # blank tape: unattributed
    notice = client.get("/api/sync/stale-notice").json()
    assert notice is not None and notice["reason"] == "unattributed_erase"
    assert client.post("/api/sync/stale-notice/dismiss").json() == {"dismissed": True}
    assert client.get("/api/sync/stale-notice").json() is None


def test_sessions_erase_mark_applies_deep_report(
    client: TestClient, tmp_path: Path
) -> None:
    """The sessions erase-mark endpoint marks exactly the listed ids."""
    catalog: CatalogRepository = client.app.state.catalog  # type: ignore[attr-defined]
    tape_id = _register_tape_with_session(catalog)
    second = catalog.create_session(tape_id, job_name="docs")
    catalog.finish_session(second.id, status="ok")
    unknown_id = second.id + 500
    response = client.post(
        f"/api/tapes/{tape_id}/sessions/erase-mark",
        json={"session_ids": [second.id, unknown_id]},
    )
    assert response.status_code == 200
    assert response.json()["erased_sessions"] == 1
    statuses = [s.status for s in catalog.list_sessions(tape_id)]
    assert statuses.count("erased") == 1


def test_sessions_erase_mark_unknown_tape(client: TestClient) -> None:
    """Unknown tape ids are rejected with 404."""
    response = client.post(
        "/api/tapes/9999/sessions/erase-mark", json={"session_ids": [1]}
    )
    assert response.status_code == 404


@pytest.mark.parametrize("payload", [{}])
def test_sessions_erase_mark_requires_ids(
    client: TestClient, payload: dict[str, object]
) -> None:
    """The endpoint validates the request body."""
    catalog: CatalogRepository = client.app.state.catalog  # type: ignore[attr-defined]
    tape_id = _register_tape_with_session(catalog)
    response = client.post(f"/api/tapes/{tape_id}/sessions/erase-mark", json=payload)
    assert response.status_code == 422
