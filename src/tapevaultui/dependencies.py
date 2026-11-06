"""
File:   dependencies.py
Brief:  FastAPI dependency accessors for services stored on app.state.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.3.0
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from tapevaultui.config import Settings
from tapevaultui.services.drive_service import DriveService
from tapevaultui.services.job_defs import JobDefStore
from tapevaultui.services.job_runtime import JobRuntime


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


def get_job_def_store(request: Request) -> JobDefStore:
    """Return the job definition store.

    Args:
        request: Current HTTP request.

    Returns:
        The JobDefStore instance created in the app lifespan.
    """
    store: JobDefStore = request.app.state.job_defs
    return store


def get_job_runtime(request: Request) -> JobRuntime:
    """Return the job runtime.

    Args:
        request: Current HTTP request.

    Returns:
        The JobRuntime instance created in the app lifespan.
    """
    runtime: JobRuntime = request.app.state.job_runtime
    return runtime


SettingsDep = Annotated[Settings, Depends(get_settings)]
DriveServiceDep = Annotated[DriveService, Depends(get_drive_service)]
JobDefStoreDep = Annotated[JobDefStore, Depends(get_job_def_store)]
JobRuntimeDep = Annotated[JobRuntime, Depends(get_job_runtime)]
