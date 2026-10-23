"""Local dashboard inventory and a bounded view of public BLE advertisements."""
from __future__ import annotations

from collections import OrderedDict
from math import isfinite
from pathlib import Path
import threading
import time
from typing import Any, Callable

from .config import DeviceConfig, normalize_address, save_config
from .models import AdvertisementRecord, mibeacon_header
from .standard_auth import DirectError


class NearbyDiscovery:
    """Observe the existing scanner, without recording other devices' payloads."""

    def __init__(self, configured: Callable[[], set[str]], *, clock=time.monotonic) -> None:
        self.configured, self.clock = configured, clock
        self.lock = threading.Lock()
        self.until = 0.0
        self.items: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def command(self, action: str) -> dict[str, Any]:
        with self.lock:
            if action == "start":
                self.items.clear()
                self.until = self.clock() + 60
            elif action == "stop":
                self.until = 0
            else:
                raise ValueError("掃描操作必須為 start 或 stop。")
        return self.snapshot()

    def observe(self, record: AdvertisementRecord) -> None:
        with self.lock:
            if self.clock() >= self.until:
                return
            try:
                address = normalize_address(record.address)
                header = mibeacon_header(record)
            except (ValueError, TypeError):
                return
            if header is None:
                return
            rssi = record.rssi
            valid_rssi = type(rssi) in {int, float} and -127 < rssi < 0 and isfinite(rssi)
            self.items[address] = {
                "address": address, "name": (record.name or "")[:120],
                "model": header.get("model"), "product_id": header.get("product_id"),
                "rssi": rssi if valid_rssi else None, "last_seen": record.timestamp,
                "receiver": record.receiver, "supported": bool(header.get("model")),
            }
            self.items.move_to_end(address)
            while len(self.items) > 256:
                self.items.popitem(last=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            configured = self.configured()
            remaining = max(0, self.until - self.clock())
            return {"status": "scanning" if remaining else "stopped", "remaining_seconds": round(remaining),
                    "devices": [{**item, "configured": address in configured}
                                for address, item in self.items.items()], "error": None}


class DashboardInventory:
    """Validate and persist mutations before exposing fresh decoder/controller state."""

    def __init__(self, state, devices: list[DeviceConfig], path: str | Path, *,
                 controller_factory: Callable[[DeviceConfig], Any],
                 new_telemetry: Callable[[DeviceConfig], dict[str, Any]],
                 on_controllers_changed: Callable[[], None] | None = None) -> None:
        self.state, self.devices, self.path = state, devices, Path(path)
        self.controller_factory, self.new_telemetry = controller_factory, new_telemetry
        self.on_controllers_changed = on_controllers_changed
        self.lock = threading.RLock()
        self.discovery = NearbyDiscovery(lambda: {device.address for device in self.devices})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"enabled": self.state.mode == "live", "devices": [
                {"address": item.address, "model": item.model, "name": item.name,
                 "key_configured": bool(item.bindkey), "token_configured": bool(item.auth_token),
                 "miot_model": item.miot_model} for item in self.devices]}

    def source(self) -> list[DeviceConfig]:
        # List replacement is atomic. Capture refreshes only when identity changes.
        return self.devices

    def submit_auto(self, controller, command: dict[str, Any]) -> str | None:
        """Skip a busy inventory rather than deadlocking its auto-thread stop."""
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if (self.state.mode != "live" or controller.device.model != "ES5BB"
                    or self.state.direct_devices.get(controller.device.address) is not controller):
                return None
            status = controller.snapshot()
            if (status.get("busy") or status.get("authenticated") or not status.get("token_configured")
                    or (status.get("last_operation") or {}).get("action") == "disconnect"):
                return None
            return controller.submit(command)
        finally:
            self.lock.release()

    def mutate(self, command: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.state.mode != "live":
                raise DirectError("離線重播模式不能修改裝置。")
            allowed = {"action", "address", "model", "name", "bindkey", "auth_token", "miot_model"}
            if not isinstance(command, dict) or not command.keys() <= allowed:
                raise ValueError("裝置設定欄位不正確。")
            action = command.get("action")
            if action not in {"add", "update", "remove"}:
                raise ValueError("裝置操作必須為 add、update 或 remove。")
            address = normalize_address(command.get("address"))
            old = next((item for item in self.devices if item.address == address), None)
            if action == "add" and old is not None:
                raise DirectError("此裝置已加入；請使用編輯功能。")
            if action != "add" and old is None:
                raise DirectError("找不到此裝置；請重新整理裝置清單。")
            if action == "remove" and command.keys() != {"action", "address"}:
                raise ValueError("移除操作只接受裝置位址。")
            candidate = None
            if action != "remove":
                model = command.get("model", old.model if old else None)
                if old is not None and model != old.model:
                    raise ValueError("已加入的裝置不能變更型號；請移除後重新加入。")
                name = command.get("name", old.name if old else model)
                if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    raise ValueError("名稱需為 1～120 個字元，且不可包含控制字元。")
                credentials = {}
                for key in ("bindkey", "auth_token"):
                    supplied = command.get(key, "")
                    if not isinstance(supplied, str):
                        raise ValueError("憑證需為十六進位字串；留空會保留目前設定。")
                    credentials[key] = supplied.strip() or (getattr(old, key) if old else None)
                candidate = DeviceConfig(address, model, name.strip(), **credentials,
                    miot_model=command.get("miot_model", old.miot_model if old else None))
            changed_credentials = old is None or candidate is None or (
                candidate.bindkey != old.bindkey or candidate.auth_token != old.auth_token
                or candidate.miot_model != old.miot_model)
            controller = self.state.direct_devices.get(address)
            if changed_credentials and controller is not None:
                status = controller.snapshot()
                if (status.get("busy") or status.get("authenticated") or status.get("connected")
                        or status.get("status") not in {None, "disconnected", "unavailable"}
                        or status.get("calibration", {}).get("restore_required")):
                    raise DirectError("請先停止操作並中斷直連；若有待還原校正，請先完成還原。")
            replacement = self.controller_factory(candidate) if candidate and changed_credentials else None
            updated = [item for item in self.devices if item.address != address]
            if candidate is not None:
                if old is not None:
                    updated = [candidate if item.address == address else item for item in self.devices]
                else:
                    updated.append(candidate)
            try:
                save_config(self.path, updated)
            except OSError:
                raise DirectError("無法儲存本機設定檔；裝置清單尚未變更。") from None
            if changed_credentials and controller is not None:
                controller.stop()
            with self.state.lock:
                if candidate is None:
                    self.state.devices.pop(address, None)
                    self.state.direct_devices.pop(address, None)
                    self.state.events = type(self.state.events)(
                        (event for event in self.state.events if event["address"] != address), maxlen=300)
                else:
                    telemetry = self.state.devices.get(address)
                    if telemetry is None:
                        telemetry = self.new_telemetry(candidate)
                        self.state.devices[address] = telemetry
                    telemetry.update(name=candidate.name, key_configured=bool(candidate.bindkey),
                                     token_configured=bool(candidate.auth_token))
                    if old and candidate.bindkey != old.bindkey:
                        telemetry.update(verified=False, last_error=None)
                    if replacement is not None:
                        self.state.direct_devices[address] = replacement
                    elif controller is not None:
                        controller.device = candidate
                    for event in self.state.events:
                        if event["address"] == address:
                            event["name"] = candidate.name
                self.devices = updated
                self.state.direct = None
                self.state.sensor_direct = {}
                for device in updated:
                    current = self.state.direct_devices.get(device.address)
                    if device.model == "ES5BB" and self.state.direct is None:
                        self.state.direct = current
                    elif device.model != "ES5BB" and current is not None:
                        self.state.sensor_direct.setdefault(device.model, current)
            if self.on_controllers_changed:
                self.on_controllers_changed()
            return self.snapshot()
