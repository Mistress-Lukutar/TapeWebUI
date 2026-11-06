"""
File:   test_settings_api.py
Brief:  API tests for the Settings page (editable values + persistence).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_get_settings(client: TestClient) -> None:
    """GET returns editable defaults plus read-only environment info."""
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    editable = body["editable"]
    assert editable["drive_poll_interval"] == 15.0
    assert editable["itdt_timeout_s"] == 600
    assert editable["itdt_open_retry_s"] == 2.0
    assert editable["itdt_open_retries"] == 3
    info = body["info"]
    assert info["version"]
    assert info["device_name"] == "\\\\.\\tape0"
    assert info["fake_transport"] is True
    assert info["force_generic_dd"] is True


def test_put_settings_applies_live(client: TestClient) -> None:
    """PUT applies values to the live drive service and transport."""
    app = client.app
    response = client.put(
        "/api/settings",
        json={
            "drive_poll_interval": 42.0,
            "itdt_timeout_s": 900,
            "itdt_open_retry_s": 5.0,
            "itdt_open_retries": 7,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["editable"]["drive_poll_interval"] == 42.0

    drive = app.state.drive  # type: ignore[attr-defined]
    transport = app.state.transport  # type: ignore[attr-defined]
    assert drive._poll_interval_s == 42.0
    assert transport.last_timeouts == {  # type: ignore[attr-defined]
        "default_timeout_s": 900,
        "open_retry_s": 5.0,
        "open_retries": 7,
    }


def test_put_settings_validates_bounds(client: TestClient) -> None:
    """Out-of-bounds values are rejected with 422."""
    response = client.put(
        "/api/settings",
        json={
            "drive_poll_interval": 0.5,
            "itdt_timeout_s": 600,
            "itdt_open_retry_s": 2.0,
            "itdt_open_retries": 3,
        },
    )
    assert response.status_code == 422


def test_settings_persist_across_restart(client: TestClient) -> None:
    """Overrides survive an app restart via data/settings.json."""
    response = client.put(
        "/api/settings",
        json={
            "drive_poll_interval": 42.0,
            "itdt_timeout_s": 900,
            "itdt_open_retry_s": 5.0,
            "itdt_open_retries": 7,
        },
    )
    assert response.status_code == 200

    # Fresh app instance (same isolated data dir) picks up the overrides.
    from tapevaultui.main import create_app

    with TestClient(create_app()) as restarted:
        body = restarted.get("/api/settings").json()
        assert body["editable"]["drive_poll_interval"] == 42.0
        assert body["editable"]["itdt_timeout_s"] == 900
        drive = restarted.app.state.drive  # type: ignore[attr-defined]
        assert drive._poll_interval_s == 42.0
