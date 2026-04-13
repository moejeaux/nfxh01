"""Async event bus — in-process asyncio queues with typed publish/subscribe.

Upgrade path: swap queue backend for Redis Streams without changing interface.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

logger = logging.getLogger(__name__)

EventHandler = Callable[[BaseModel], Awaitable[None]]


class EventBus:
    """Lightweight async event bus backed by asyncio queues.

    Supports multiple handlers per event type.
    All handlers for a given event run concurrently.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._queue: asyncio.Queue[tuple[str, BaseModel]] = asyncio.Queue()
        self._running = False
        self._processed_count = 0
        self._error_count = 0

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)
        logger.debug("Subscribed %s to %s", handler.__qualname__, event_type)

    async def publish(self, event_type: str, event: BaseModel) -> None:
        await self._queue.put((event_type, event))
        logger.debug("Published %s (queue size=%d)", event_type, self._queue.qsize())

    def publish_sync(self, event_type: str, event: BaseModel) -> None:
        """Non-async publish for use in sync contexts (fire-and-forget)."""
        try:
            self._queue.put_nowait((event_type, event))
        except asyncio.QueueFull:
            logger.warning("Event bus queue full — dropping %s", event_type)

    async def start(self) -> None:
        """Start the dispatch loop. Call as an asyncio task."""
        self._running = True
        logger.info("EventBus started — %d event types registered", len(self._handlers))
        while self._running:
            try:
                event_type, event = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue

            handlers = self._handlers.get(event_type, [])
            if not handlers:
                logger.debug("No handlers for %s — event discarded", event_type)
                continue

            tasks = [
                asyncio.create_task(self._safe_call(h, event, event_type))
                for h in handlers
            ]
            await asyncio.gather(*tasks)
            self._processed_count += 1

    async def _safe_call(
        self, handler: EventHandler, event: BaseModel, event_type: str
    ) -> None:
        try:
            await handler(event)
        except Exception as e:
            self._error_count += 1
            logger.error(
                "Handler %s failed for %s: %s",
                handler.__qualname__, event_type, e, exc_info=True,
            )

    async def stop(self) -> None:
        self._running = False
        logger.info(
            "EventBus stopped — processed=%d errors=%d",
            self._processed_count, self._error_count,
        )

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "queue_size": self._queue.qsize(),
            "event_types": list(self._handlers.keys()),
            "processed": self._processed_count,
            "errors": self._error_count,
        }
