"""
File:   sse_manager.py
Brief:  Server-Sent Events fan-out manager (ported from OledWebUI).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.1.1
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

CLIENT_QUEUE_SIZE = 100
PING_INTERVAL_S = 25.0


class SseManager:
    """Fans out events to all connected SSE clients.

    Each client gets a bounded queue; a slow client drops events (logged)
    instead of blocking publishers.
    """

    def __init__(self) -> None:
        """Initialize with no connected clients."""
        self._queues: list[asyncio.Queue[str]] = []

    async def subscribe(self) -> AsyncIterator[str]:
        """Yield SSE-formatted messages for one connected client.

        Yields:
            Strings in ``event: <type>\\ndata: <json>\\n\\n`` format, plus
            keepalive ping comments.

        Emits:
            None.
        """
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self._queues.append(queue)
        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        queue.get(), timeout=PING_INTERVAL_S
                    )
                    yield message
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._queues.remove(queue)

    def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        """Send an event to every connected client.

        Args:
            event_type: SSE event name, e.g. ``drive``.
            payload: JSON-serializable event payload.
        """
        message = f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
        for queue in self._queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("sse_client_queue_full", event=event_type)


# Module-level singleton, shared by the event-bus bridge and the SSE endpoint.
sse_manager = SseManager()
