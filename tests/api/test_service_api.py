"""
File:   test_service_api.py
Brief:  API tests for the Service page endpoints (statistics + manual ops).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_service_statistics(client: TestClient) -> None:
    """Statistics endpoint returns lifecycle counters and drive identity."""
    response = client.get("/api/service/statistics")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["manufacturer"] == "IBM"
    assert body["product_id"] == "ULTRIUM-TD3"
    assert body["revision"] == "93G0"
    assert body["serial"] == "1013000284"
    stats = body["statistics"]
    assert stats["lifetime_poh"] == "1850"
    assert stats["lifetime_media_loads"] == "103"


def test_service_op_simple(client: TestClient) -> None:
    """A simple op issues its ITDT command and returns refreshed status."""
    transport = client.app.state.transport  # type: ignore[attr-defined]
    transport.commands.clear()
    response = client.post("/api/service/ops/unload")
    assert response.status_code == 200
    body = response.json()
    assert body["op"] == "unload"
    assert body["status"]["reachable"] is True
    assert transport.commands[0] == ("unload",)
    # Post-op status refresh happened.
    assert ("vpd",) in transport.commands


def test_service_op_unknown(client: TestClient) -> None:
    """Unknown ops are rejected with 404."""
    response = client.post("/api/service/ops/does_not_exist")
    assert response.status_code == 404


def test_service_op_failure(client: TestClient) -> None:
    """A failing ITDT command surfaces as HTTP 409."""
    transport = client.app.state.transport  # type: ignore[attr-defined]
    transport.scenarios["load"] = (" cartridge motor fault ", 3)
    response = client.post("/api/service/ops/load")
    assert response.status_code == 409
    assert "load failed" in response.json()["detail"]
