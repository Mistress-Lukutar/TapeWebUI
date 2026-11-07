"""
File:   job_defs.py
Brief:  YAML job definitions: pydantic schema, loader and store (data/jobs).
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = structlog.get_logger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SourceDef(BaseModel):
    """One backup source directory with include and exclude patterns."""

    path: str = Field(description="Source directory path")
    patterns: list[str] = Field(
        default_factory=list,
        description="glob patterns relative to path; empty = all files",
    )
    excludes: list[str] = Field(
        default_factory=list,
        description="gitignore-style ignore patterns relative to path",
    )

    @field_validator("excludes")
    @classmethod
    def _check_excludes(cls, value: list[str]) -> list[str]:
        """Normalize ignore patterns and reject Windows separators.

        Args:
            value: Raw pattern list from YAML.

        Returns:
            Stripped, de-duplicated patterns in original order.

        Raises:
            ValueError: If a pattern contains a backslash separator.
        """
        cleaned: list[str] = []
        for pattern in value:
            stripped = pattern.strip()
            if not stripped:
                continue
            if "\\" in stripped:
                raise ValueError(
                    f"Ignore pattern {stripped!r} contains '\\\\': "
                    "use '/' as the path separator"
                )
            if stripped not in cleaned:
                cleaned.append(stripped)
        return cleaned


class JobOptions(BaseModel):
    """Backup behavior options."""

    mode: Literal["append", "overwrite"] = "append"
    verify: Literal["none", "readback"] = "readback"
    continue_on_error: bool = True


class ScheduleSpec(BaseModel):
    """In-app schedule for a job (manual = run from the UI only)."""

    kind: Literal["manual", "daily", "weekly"] = "manual"
    days: list[str] = Field(default_factory=list)
    time: str = "03:00"
    catch_up: Literal["once", "skip"] = "once"

    @field_validator("days")
    @classmethod
    def _check_days(cls, value: list[str]) -> list[str]:
        """Validate weekday names.

        Args:
            value: List of weekday abbreviations.

        Returns:
            The validated list.

        Raises:
            ValueError: If a weekday name is unknown.
        """
        for day in value:
            if day not in _VALID_DAYS:
                raise ValueError(f"Invalid weekday: {day!r}")
        return value

    @field_validator("time")
    @classmethod
    def _check_time(cls, value: str) -> str:
        """Validate HH:MM time.

        Args:
            value: Time string.

        Returns:
            The validated time.

        Raises:
            ValueError: If the time format is wrong.
        """
        if not _TIME_RE.match(value):
            raise ValueError(f"Invalid time (expected HH:MM): {value!r}")
        return value

    @model_validator(mode="after")
    def _check_days_for_weekly(self) -> ScheduleSpec:
        """Weekly schedules must list at least one day.

        Raises:
            ValueError: If kind is weekly and days is empty.
        """
        if self.kind == "weekly" and not self.days:
            raise ValueError("Weekly schedule requires at least one day")
        if self.kind != "weekly" and self.days:
            raise ValueError("days are only valid for weekly schedules")
        return self


class JobDefinition(BaseModel):
    """A complete backup job definition (one YAML file)."""

    name: str
    sources: list[SourceDef] = Field(min_length=1)
    options: JobOptions = Field(default_factory=JobOptions)
    schedule: ScheduleSpec = Field(default_factory=ScheduleSpec)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Validate the job name (used as file name).

        Args:
            value: Job name.

        Returns:
            The validated name.

        Raises:
            ValueError: If the name has forbidden characters.
        """
        if not _NAME_RE.match(value):
            raise ValueError(
                f"Invalid job name {value!r}: use lowercase letters, digits, - and _"
            )
        return value


class JobDefsError(ValueError):
    """Raised when a job definition cannot be parsed or stored."""


class JobDefStore:
    """Loads and stores job YAML files under <data_dir>/jobs."""

    def __init__(self, jobs_dir: Path) -> None:
        """Initialize the store.

        Args:
            jobs_dir: Directory holding <name>.yaml files.
        """
        self._jobs_dir = jobs_dir

    @property
    def jobs_dir(self) -> Path:
        """Directory holding job YAML files."""
        return self._jobs_dir

    def list_jobs(self) -> list[JobDefinition]:
        """List all defined jobs sorted by name.

        Returns:
            Parsed JobDefinition list (invalid files are logged, skipped).

        Raises:
            JobDefsError: If the jobs directory cannot be read.
        """
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        definitions: list[JobDefinition] = []
        for path in sorted(self._jobs_dir.glob("*.yaml")):
            try:
                definitions.append(self._load_file(path))
            except Exception:
                logger.exception("job_definition_invalid", file=str(path))
        return definitions

    def load(self, name: str) -> JobDefinition:
        """Load one job by name.

        Args:
            name: Job name (file stem).

        Returns:
            The parsed JobDefinition.

        Raises:
            JobDefsError: If the file is missing or invalid.
        """
        path = self._path_for(name)
        if not path.exists():
            raise JobDefsError(f"Job not found: {name}")
        return self._load_file(path)

    def save(self, definition: JobDefinition) -> Path:
        """Save a job definition as YAML.

        Args:
            definition: The definition to store.

        Returns:
            The written file path.
        """
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(definition.name)
        payload = definition.model_dump(mode="json")
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        logger.info("job_definition_saved", name=definition.name)
        return path

    def delete(self, name: str) -> None:
        """Delete a job definition file.

        Args:
            name: Job name.

        Raises:
            JobDefsError: If the file does not exist.
        """
        path = self._path_for(name)
        if not path.exists():
            raise JobDefsError(f"Job not found: {name}")
        path.unlink()
        logger.info("job_definition_deleted", name=name)

    def parse_yaml(
        self, content: str, expected_name: str | None = None
    ) -> JobDefinition:
        """Parse and validate YAML text as a job definition.

        Args:
            content: YAML document text.
            expected_name: If set, the parsed name must match (edit guard).

        Returns:
            The parsed JobDefinition.

        Raises:
            JobDefsError: On YAML errors, validation errors or name mismatch.
        """
        try:
            payload = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise JobDefsError(f"Invalid YAML: {exc}") from exc
        if not isinstance(payload, dict):
            raise JobDefsError("Job YAML must be a mapping")
        try:
            definition = JobDefinition.model_validate(payload)
        except ValueError as exc:
            raise JobDefsError(str(exc)) from exc
        if expected_name is not None and definition.name != expected_name:
            raise JobDefsError(
                f"Job name mismatch: file is {expected_name!r}, content is "
                f"{definition.name!r}"
            )
        return definition

    def _path_for(self, name: str) -> Path:
        """Build the YAML path for a job name.

        Args:
            name: Job name.

        Returns:
            Path to <name>.yaml.
        """
        if not _NAME_RE.match(name):
            raise JobDefsError(f"Invalid job name: {name!r}")
        return self._jobs_dir / f"{name}.yaml"

    def _load_file(self, path: Path) -> JobDefinition:
        """Load and validate one YAML file.

        Args:
            path: YAML file path.

        Returns:
            The parsed JobDefinition.

        Raises:
            JobDefsError: On parse or validation errors, or name mismatch.
        """
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise JobDefsError(f"Cannot read {path}: {exc}") from exc
        return self.parse_yaml(content, expected_name=path.stem)
