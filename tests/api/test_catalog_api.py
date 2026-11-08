"""
File:   test_catalog_api.py
Brief:  API tests for tapes, catalog search, history and restore endpoints.
Author: Mistress-Lukutar
Date:   2026-11-08
Version: v0.9.0
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from tapevaultui.services.tape_format import build_session_header, serialize_header


def _seed(client: TestClient) -> tuple[int, list[int]]:
    """Seed a tape/session/files through the app's catalog.

    Args:
        client: Test client with a running lifespan.

    Returns:
        (tape_row_id, file_ids) tuple.
    """
    catalog = client.app.state.catalog  # type: ignore[attr-defined]
    tape = catalog.register_tape("uuid-api-tape", label="API Tape")
    session = catalog.create_session(tape.id, job_name="apijob")
    mtime = datetime.now(UTC)
    ids = []
    for index in range(1, 3):
        entry = catalog.add_file(
            session.id,
            tape_file_index=index,
            path=f"D:/Photos/pic{index}.jpg",
            size=10 * index,
            mtime=mtime,
            sha256="a" * 64,
        )
        ids.append(entry.id)
    return tape.id, ids


def test_tapes_list_and_details(client: TestClient) -> None:
    """Tapes appear in the list with sessions in the details."""
    tape_id, _ = _seed(client)
    response = client.get("/api/tapes")
    assert response.status_code == 200
    assert any(t["uuid"] == "uuid-api-tape" for t in response.json())

    response = client.get(f"/api/tapes/{tape_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "API Tape"
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["kind"] == "full"

    assert client.get("/api/tapes/9999").status_code == 404


def test_tape_files_endpoint(client: TestClient) -> None:
    """Tape files are listed in tape order."""
    tape_id, _ = _seed(client)
    response = client.get(f"/api/tapes/{tape_id}/files")
    assert response.status_code == 200
    indexes = [f["tape_file_index"] for f in response.json()]
    assert indexes == [1, 2]


def test_catalog_search(client: TestClient) -> None:
    """Search finds files by path substring with tape context."""
    _, _ = _seed(client)
    response = client.get("/api/catalog/search", params={"q": "pic1"})
    assert response.status_code == 200
    hits = response.json()
    assert len(hits) == 1
    assert hits[0]["tape_uuid"] == "uuid-api-tape"


def test_history_endpoint(client: TestClient) -> None:
    """History lists job runs after one is recorded."""
    catalog = client.app.state.catalog  # type: ignore[attr-defined]
    run = catalog.create_job_run()
    catalog.update_job_run(run.id, status="done", finished=True)

    response = client.get("/api/history")
    assert response.status_code == 200
    assert any(row["id"] == run.id for row in response.json())


def test_restore_plan_and_run(client: TestClient) -> None:
    """Plan builds from file ids; run waits for the tape and cancels."""
    _, file_ids = _seed(client)

    response = client.post("/api/restore/plan", json={"file_ids": file_ids})
    assert response.status_code == 200
    plan = response.json()
    assert len(plan["tapes"]) == 1
    assert plan["tapes"][0]["tape_id"] == "uuid-api-tape"
    assert plan["files_count"] == 2

    response = client.post(
        "/api/restore/run",
        json={
            "file_ids": file_ids,
            "destination_mode": "alternate",
            "destination_dir": "Z:/out",
            "collision": "rename",
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True

    # The blank default fake has no matching tape: engine waits for it.
    for _ in range(50):
        status = client.get("/api/restore/status").json()
        if status["active"] and status["active"]["state"] == "waiting_media":
            break
        time.sleep(0.05)
    assert status["active"]["state"] == "waiting_media"
    assert status["active"]["tape_id"] == "uuid-api-tape"

    response = client.post("/api/restore/cancel")
    assert response.json()["cancelled"] is True
    for _ in range(50):
        status = client.get("/api/restore/status").json()
        if status["active"] and status["active"]["state"] == "cancelled":
            break
        time.sleep(0.05)
    assert status["active"]["state"] == "cancelled"


def test_tape_delete_endpoint(client: TestClient) -> None:
    """Delete refuses live sessions, then cascades an erased tape."""
    tape_id, _ = _seed(client)

    response = client.delete(f"/api/tapes/{tape_id}")
    assert response.status_code == 409
    assert "non-erased" in response.json()["detail"]
    assert client.get(f"/api/tapes/{tape_id}").status_code == 200

    catalog = client.app.state.catalog  # type: ignore[attr-defined]
    catalog.mark_tape_erased(tape_id)

    response = client.delete(f"/api/tapes/{tape_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted_sessions"] == 1
    assert body["deleted_files"] == 2
    assert client.get(f"/api/tapes/{tape_id}").status_code == 404
    assert client.get("/api/tapes").json() == []
    assert client.delete(f"/api/tapes/{tape_id}").status_code == 404


def test_tape_adopt_blank_tape(client: TestClient) -> None:
    """Adoption arms on a blank tape and the list marks it pending.

    The default fake transport reads as a blank cartridge, so the
    adopt precondition is satisfied without extra scenarios.
    """
    tape_id, _ = _seed(client)
    catalog = client.app.state.catalog  # type: ignore[attr-defined]
    catalog.mark_tape_erased(tape_id)

    response = client.post(f"/api/tapes/{tape_id}/adopt")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == tape_id
    assert body["adoption_pending"] is True
    assert body["uuid"] != "uuid-api-tape"  # re-identified at arm time

    response = client.get("/api/tapes")
    assert response.status_code == 200
    assert response.json()[0]["adoption_pending"] is True

    response = client.delete(f"/api/tapes/{tape_id}/adopt")
    assert response.status_code == 200
    assert response.json()["adoption_pending"] is False
    assert client.get("/api/tapes").json()[0]["adoption_pending"] is False


def test_tape_adopt_rejects_tapewebui_tape(client: TestClient) -> None:
    """Adoption refuses a loaded tape that already holds sessions."""
    tape_id, _ = _seed(client)
    header = build_session_header(
        tape_id="on-tape-uuid",
        session_id=str(uuid4()),
        started_at=datetime.now(UTC),
    )
    fake = client.app.state.transport  # type: ignore[attr-defined]
    fake.scenarios["read"] = (serialize_header(header).decode(), 0)

    response = client.post(f"/api/tapes/{tape_id}/adopt")
    assert response.status_code == 409
    assert "already holds" in response.json()["detail"]
    assert client.post("/api/tapes/9999/adopt").status_code == 404


def test_restore_plan_validation(client: TestClient) -> None:
    """Unknown file ids give 422."""
    response = client.post("/api/restore/plan", json={"file_ids": [424242]})
    assert response.status_code == 422
