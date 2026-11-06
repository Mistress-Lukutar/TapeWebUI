"""
File:   main.py
Brief:  FastAPI app factory, lifespan, SSE endpoint and static mounting.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.2.0
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from tapevaultui.config import Settings, get_settings
from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport, ItDtTransport
from tapevaultui.routers import device as device_router
from tapevaultui.services.drive_service import DriveService
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.sse_manager import sse_manager

logger = structlog.get_logger(__name__)

SSE_TOPICS: tuple[str, ...] = (
    "drive",
    "job",
    "schedule",
    "catalog",
    "restore",
    "error",
)


def configure_logging() -> None:
    """Configure structlog with a console renderer."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def _make_sse_bridge(topic: str) -> Any:
    """Create an event-bus handler that broadcasts a topic over SSE.

    Args:
        topic: EventBus topic to bridge.

    Returns:
        Async handler broadcasting payloads on that topic.
    """

    async def bridge(payload: dict[str, Any]) -> None:
        sse_manager.broadcast(topic, payload)

    return bridge


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: create services, start polling, stop on exit.

    Args:
        app: The FastAPI application instance.

    Yields:
        None once startup is complete.
    """
    configure_logging()
    settings: Settings = app.state.settings
    settings.ensure_dirs()

    event_bus = EventBus()
    for topic in SSE_TOPICS:
        event_bus.subscribe(topic, _make_sse_bridge(topic))

    if settings.fake_transport:
        transport: ItDtTransport | FakeItDtTransport = FakeItDtTransport()
        logger.warning("using_fake_itdt_transport")
    else:
        transport = ItDtTransport(
            itdt_exe=settings.itdt_exe,
            device_name=settings.device_name,
            force_generic_dd=settings.force_generic_dd,
            default_timeout_s=settings.itdt_timeout_s,
            open_retry_s=settings.itdt_open_retry_s,
            open_retries=settings.itdt_open_retries,
        )

    drive = DriveService(
        transport=transport,
        event_bus=event_bus,
        poll_interval_s=settings.drive_poll_interval,
    )
    app.state.event_bus = event_bus
    app.state.transport = transport
    app.state.drive = drive
    await drive.start()

    yield

    await drive.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI instance.
    """
    app = FastAPI(title="TapeWebUI", version="0.1.0", lifespan=lifespan)
    app.state.settings = get_settings()

    app.include_router(device_router.router)

    @app.get("/events")
    async def events() -> StreamingResponse:
        """SSE stream of application events.

        Returns:
            StreamingResponse yielding SSE messages.
        """
        return StreamingResponse(
            sse_manager.subscribe(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        """Liveness probe.

        Returns:
            JSON with status and drive reachability.
        """
        drive: DriveService | None = getattr(app.state, "drive", None)
        return JSONResponse(
            {
                "status": "ok",
                "drive_reachable": drive.last_status.reachable
                if drive and drive.last_status
                else None,
            }
        )

    _mount_static(app)
    return app


def _mount_static(app: FastAPI) -> None:
    """Serve the built frontend SPA from static/dist when present.

    Args:
        app: The FastAPI application instance.
    """
    dist_dir = Path(__file__).resolve().parents[2] / "static" / "dist"
    index_html = dist_dir / "index.html"
    if not index_html.exists():
        logger.warning("frontend_build_missing", dist_dir=str(dist_dir))
        return

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        """Serve SPA assets or fall back to index.html.

        Args:
            full_path: Requested path below the root.

        Returns:
            The requested file or the SPA entry point.
        """
        candidate = (dist_dir / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist_dir):
            return FileResponse(candidate)
        return FileResponse(index_html)


def main() -> None:
    """Console entry point: run the Uvicorn server."""
    settings = get_settings()
    uvicorn.run(
        "tapevaultui.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
