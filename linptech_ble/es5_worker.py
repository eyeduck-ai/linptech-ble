"""Process boundary for WinRT calls that can block inside native Close()."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import multiprocessing
import threading
import time
from typing import Any

from .es5_direct import DirectController, now_iso
from .standard_auth import DirectError


def _worker_main(device, recording_dir, pipe, backup, restore_required, transport=None) -> None:
    controller_type = DirectController
    if device is not None and device.model != "ES5BB":
        from .sensor_direct import SensorDirectController
        controller_type = SensorDirectController
    controller = controller_type(device, recording_dir=recording_dir,
        transport=transport,
        on_value=lambda record: pipe.send(("value", record)),
        on_backup=lambda backup: pipe.send(("backup", backup)))
    controller.cal_backup = backup
    if backup:
        controller._cal_state(backup_available=True, restore_required=restore_required,
            message="已保留前一工作程序的閾值備份；請連線後確認或還原。")

    async def run():
        task = asyncio.create_task(controller._run())
        await asyncio.sleep(0)

        def receive():
            try:
                while not controller.stop_requested.is_set():
                    if not pipe.poll(.5):
                        continue
                    kind, value = pipe.recv()
                    if kind == "stop":
                        controller.stop_requested.set()
                        controller.loop.call_soon_threadsafe(task.cancel)
                        return
                    if kind == "command":
                        with controller.lock:
                            controller.state.update(busy=True, last_operation={"id": value["id"],
                                "action": value["action"], "status": "pending", "at": now_iso()})
                        controller.loop.call_soon_threadsafe(controller.commands.put_nowait, value)
            except (EOFError, OSError, ValueError):
                controller.stop_requested.set()
                with suppress(RuntimeError):
                    controller.loop.call_soon_threadsafe(task.cancel)

        threading.Thread(target=receive, daemon=True, name="es5-worker-input").start()
        try:
            while not task.done():
                pipe.send(("state", (controller.snapshot(), controller.cal_backup)))
                await asyncio.sleep(.5)
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            pipe.close()

    asyncio.run(run())


class ProcessDirectController(DirectController):
    """Keep HTTP/scanning responsive even if a WinRT native call never returns."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = multiprocessing.get_context("spawn")
        self.worker = None
        self.pipe = None
        self.ipc_lock = threading.Lock()
        self.pending_command: dict[str, Any] | None = None
        self.command_at = 0.0
        self.heartbeat = 0.0
        self.generation = 0
        self.defer_start = False
        self._startup_lock = threading.Lock()

    def _spawn(self):
        parent, child = self.context.Pipe()
        self.pipe = parent
        self.worker = self.context.Process(target=_worker_main,
            args=(self.device, self.recording_dir, child, self.cal_backup,
                  bool(self.state["calibration"].get("restore_required")), self.transport), daemon=True, name="es5-ble-worker")
        try:
            self.worker.start()
        except BaseException:
            parent.close()
            self.pipe = None
            self.worker = None
            raise
        finally:
            child.close()
        self.heartbeat = time.monotonic()
        self.generation += 1

    def start(self):
        if self.defer_start:
            return
        self._start_now()

    def _start_now(self):
        if self.loop is not None or self.stop_requested.is_set():
            return
        if self.transport.backend == "esphome":
            # Native API sends GATT replies to the advertisement subscriber.
            # Both consumers must share the same process-local API broker.
            return DirectController.start(self)
        self._spawn()
        # Reuse the control allowlist and fresh-read validation in the parent.
        class QueueProxy:
            def put_nowait(_, command):
                self.pending_command = command
                self.command_at = time.monotonic()
                try:
                    with self.ipc_lock:
                        self.pipe.send(("command", command))
                except OSError:
                    self.pending_command = None
                    self._update(busy=False)
                    raise DirectError("直連程序正在重新啟動，請稍候重試。") from None
        class LoopProxy:
            def call_soon_threadsafe(_, fn, *args):
                fn(*args)
        self.commands, self.loop = QueueProxy(), LoopProxy()
        self.thread = threading.Thread(target=self._monitor, daemon=True, name="es5-worker-monitor")
        self.thread.start()

    def submit(self, value):
        # A large dashboard should not allocate one idle native subprocess per
        # configured sensor. Only an explicit connect (or opt-in auto-connect)
        # starts its controller; the default worker API remains eager.
        if (self.defer_start and value.get("action") == "connect" and self.device and self.device.auth_token
                and getattr(self, "catalog", True)):
            with self._startup_lock:
                self._start_now()
        return super().submit(value)

    def _monitor(self):
        while not self.stop_requested.is_set():
            try:
                if self.pipe.poll(.25):
                    kind, value = self.pipe.recv()
                    self.heartbeat = time.monotonic()
                    if kind == "value":
                        if self.on_value:
                            self.on_value(value)
                    elif kind == "backup":
                        with self.lock:
                            self.cal_backup = deepcopy(value)
                            self.state["calibration"].update(backup_available=True, restore_required=True)
                    elif kind == "state":
                        state, backup = value
                        with self.lock:
                            if self.generation > 1 and not self.pending_command and state["status"] == "disconnected" and self.state["error"]:
                                continue
                            self.cal_backup = backup
                            if self.pending_command:
                                operation = state.get("last_operation")
                                if operation and operation["id"] == self.pending_command["id"] and operation["status"] != "pending":
                                    self.pending_command = None
                                else:
                                    state["busy"] = True
                                    state["last_operation"] = self.state["last_operation"]
                            state["worker_restarts"] = self.generation - 1
                            self.state = state
                if self.stop_requested.is_set():
                    break
                stalled = time.monotonic() - self.heartbeat > 12
                expired = self.pending_command and time.monotonic() - self.command_at > 150
                if not self.worker.is_alive() or stalled or expired:
                    self._restart()
            except (EOFError, OSError):
                if not self.stop_requested.is_set():
                    self._restart()

    def _retire(self):
        if self.worker and self.worker.pid is not None:
            if self.worker.is_alive():
                self.worker.terminate()
            self.worker.join(timeout=2)
            if self.worker.is_alive():
                self.worker.kill()
                self.worker.join(timeout=1)
        if self.pipe:
            self.pipe.close()

    def _restart(self):
        model = self.device.model if self.device else "裝置"
        message = f"藍牙直連作業未回應，已重新啟動直連程序；請再按連接 {model}。"
        with self.lock, self.ipc_lock:
            restore_required = self.state["calibration"].get("restore_required") or (
                self.pending_command and self.pending_command["action"] in {"calibration_apply", "calibration_restore"})
            self._retire()
            for prop in self.state["properties"].values():
                if prop["status"] == "read":
                    prop["status"] = "cached"
            operation = self.state.get("last_operation")
            if operation and operation["status"] == "pending":
                operation.update(status="error", error=message, at=now_iso())
            self.pending_command = None
            self.state.update(status="error", authenticated=False, busy=False, error=message)
            self.state["streaming"].update(active=False, requested=False)
            if self.cal_backup and restore_required:
                self.state["calibration"].update(restore_required=True, backup_available=True)
            if self.state["calibration"]["phase"] in {"countdown", "waiting_empty", "scanning", "radar_wait", "preview"}:
                self.state["calibration"].update(phase="cancelled", message="Windows 直連程序已重啟，掃描取消；尚未自動套用建議閾值。")
            self._spawn()
        # Keep the failure visible until the next deliberate user operation.
        self.state["worker_restarts"] = self.generation - 1

    def stop(self):
        if self.transport.backend == "esphome":
            return DirectController.stop(self)
        self.stop_requested.set()
        with self.ipc_lock, suppress(OSError):
            if self.pipe:
                self.pipe.send(("stop", None))
        if self.thread and self.thread.ident is not None:
            self.thread.join(timeout=2)
        if self.worker and self.worker.pid is not None:
            self.worker.join(timeout=3)
        with self.ipc_lock:
            self._retire()
        self.loop, self.commands = None, None


# The worker boundary is shared; each model retains its own property allowlist.
from .sensor_direct import SensorDirectController


class ProcessSensorController(ProcessDirectController, SensorDirectController):
    pass
