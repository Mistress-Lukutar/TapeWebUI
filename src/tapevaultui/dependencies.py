"""
File:   dependencies.py
Brief:  FastAPI dependency accessors for services stored on app.state.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.1.1
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from tapevaultui.config import Settings
from tapevaultui.services.drive_service import DriveService


def get_settings(request: Request) -> Settings:
    """Return application settings.

    Args:
        request: Current HTTP request.

    Returns:
        The Settings instance created in the app lifespan.
    """
    settings: Settings = request.app.state.settings
    return settings


def get_drive_service(request: Request) -> DriveService:
    """Return the drive service.

    Args:
        request: Current HTTP request.

    Returns:
        The DriveService instance created in the app lifespan.
    """
    drive: DriveService = request.app.state.drive
    return drive


SettingsDep = Annotated[Settings, Depends(get_settings)]
DriveServiceDep = Annotated[DriveService, Depends(get_drive_service)]
