"""
File:   conftest.py
Brief:  Shared pytest fixtures: isolated env, fake transport, API client.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapevaultui.config import get_settings
from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "itdt"


def load_fixture(name: str) -> str:
    """Load an ITDT output fixture by file name.

    Args:
        name: Fixture file name, e.g. ``fixture_vpd.txt``.

    Returns:
        Fixture text content.
    """
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate each test: fake transport, temp data dir, fresh settings.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch helper.

    Yields:
        None.
    """
    monkeypatch.setenv("TAPE_FAKE_TRANSPORT", "true")
    monkeypatch.setenv("TAPE_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_transport() -> FakeItDtTransport:
    """Fake transport preloaded with the real captured fixtures.

    Returns:
        FakeItDtTransport serving vpd/qrypos/getparms/devinfo fixtures.
    """
    return FakeItDtTransport(
        scenarios={
            "vpd": (load_fixture("fixture_vpd.txt"), 0),
            "qrypos": (load_fixture("fixture_qrypos.txt"), 0),
            "getparms": (load_fixture("fixture_getparms.txt"), 0),
            "devinfo": (load_fixture("fixture_devinfo.txt"), 0),
            "inqj": (load_fixture("fixture_inqj.txt"), 0),
        }
    )


@pytest.fixture
def client(
    fake_transport: FakeItDtTransport, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Test client with the fake transport wired into the app lifespan.

    Args:
        fake_transport: Preloaded fake transport.
        monkeypatch: Pytest monkeypatch helper.

    Yields:
        Configured TestClient.
    """
    # Make the lifespan itself build the preloaded fake transport.
    monkeypatch.setattr(
        "tapevaultui.main.FakeItDtTransport",
        lambda: fake_transport,
    )
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def pytest_configure(config: Any) -> None:
    """Register custom pytest markers.

    Args:
        config: Pytest config object.
    """
    config.addinivalue_line("markers", "unit: fast isolated unit tests")
