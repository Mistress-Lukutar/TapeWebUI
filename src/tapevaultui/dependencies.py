"""
File:   dependencies.py
Brief:  FastAPI dependency accessors for services stored on app.state.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from tapevaultui.config import Settings
from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport, ItDtTransport
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.drive_service import DriveService
from tapevaultui.services.job_defs import JobDefStore
from tapevaultui.services.job_runtime import JobRuntime
from tapevaultui.services.restore_engine import RestoreEngine
from tapevaultui.services.scheduler_service import SchedulerService


def get_settings(request: Request) -> Settings:
    """Return application settings.

    Args:
        request: Current HTTP request.

    Returns:
        The Settings instance created in the app lifespan.
    """
    settings: Settings = request.app.state.settings
    return settings


def get_transport(
    request: Request,
) -> ItDtTransport | FakeItDtTransport:
    """Return the ITDT transport.

    Args:
        request: Current HTTP request.

    Returns:
        The transport instance created in the app lifespan.
    """
    transport: ItDtTransport | FakeItDtTransport = request.app.state.transport
    return transport


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


def get_catalog(request: Request) -> CatalogRepository:
    """Return the catalog repository.

    Args:
        request: Current HTTP request.

    Returns:
        The CatalogRepository instance created in the app lifespan.
    """
    catalog: CatalogRepository = request.app.state.catalog
    return catalog


def get_restore_engine(request: Request) -> RestoreEngine:
    """Return the restore engine.

    Args:
        request: Current HTTP request.

    Returns:
        The RestoreEngine instance created in the app lifespan.
    """
    engine: RestoreEngine = request.app.state.restore_engine
    return engine


def get_scheduler(request: Request) -> SchedulerService:
    """Return the scheduler service.

    Args:
        request: Current HTTP request.

    Returns:
        The SchedulerService instance created in the app lifespan.
    """
    scheduler: SchedulerService = request.app.state.scheduler
    return scheduler


SettingsDep = Annotated[Settings, Depends(get_settings)]
TransportDep = Annotated[ItDtTransport | FakeItDtTransport, Depends(get_transport)]
DriveServiceDep = Annotated[DriveService, Depends(get_drive_service)]
JobDefStoreDep = Annotated[JobDefStore, Depends(get_job_def_store)]
JobRuntimeDep = Annotated[JobRuntime, Depends(get_job_runtime)]
CatalogDep = Annotated[CatalogRepository, Depends(get_catalog)]
RestoreEngineDep = Annotated[RestoreEngine, Depends(get_restore_engine)]
SchedulerDep = Annotated[SchedulerService, Depends(get_scheduler)]
