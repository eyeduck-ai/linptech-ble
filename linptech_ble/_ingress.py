"""Bounded advertisement handoff between threads; never used for GATT data."""
from __future__ import annotations

import asyncio
from collections import deque
import threading
from typing import Any, Callable


class AdvertisementIngress:
    """Drop oldest advertisements before scheduling one coalesced wakeup.

    ``callback`` runs synchronously on ``loop``. An owner may schedule bounded
    async work there, but this mailbox never creates one task per packet. A
    drain yields after 64 packets so other loop work can proceed during bursts.
    Snapshots contain counters only, with no payloads or callback references.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, callback: Callable[..., Any], *, capacity: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Advertisement ingress capacity must be a positive integer.")
        self.loop, self.callback, self.capacity = loop, callback, capacity
        self._lock = threading.Lock()
        self._queue: deque[tuple[Any, ...]] = deque()
        self._scheduled = False
        self._closed = False
        self._wakeup_failed = False
        self._counts = {key: 0 for key in ("received", "delivered", "dropped", "discarded_on_close", "callback_errors")}

    def _schedule(self, *, threadsafe: bool) -> None:
        # The caller holds _lock. call_soon[_threadsafe] only schedules work;
        # it never executes the callback inline while this lock is held.
        self._scheduled = True
        try:
            if threadsafe:
                self.loop.call_soon_threadsafe(self._drain)
            else:
                self.loop.call_soon(self._drain)
        except RuntimeError:
            self._wakeup_failed = self._closed = True
            self._counts["discarded_on_close"] += len(self._queue)
            self._queue.clear()
            self._scheduled = False

    def put(self, *args: Any) -> None:
        with self._lock:
            if self._closed:
                return
            self._counts["received"] += 1
            if len(self._queue) == self.capacity:
                self._queue.popleft()
                self._counts["dropped"] += 1
            self._queue.append(args)
            if not self._scheduled:
                self._schedule(threadsafe=True)

    def _drain(self) -> None:
        for _ in range(64):
            with self._lock:
                if self._closed or not self._queue:
                    self._scheduled = False
                    return
                args = self._queue.popleft()
            try:
                self.callback(*args)
            except (Exception, asyncio.CancelledError):
                with self._lock:
                    self._counts["callback_errors"] += 1
            finally:
                with self._lock:
                    self._counts["delivered"] += 1
        with self._lock:
            if not self._closed and self._queue:
                self._schedule(threadsafe=False)
            else:
                self._scheduled = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._counts["discarded_on_close"] += len(self._queue)
            self._queue.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"capacity": self.capacity, "queued": len(self._queue), "closed": self._closed,
                    "wakeup_failed": self._wakeup_failed, **self._counts}
