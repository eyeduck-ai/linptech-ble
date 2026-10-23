"""Public, bounded advertisement stream without files or application state."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from copy import deepcopy
from typing import Any

from .config import normalize_address
from .models import AdvertisementRecord
from .transport import BluetoothConfig, TransportError, scanner_context

_END = object()


class AdvertisementScanner(AsyncIterator[AdvertisementRecord]):
    """Receive local or proxy advertisements in one explicit async context.

    Construction performs no I/O. Each instance has one context lifetime and
    belongs to the loop that enters it. Packets are not deduplicated: every
    receiver's RSSI remains available to the caller. Reuse a caller-owned
    ``Decoder`` to suppress duplicate sensor events across those receivers.

    The bounded queue drops its oldest packet on overflow. ``health()`` exposes
    counters; this stream is not a lossless packet recorder. Closing discards
    buffered records and wakes a waiting iterator. Cancelling one ``__anext__``
    call leaves the context open; context exit releases the scanner ownership.
    """

    def __init__(self, *, transport: BluetoothConfig | None = None,
                 addresses: Iterable[str] | None = None, queue_size: int = 1024) -> None:
        if transport is not None and not isinstance(transport, BluetoothConfig):
            raise TypeError("transport must be a BluetoothConfig or None.")
        if type(queue_size) is not int or not 1 <= queue_size <= 65536:
            raise ValueError("queue_size must be between 1 and 65536.")
        if isinstance(addresses, (str, bytes)):
            raise TypeError("addresses must be an iterable of MAC addresses, not a string.")
        self.transport = transport or BluetoothConfig()
        self.addresses = frozenset(normalize_address(value) for value in addresses) if addresses is not None else None
        self.queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._context: Any = None
        self._scanner: Any = None
        self._entered = False
        self._used = False
        self._ended = False
        self._closed = False
        self._failure: TransportError | None = None
        self._failure_raised = False
        self._next_active = False
        self._health_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._opening_task: asyncio.Task | None = None
        self._close_caller: asyncio.Task | None = None
        self._closing = False
        self._counts = {key: 0 for key in ("received", "selected", "yielded", "dropped", "invalid", "discarded_on_close")}

    def _check_loop(self) -> None:
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("Use this scanner on the event loop that entered it.")

    def _receive(self, device: Any, advertisement: Any) -> None:
        if self._ended:
            return
        try:
            record = AdvertisementRecord.from_dict(AdvertisementRecord.from_bleak(device, advertisement).to_dict())
        except (ValueError, TypeError, AttributeError, OverflowError):
            self._counts["invalid"] += 1
            return
        self._counts["received"] += 1
        if self.addresses is not None and record.address not in self.addresses:
            return
        self._counts["selected"] += 1
        assert self._queue is not None
        if self._queue.full():
            self._queue.get_nowait()
            self._counts["dropped"] += 1
        self._queue.put_nowait(record)

    def _callback(self, device: Any, advertisement: Any) -> None:
        if self._ended or self._loop is None:
            return
        try:
            same_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            self._receive(device, advertisement)
        else:
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._receive, device, advertisement)

    def _finish(self) -> None:
        if self._ended:
            return
        self._ended = True
        if self._queue is not None:
            while not self._queue.empty():
                self._queue.get_nowait()
                self._counts["discarded_on_close"] += 1
            self._queue.put_nowait(_END)

    async def _watch_health(self) -> None:
        while not self._ended:
            try:
                if hasattr(self._scanner, "check_health"):
                    self._scanner.check_health()
            except Exception as error:
                self._failure = error if isinstance(error, TransportError) else TransportError("Bluetooth receiver health check failed.")
                self._finish()
                return
            await asyncio.sleep(1)

    async def __aenter__(self) -> AdvertisementScanner:
        if self._used:
            raise RuntimeError("Create a new AdvertisementScanner for each context.")
        self._used = True
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(self.queue_size)
        self._context = scanner_context(self.transport, self._callback)
        self._opening_task = asyncio.current_task()
        try:
            self._scanner = await self._context.__aenter__()
            self._entered = True
            if self._closing:
                # A backend may complete entry while reacting to cancellation.
                # The external close owns exit; never revive a closed stream.
                raise asyncio.CancelledError()
            self._health_task = asyncio.create_task(self._watch_health(), name="linptech-advertisement-health")
            return self
        except BaseException as error:
            self._finish()
            if self._entered and not self._closing:
                await self.aclose()
            elif not self._entered:
                self._closed = True
            if isinstance(error, (asyncio.CancelledError, TransportError)):
                raise
            raise TransportError(f"Bluetooth scanning could not start ({type(error).__name__}).") from None
        finally:
            self._opening_task = None

    def __aiter__(self) -> AdvertisementScanner:
        return self

    async def __anext__(self) -> AdvertisementRecord:
        self._check_loop()
        if self._queue is None:
            raise RuntimeError("Enter AdvertisementScanner with async with before iterating.")
        if self._ended:
            if self._failure is not None and not self._failure_raised:
                self._failure_raised = True
                raise self._failure
            raise StopAsyncIteration
        if self._next_active:
            raise RuntimeError("Only one iterator may consume a scanner at a time.")
        self._next_active = True
        try:
            value = await self._queue.get()
        finally:
            self._next_active = False
        if value is _END:
            if self._failure is not None and not self._failure_raised:
                self._failure_raised = True
                raise self._failure
            raise StopAsyncIteration
        self._counts["yielded"] += 1
        return value

    def health(self) -> dict[str, Any]:
        """Return receiver health and queue counters, without keys or payloads."""
        transport = {"backend": self.transport.backend, "connected": self._entered and not self._ended}
        if self._scanner is not None and hasattr(self._scanner, "health"):
            try:
                transport = self._scanner.health()
            except Exception:
                transport = {"backend": self.transport.backend, "connected": False}
        return deepcopy({"listening": self._entered and not self._ended,
            "closed": self._closed, "queue_size": self.queue_size,
            "queued": 0 if self._ended or self._queue is None else self._queue.qsize(),
            "counts": self._counts, "transport": transport,
            "error": str(self._failure) if self._failure else None})

    async def _close(self) -> None:
        self._finish()
        opening = self._opening_task
        if opening is not None and opening is not self._close_caller:
            # Failed __aenter__ cleanup belongs to the backend context. Wait
            # for it before discarding the context or reporting a closed scan.
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
        if self._health_task is not None:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
        try:
            if self._entered:
                async with asyncio.timeout(self.transport.connect_timeout + 10):
                    await self._context.__aexit__(None, None, None)
        except Exception as error:
            self._failure = error if isinstance(error, TransportError) else TransportError("Bluetooth scanner cleanup did not complete.")
            raise self._failure from None
        finally:
            self._entered = False
            self._closed = True
            self._scanner = self._context = None

    async def aclose(self) -> None:
        """Stop this scanner; cancellation does not cancel its cleanup task."""
        if self._loop is None:
            self._used = self._ended = self._closed = True
            return
        self._check_loop()
        if self._close_task is None:
            self._closing = True
            self._close_caller = asyncio.current_task()
            self._close_task = asyncio.create_task(self._close(), name="linptech-advertisement-close")
        cancelled = None
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as error:
                cancelled = error
        if cancelled is not None:
            # Finish bounded native/API cleanup before propagating cancellation,
            # including when context exit is the last action of asyncio.run().
            with suppress(Exception):
                self._close_task.result()
            raise cancelled
        self._close_task.result()

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()
