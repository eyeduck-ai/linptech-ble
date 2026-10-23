"""Opt-in ES5 connection retries, using the same guarded controller as the UI."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .standard_auth import DirectError


class AutoConnector:
    def __init__(self, controller: Any, *, retry_seconds: float = 30, ready: Callable[[], bool] = lambda: True,
                 submit: Callable[[dict[str, Any]], str | None] | None = None) -> None:
        self.controller, self.retry_seconds, self.ready = controller, retry_seconds, ready
        self.submit_command = submit
        self.next_attempt = float("-inf")
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.stopping.is_set() or now < self.next_attempt or not self.ready():
            return
        state = self.controller.snapshot()
        if not state.get("token_configured") or state.get("authenticated") or state.get("busy"):
            return
        # A deliberate disconnect suspends auto-connect until the next
        # explicit connect (or a service restart). Never fight the user's action.
        if (state.get("last_operation") or {}).get("action") == "disconnect":
            return
        try:
            operation = (self.submit_command or self.controller.submit)({"action": "connect"})
        except (DirectError, OSError, RuntimeError):
            # Operation errors are already represented by the controller. In
            # particular no transport exception text or credentials are logged.
            self.next_attempt = now + self.retry_seconds
        else:
            # A guarded dashboard callback returns None when inventory is busy
            # or this controller was replaced. Retry later without blocking a
            # mutation that is stopping this connector's thread.
            if self.submit_command is None or operation is not None:
                self.next_attempt = now + self.retry_seconds

    def _run(self) -> None:
        while not self.stopping.is_set():
            self.tick()
            self.stopping.wait(1)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="es5-auto-connect")
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)
