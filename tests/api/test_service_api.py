"""
File:   test_service_api.py
Brief:  API tests for the Service page endpoints (statistics + manual ops).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import load_fixture


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


def test_ops_state_initial(client: TestClient) -> None:
    """The state endpoint reports unlocked/not-reserved/loaded defaults."""
    response = client.get("/api/service/ops/state")
    assert response.status_code == 200
    assert response.json() == {
        "door_locked": False,
        "unit_reserved": False,
        "cartridge_loaded": True,
    }


def test_ops_state_follows_paired_ops(client: TestClient) -> None:
    """Paired ops flip the state the toggle buttons render."""
    client.post("/api/service/ops/lock")
    client.post("/api/service/ops/reserve")
    body = client.get("/api/service/ops/state").json()
    assert body["door_locked"] is True
    assert body["unit_reserved"] is True
    assert body["cartridge_loaded"] is True

    client.post("/api/service/ops/unlock")
    client.post("/api/service/ops/release")
    body = client.get("/api/service/ops/state").json()
    assert body["door_locked"] is False
    assert body["unit_reserved"] is False


def test_ops_state_cartridge_unloaded(client: TestClient) -> None:
    """After unload the fake reports no medium; cartridge_loaded flips."""
    client.post("/api/service/ops/unload")
    body = client.get("/api/service/ops/state").json()
    assert body["cartridge_loaded"] is True  # fake qrypos still healthy

    transport = client.app.state.transport  # type: ignore[attr-defined]
    transport.scenarios["qrypos"] = (
        load_fixture("fixture_qrypos_no_medium.txt"),
        2,
    )
    client.post("/api/service/ops/tur")
    body = client.get("/api/service/ops/state").json()
    assert body["cartridge_loaded"] is False
