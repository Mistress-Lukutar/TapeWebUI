"""
File:   test_job_runtime.py
Brief:  Job runtime queue and state machine tests.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.backup_engine import BackupEngine
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.job_defs import JobDefinition
from tapevaultui.services.job_runtime import JobRuntime


def _definition(name: str = "rtjob") -> JobDefinition:
    """Build a minimal job definition.

    Args:
        name: Job name.

    Returns:
        A JobDefinition with verify disabled.
    """
    return JobDefinition.model_validate(
        {
            "name": name,
            "sources": [{"path": "Z:/missing"}],  # snapshot skips it
            "options": {"verify": "none"},
        }
    )


def _make_runtime(tmp_path: Path) -> tuple[JobRuntime, CatalogRepository]:
    """Build a runtime over in-memory catalog and fake transport.

    Args:
        tmp_path: Pytest temp dir for engine work files.

    Returns:
        (runtime, catalog) tuple.
    """
    repo = CatalogRepository("sqlite://")
    repo.create_all()
    engine = BackupEngine(FakeItDtTransport(), repo, tmp_path / "work", EventBus())
    runtime = JobRuntime(engine, repo, EventBus())
    return runtime, repo


async def _wait_terminal(entry: object, timeout_s: float = 3.0) -> str:
    """Wait until an entry reaches a terminal state.

    Args:
        entry: QueueEntry to watch.
        timeout_s: Max seconds to wait.

    Returns:
        The terminal state name.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        state = str(entry.stats.state)  # type: ignore[attr-defined]
        if state in ("done", "failed", "cancelled"):
            return state
        await asyncio.sleep(0.01)
    raise TimeoutError("job did not reach a terminal state")


async def test_job_completes_and_records_run(tmp_path: Path) -> None:
    """An enqueued job runs to done and records the run in the catalog."""
    runtime, _ = _make_runtime(tmp_path)
    entry = runtime.enqueue(_definition())
    state = await _wait_terminal(entry)
    assert state == "done"

    status = runtime.status()
    assert status["active"] is None
    assert status["queue_length"] == 0


async def test_cancel_drops_queued_jobs(tmp_path: Path) -> None:
    """request_cancel marks queued runs cancelled without starting them."""
    runtime, repo = _make_runtime(tmp_path)
    entry = runtime.enqueue(_definition())
    assert runtime.request_cancel() is True
    assert runtime.status()["queue_length"] == 0

    await asyncio.sleep(0.1)  # give the worker a chance to (not) start it
    assert entry.stats.state == "queued"  # never picked up

    run = repo.get_job_run(entry.run_id)
    assert run is not None
    assert run.status == "cancelled"


async def test_runtime_status_shape(tmp_path: Path) -> None:
    """status() exposes active/queued sections before any job."""
    runtime, _ = _make_runtime(tmp_path)
    status = runtime.status()
    assert status["active"] is None
    assert status["queued"] == []
    assert status["queue_length"] == 0
