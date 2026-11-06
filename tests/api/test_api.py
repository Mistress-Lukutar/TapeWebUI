"""
File:   test_api.py
Brief:  API smoke tests against the app with the fake transport.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.0
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    """The health endpoint reports liveness."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["drive_reachable"] is True


def test_drive_status(client: TestClient) -> None:
    """Drive status returns the parsed identity and position."""
    response = client.get("/api/drive/status")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["manufacturer"] == "IBM"
    assert body["product_id"] == "ULTRIUM-TD3"
    assert body["revision"] == "93G0"
    assert body["block_id"] == 0
    assert body["compression"] is True
    assert body["parms_block_size"] == 65536


def test_drive_refresh(client: TestClient) -> None:
    """Forced refresh re-queries ITDT and returns fresh status."""
    response = client.post("/api/drive/refresh")
    assert response.status_code == 200
    assert response.json()["reachable"] is True


def test_drive_erase(client: TestClient) -> None:
    """Erase issues rewind + short erase and returns refreshed status."""
    transport = client.app.state.transport  # type: ignore[union-attr]
    transport.commands.clear()
    response = client.post("/api/drive/erase")
    assert response.status_code == 200
    assert response.json()["reachable"] is True
    assert [cmd[0] for cmd in transport.commands] == ["rewind", "erase", "vpd",
                                                      "qrypos", "getparms"]
    assert ("erase", "-short") in transport.commands
