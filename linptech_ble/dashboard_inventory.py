"""Local dashboard inventory and a bounded view of public BLE advertisements."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict
import hashlib
import json
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
        self._revisions: dict[str, int] = {}
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

    def _credential_fingerprint(self, address: str) -> str:
        item = next((device for device in self.devices if device.address == address), None)
        value = [self._revisions.get(address, 0), asdict(item) if item else None]
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def credential_snapshot(self, addresses: list[str]) -> dict[str, str]:
        """Internal optimistic-concurrency tokens; never send these to a browser."""
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 256:
            raise ValueError("請選取 1～256 個裝置。")
        normalized = [normalize_address(address) for address in addresses]
        if len(set(normalized)) != len(normalized):
            raise ValueError("裝置位址不得重複。")
        with self.lock:
            return {address: self._credential_fingerprint(address) for address in normalized}

    def merge_credentials(self, rows: list[dict[str, Any]], *, expected: dict[str, str]) -> dict[str, Any]:
        """Apply only selected cloud credentials in one validated disk transaction.

        Capture ``expected`` before performing network requests. Names already
        configured locally and missing credentials remain untouched. The batch
        either validates in full and saves once, or changes nothing.
        """
        if not isinstance(rows, list) or not 1 <= len(rows) <= 256 or not isinstance(expected, dict):
            raise ValueError("請選取有效的裝置憑證。")
        with self.lock:
            if self.state.mode != "live":
                raise DirectError("離線重播模式不能修改裝置。")
            existing = {device.address: device for device in self.devices}
            candidates: dict[str, DeviceConfig] = {}
            allowed = {"address", "model", "name", "miot_model", "bindkey", "auth_token"}
            for row in rows:
                if not isinstance(row, dict) or not row.keys() <= allowed:
                    raise ValueError("雲端裝置資料格式不正確。")
                address = normalize_address(row.get("address"))
                if address in candidates:
                    raise ValueError("裝置位址不得重複。")
                old = existing.get(address)
                model = row.get("model")
                if old is not None and old.model != model:
                    raise DirectError("雲端型號與本機裝置不符；未儲存任何變更。")
                name = old.name if old else row.get("name") or model
                if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    raise ValueError("裝置名稱格式不正確。")
                credentials = {}
                for key in ("bindkey", "auth_token"):
                    supplied = row.get(key)
                    if supplied is not None and not isinstance(supplied, str):
                        raise ValueError("雲端憑證格式不正確。")
                    credentials[key] = supplied.strip() if supplied and supplied.strip() else getattr(old, key, None)
                if old is None and not any(credentials.values()):
                    raise ValueError("新裝置沒有可用憑證；未儲存任何變更。")
                candidates[address] = DeviceConfig(address, model, name, **credentials,
                    miot_model=row.get("miot_model") or getattr(old, "miot_model", None))
            if set(expected) != set(candidates):
                raise ValueError("憑證儲存快照與所選裝置不符。")
            if any(expected[address] != self._credential_fingerprint(address) for address in candidates):
                raise DirectError("查詢期間裝置設定已變更；請重新選取裝置並查詢憑證。")
            updated = [candidates.get(device.address, device) for device in self.devices]
            updated.extend(device for address, device in candidates.items() if address not in existing)
            self._commit_devices(updated)
            public = {device["address"]: device for device in self.snapshot()["devices"]}
            return {"devices": [public[address] for address in candidates]}

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
            updated = [item for item in self.devices if item.address != address]
            if candidate is not None:
                if old is not None:
                    updated = [candidate if item.address == address else item for item in self.devices]
                else:
                    updated.append(candidate)
            self._commit_devices(updated)
            return self.snapshot()

    def _commit_devices(self, updated: list[DeviceConfig]) -> None:
        """Called with the inventory lock held; validate all targets before I/O."""
        if updated == self.devices:
            return
        old_devices = {device.address: device for device in self.devices}
        new_devices = {device.address: device for device in updated}
        changed = [address for address in dict.fromkeys([*old_devices, *new_devices])
                   if old_devices.get(address) != new_devices.get(address)]
        replacements, retired = {}, []
        for address in changed:
            old, candidate = old_devices.get(address), new_devices.get(address)
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
                retired.append(controller)
            if candidate is not None and changed_credentials:
                replacements[address] = self.controller_factory(candidate)
        try:
            save_config(self.path, updated)
        except OSError:
            raise DirectError("無法儲存本機設定檔；裝置清單尚未變更。") from None
        for controller in retired:
            controller.stop()
        with self.state.lock:
            for address in changed:
                old, candidate = old_devices.get(address), new_devices.get(address)
                controller = self.state.direct_devices.get(address)
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
                    if address in replacements:
                        self.state.direct_devices[address] = replacements[address]
                    elif controller is not None:
                        controller.device = candidate
                    for event in self.state.events:
                        if event["address"] == address:
                            event["name"] = candidate.name
                self._revisions[address] = self._revisions.get(address, 0) + 1
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
