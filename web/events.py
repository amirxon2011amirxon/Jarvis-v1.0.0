"""Thread-safe event fan-out for the local UI.

The conversation runs on its own asyncio loop and on audio worker threads; the
HTTP server runs on a different loop.  ``EventHub`` is the only bridge between
those worlds.  Publishing is safe from any thread and never blocks the caller:
the event is delivered through ``loop.call_soon_threadsafe`` to each connected
browser's queue.  Slow or stalled browsers therefore cannot stall the voice
session.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Set


class EventHub:
    """Fan out JSON-serialisable events to every open WebSocket."""

    # A browser that stops reading gets its oldest events dropped rather than
    # growing the server's memory without bound.
    QUEUE_LIMIT = 800

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: Set[asyncio.Queue] = set()
        self._backlog: Deque[Dict[str, Any]] = deque(maxlen=100)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop
            self._backlog.clear()

    def detach_loop(self) -> None:
        with self._lock:
            self._loop = None

    # ── subscriptions ────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        """Create a queue for one WebSocket.  Call on the server event loop."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_LIMIT)
        with self._lock:
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(queue)

    # ── publishing ───────────────────────────────────────────────────────────

    def publish(self, event: Dict[str, Any]) -> None:
        """Publish from any thread.  Never raises, never blocks for long."""
        with self._lock:
            loop = self._loop
            queues = list(self._queues)
            if loop is None or not loop.is_running():
                self._backlog.append(event)
                return

        for queue in queues:
            try:
                loop.call_soon_threadsafe(self._deliver, queue, event)
            except Exception:
                # The server is shutting down or the loop closed between the
                # check and the call.  Dropping the event is correct then.
                pass

    def _deliver(self, queue: asyncio.Queue, event: Dict[str, Any]) -> None:
        if queue.full():
            # Keep the most recent state.  Audio levels are the only events
            # produced quickly enough to hit this in normal use.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    # ── test / diagnostics ───────────────────────────────────────────────────

    def client_count(self) -> int:
        with self._lock:
            return len(self._queues)

    @staticmethod
    def now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
