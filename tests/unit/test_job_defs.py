"""
File:   test_job_defs.py
Brief:  Job definition schema and YAML store tests.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.3.0
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tapevaultui.services.job_defs import (
    JobDefinition,
    JobDefsError,
    JobDefStore,
)

VALID_YAML = """
name: photos
sources:
  - path: "D:/Photos"
    patterns: ["**/*.jpg"]
options:
  mode: append
  verify: readback
  continue_on_error: true
schedule:
  kind: weekly
  days: [sat]
  time: "03:00"
  catch_up: once
"""


def test_parse_and_save_load_roundtrip(tmp_path: Path) -> None:
    """A saved YAML job loads back to an equal definition."""
    store = JobDefStore(tmp_path / "jobs")
    definition = store.parse_yaml(VALID_YAML)
    assert definition.name == "photos"
    assert definition.options.mode == "append"
    assert definition.schedule.days == ["sat"]

    store.save(definition)
    loaded = store.load("photos")
    assert loaded == definition


def test_parse_invalid_yaml(tmp_path: Path) -> None:
    """Broken YAML raises JobDefsError."""
    store = JobDefStore(tmp_path)
    with pytest.raises(JobDefsError, match="Invalid YAML"):
        store.parse_yaml("name: [unclosed")


def test_parse_non_mapping(tmp_path: Path) -> None:
    """A YAML list document is rejected."""
    store = JobDefStore(tmp_path)
    with pytest.raises(JobDefsError, match="mapping"):
        store.parse_yaml("- a\n- b\n")


def test_invalid_name_rejected(tmp_path: Path) -> None:
    """Names with forbidden characters are rejected."""
    store = JobDefStore(tmp_path)
    with pytest.raises(JobDefsError, match="Invalid job name"):
        store.parse_yaml("name: Bad Name!\nsources:\n  - path: D:/x\n")


def test_weekly_requires_days(tmp_path: Path) -> None:
    """Weekly schedules without days are invalid."""
    store = JobDefStore(tmp_path)
    with pytest.raises(JobDefsError, match="at least one day"):
        store.parse_yaml(
            "name: x\nsources:\n  - path: D:/x\nschedule:\n  kind: weekly\n"
        )


def test_days_only_for_weekly(tmp_path: Path) -> None:
    """daily schedules must not carry days."""
    store = JobDefStore(tmp_path)
    with pytest.raises(JobDefsError, match="only valid for weekly"):
        store.parse_yaml(
            "name: x\nsources:\n  - path: D:/x\n"
            "schedule:\n  kind: daily\n  days: [mon]\n"
        )


def test_name_mismatch_guard(tmp_path: Path) -> None:
    """Editing a job under a mismatched name is rejected."""
    store = JobDefStore(tmp_path)
    with pytest.raises(JobDefsError, match="mismatch"):
        store.parse_yaml(
            "name: other\nsources:\n  - path: D:/x\n", expected_name="photos"
        )


def test_list_and_delete(tmp_path: Path) -> None:
    """list_jobs skips invalid files; delete removes the file."""
    store = JobDefStore(tmp_path)
    store.save(store.parse_yaml(VALID_YAML))
    (tmp_path / "broken.yaml").write_text("name: [bad\n", encoding="utf-8")

    jobs = store.list_jobs()
    assert [job.name for job in jobs] == ["photos"]

    store.delete("photos")
    assert store.list_jobs() == []
    with pytest.raises(JobDefsError, match="not found"):
        store.delete("photos")


def test_job_definition_model() -> None:
    """The JobDefinition model requires at least one source."""
    with pytest.raises(ValidationError):
        JobDefinition(name="empty", sources=[])
