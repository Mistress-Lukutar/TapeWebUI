"""
File:   settings.py
Brief:  Application settings API (Settings page): editable subset + read-only info.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from tapevaultui import __version__
from tapevaultui.dependencies import AppSettingsDep, SettingsDep
from tapevaultui.services.app_settings import EditableSettings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class AppInfo(BaseModel):
    """Read-only application/environment information."""

    version: str
    host: str
    port: int
    device_name: str
    itdt_exe: str
    force_generic_dd: bool
    fake_transport: bool
    data_dir: str
    log_dir: str
    catalog_db_size_bytes: int | None = None


class SettingsResponse(BaseModel):
    """Settings page payload: editable values + read-only info."""

    editable: EditableSettings
    info: AppInfo


def _build_info(settings_dep: SettingsDep) -> AppInfo:
    """Build the read-only info section from settings.

    Args:
        settings_dep: Application settings.

    Returns:
        AppInfo with paths, environment facts and catalog DB size.
    """
    catalog_size: int | None = None
    catalog_path = settings_dep.catalog_db_path
    if catalog_path.exists():
        catalog_size = catalog_path.stat().st_size
    return AppInfo(
        version=__version__,
        host=settings_dep.host,
        port=settings_dep.port,
        device_name=settings_dep.device_name,
        itdt_exe=str(settings_dep.itdt_exe),
        force_generic_dd=settings_dep.force_generic_dd,
        fake_transport=settings_dep.fake_transport,
        data_dir=str(settings_dep.data_dir),
        log_dir=str(settings_dep.log_dir),
        catalog_db_size_bytes=catalog_size,
    )


@router.get("", response_model=SettingsResponse)
async def get_settings_page(
    settings_dep: SettingsDep, app_settings: AppSettingsDep
) -> SettingsResponse:
    """Return the Settings page payload.

    Args:
        settings_dep: Application settings.
        app_settings: Editable-settings service.

    Returns:
        Current editable values plus read-only environment info.
    """
    return SettingsResponse(
        editable=app_settings.editable(), info=_build_info(settings_dep)
    )


@router.put("", response_model=SettingsResponse)
async def update_settings(
    values: EditableSettings,
    settings_dep: SettingsDep,
    app_settings: AppSettingsDep,
) -> SettingsResponse:
    """Apply and persist new editable settings.

    Args:
        values: New editable values (validated by the model).
        settings_dep: Application settings.
        app_settings: Editable-settings service.

    Returns:
        The refreshed Settings page payload.
    """
    app_settings.update(values)
    return SettingsResponse(
        editable=app_settings.editable(), info=_build_info(settings_dep)
    )
