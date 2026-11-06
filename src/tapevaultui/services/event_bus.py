"""
File:   event_bus.py
Brief:  Async in-process pub/sub bus (ported from OledWebUI).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    """Minimal async publish/subscribe bus.

    Handlers are awaited fire-and-forget: a failing handler is logged and
    never breaks the publisher. Each app instance owns its own bus.
    """

    def __init__(self) -> None:
        """Initialize with an empty topic registry."""
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        """Register a handler for a topic.

        Args:
            topic: Topic name, e.g. ``drive`` or ``job``.
            handler: Async callable receiving the payload dict.
        """
        self._handlers[topic].append(handler)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish an event to all handlers of a topic.

        Args:
            topic: Topic name.
            payload: Event payload (JSON-serializable dict).
        """
        for handler in self._handlers[topic]:
            try:
                await handler(payload)
            except Exception:
                logger.exception("event_handler_failed", topic=topic)

    def publish_soon(self, topic: str, payload: dict[str, Any]) -> None:
        """Schedule a publish as a background task.

        Args:
            topic: Topic name.
            payload: Event payload (JSON-serializable dict).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("publish_soon_no_loop", topic=topic)
            return
        task = loop.create_task(self.publish(topic, payload))
        task.add_done_callback(self._log_task_failure)

    @staticmethod
    def _log_task_failure(task: asyncio.Task[None]) -> None:
        """Log an unexpected failure of a scheduled publish task.

        Args:
            task: The finished publish task.
        """
        if not task.cancelled() and task.exception() is not None:
            logger.error("event_publish_failed", error=str(task.exception()))
