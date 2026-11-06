"""
File:   app_settings.py
Brief:  Runtime-editable application settings persisted to data/settings.json.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from tapevaultui.config import Settings
from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtTransport,
)
from tapevaultui.services.drive_service import DriveService

logger = structlog.get_logger(__name__)


class EditableSettings(BaseModel):
    """User-editable runtime settings (Settings page).

    Deliberately a safe subset: tuning values with immediate, bounded
    effect. Paths and the device name stay read-only (changing them
    requires an environment change and a restart anyway).
    """

    drive_poll_interval: float = Field(default=15.0, ge=1.0, le=3600.0)
    itdt_timeout_s: int = Field(default=600, ge=1, le=86400)
    itdt_open_retry_s: float = Field(default=2.0, ge=0.0, le=60.0)
    itdt_open_retries: int = Field(default=3, ge=0, le=20)


class AppSettingsService:
    """Applies and persists user-editable settings overrides.

    Overrides live in ``data/settings.json`` (user content, gitignored)
    and are re-applied on every startup after the env-based ``Settings``
    object is created, so they win over ``TAPE_*`` environment values.
    Live objects (drive poller, transport) are updated in place when
    bound via ``bind_runtime``.
    """

    def __init__(self, settings: Settings, path: Path) -> None:
        """Initialize the service.

        Args:
            settings: The application Settings instance to mutate.
            path: JSON override file path.
        """
        self._settings = settings
        self._path = path
        self._drive: DriveService | None = None
        self._transport: ItDtTransport | FakeItDtTransport | None = None

    def bind_runtime(
        self,
        drive: DriveService,
        transport: ItDtTransport | FakeItDtTransport,
    ) -> None:
        """Bind live objects that receive settings updates in place.

        Args:
            drive: Drive service (poll interval).
            transport: ITDT transport (timeouts, open retries).
        """
        self._drive = drive
        self._transport = transport

    def editable(self) -> EditableSettings:
        """Return the current values of the editable subset.

        Returns:
            EditableSettings built from the live Settings instance.
        """
        return EditableSettings(
            drive_poll_interval=self._settings.drive_poll_interval,
            itdt_timeout_s=self._settings.itdt_timeout_s,
            itdt_open_retry_s=self._settings.itdt_open_retry_s,
            itdt_open_retries=self._settings.itdt_open_retries,
        )

    def load_overrides(self) -> None:
        """Load and apply persisted overrides (called once at startup).

        A missing, corrupt or invalid file is logged and ignored; the
        env/default values stay in effect.
        """
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            values = EditableSettings.model_validate(raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "settings_overrides_invalid", path=str(self._path), error=str(exc)
            )
            return
        self._apply(values)
        logger.info("settings_overrides_applied", path=str(self._path))

    def update(self, values: EditableSettings) -> EditableSettings:
        """Apply new editable values live and persist them.

        Args:
            values: Validated new editable settings.

        Returns:
            The applied values (echo of the input).
        """
        self._apply(values)
        self._persist(values)
        logger.info("settings_updated", **values.model_dump())
        return values

    def _apply(self, values: EditableSettings) -> None:
        """Apply values to the Settings object and bound live objects.

        Args:
            values: The values to apply.
        """
        self._settings.drive_poll_interval = values.drive_poll_interval
        self._settings.itdt_timeout_s = values.itdt_timeout_s
        self._settings.itdt_open_retry_s = values.itdt_open_retry_s
        self._settings.itdt_open_retries = values.itdt_open_retries
        if self._drive is not None:
            self._drive.update_poll_interval(values.drive_poll_interval)
        if self._transport is not None:
            self._transport.update_timeouts(
                default_timeout_s=values.itdt_timeout_s,
                open_retry_s=values.itdt_open_retry_s,
                open_retries=values.itdt_open_retries,
            )

    def _persist(self, values: EditableSettings) -> None:
        """Write overrides to the JSON file atomically.

        Args:
            values: The values to persist.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(values.model_dump(), indent=2), encoding="utf-8")
        temp.replace(self._path)
