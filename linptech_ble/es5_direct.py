"""ES5BB direct controls, separate from the passive advertisement monitor."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from .config import DeviceConfig
from .client import LinptechClient, PropertyValue
from .property_catalog import CATALOG, PAIR_CATALOG, PROPERTIES, Property
from .es5_radar import EmptyRoomScan, REPORT_MASK, REPORT_MINUTES, bitmap_data, duration_reporting, hex_series, radar_bins
from .standard_auth import DirectError, PropertyUnavailable
from .transport import BluetoothConfig, TransportError


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()



def numeric_array(value: Any) -> list[float] | None:
    """Official ES5 hex CSV, with self-describing arrays for older recordings."""
    if (series := hex_series(value)) is not None:
        return series
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        return None
    if not all(type(v) in (int, float) and math.isfinite(v) for v in value):
        return None
    return [float(v) for v in value]


class DirectController:
    """Dashboard orchestration over LinptechClient; no BLE wire ownership.

    The application owns UI state, polling, the empty-room workflow and durable
    backups. Authentication, typed observations, verified writes, stream
    transactions and GATT cleanup belong to the public library client.
    """
    OPTIONAL_REFRESH_SECONDS = 12

    def __init__(self, device: DeviceConfig | None, *, recording_dir: Path = Path("captures"), on_value: Any = None, on_backup: Any = None, transport: BluetoothConfig | None = None) -> None:
        self.device = device
        self.transport = transport or BluetoothConfig()
        self.recording_dir = recording_dir
        self.on_value = on_value
        self.on_backup = on_backup
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {
            "status": "disconnected", "authenticated": False,
            "token_configured": bool(device and device.auth_token), "firmware": None,
            "error": None, "busy": False, "last_operation": None,
            "client_diagnostics": None,
            "experimental": True, "diagnostics": [], "recording": None,
            "properties": {p.name: {"label": p.label, "siid": p.siid, "piid": p.piid,
                "format": p.format, "writable": p.writable, "minimum": p.minimum, "maximum": p.maximum,
                "choices": list(p.choices), "optional": p.optional, "notification_only": p.notification_only,
                "allowed_mask": p.allowed_mask, "allowed_values": list(p.allowed_values),
                "status": "unread", "value": None, "updated_at": None}
                for p in PROPERTIES},
            "calibration": {"available": False, "phase": "idle", "message": "連線並取得即時能量後可開始。"},
            "distance_mapping_verified": True, "bin_meters": .25, "bin_count": 32,
            # active/updated_at retain their historical radar-only meaning.
            "streaming": {"requested": False, "active": False, "updated_at": None,
                "mode": "off", "requested_mode": "off", "acknowledged": None,
                "radar_active": False, "radar_updated_at": None,
                "pir_active": False, "pir_updated_at": None},
            "pir": {"samples": [], "unit": "raw", "minimum": -1000, "maximum": 1000},
            "transport": self.transport.public_info(),
        }
        self.loop: asyncio.AbstractEventLoop | None = None
        self.commands: asyncio.Queue | None = None
        self.task: asyncio.Task | None = None
        self.client: LinptechClient | None = None
        self._observations: dict[str, PropertyValue] = {}
        self.thread: threading.Thread | None = None
        self.log: Any = None
        self.stop_requested = threading.Event()
        self.started = threading.Event()
        self.next_poll = 0.0
        self.poll_index = 0
        self.next_keepalive = 0.0
        self.last_energy_clock = -1e9
        self.last_pir_clock = -1e9
        self.optional_cursor = 0
        self.scan: EmptyRoomScan | None = None
        self.cal_deadline = 0.0
        self.cal_kind: str | None = None
        self.cal_backup: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = deepcopy(self.state)
        for name in ("motion_energy", "presence_energy", "motion_threshold", "presence_threshold"):
            result["properties"][name]["series"] = numeric_array(result["properties"][name]["value"])
        result["bitmaps"] = bitmap_data(result["properties"]["human_sensor_bitmaps"]["value"])
        result["regions"] = {name: radar_bins(result["properties"][name]["value"])
                             for name in ("detect_region", "interest_region")}
        result["reporting"] = {name: duration_reporting(result["properties"][name]["value"])
            for name in ("no_one_report_mask", "someone_report_mask")}
        result.setdefault("pir", {"samples": []}).update(
            value=result["properties"]["pir_energy"]["value"],
            updated_at=result["properties"]["pir_energy"]["updated_at"],
            threshold=result["properties"]["pir_threshold"]["value"],
            active=result["streaming"].get("pir_active", False),
            trigger=(result["bitmaps"] or {}).get("pir"),
            unit="raw", minimum=-1000, maximum=1000)
        result["calibration"]["available"] = (result["authenticated"] and result["status"] == "ready"
            and result["streaming"]["active"] and all(result["properties"][n]["status"] == "read"
                for n in ("motion_energy", "presence_energy", "motion_threshold", "presence_threshold", "occupancy_status", "radar_motion_sens", "radar_presence_sens")))
        return result

    def _update(self, **values: Any) -> None:
        with self.lock:
            self.state.update(values)

    def _diagnostic(self, kind: str, **values: Any) -> None:
        record = {"kind": kind, "at": now_iso(), **values}
        with self.lock:
            self.state["diagnostics"] = [record, *self.state["diagnostics"][:19]]
        if self.log:
            self.log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            self.log.flush()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True, name="es5-direct")
        self.thread.start()
        self.started.wait(timeout=2)

    def stop(self) -> None:
        self.stop_requested.set()
        if self.loop and self.task:
            with suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread and self.thread.ident is not None:
            self.thread.join(timeout=8)

    def submit(self, value: dict[str, Any]) -> str:
        if self.device is None:
            raise DirectError("設定檔中沒有 ES5BB。")
        action = value.get("action")
        if action not in {"connect", "disconnect", "refresh", "set", "stream", "calibrate", "radar_calibrate", "calibration_cancel", "calibration_apply", "calibration_restore"}:
            raise DirectError("不支援此直連操作。")
        if self.state["calibration"].get("restore_required") and action not in {"connect", "disconnect", "refresh", "calibration_restore"}:
            raise DirectError("請先確認並還原上一輪校正備份。")
        if action == "connect" and not self.device.auth_token:
            raise DirectError("尚未設定 ES5BB 的 12-byte 直連 token。")
        if action == "stream":
            if value.get("mode") not in ("radar", "pir", "off"):
                raise DirectError("串流模式只支援 radar、pir 或 off。")
            if not self.state["authenticated"] or self.state["status"] != "ready":
                raise DirectError("請先連線並完成認證，再切換串流。")
            if self.state["calibration"]["phase"] == "preview" and value["mode"] != "radar":
                raise DirectError("請先取消空房掃描預覽，再切換 PIR 串流。")
        if action == "set":
            prop = CATALOG.get(value.get("property"))
            if prop is None:
                raise DirectError("不支援此 ES5BB 屬性。")
            prop.validate(value.get("value"))
            with self.lock:
                if not self.state["authenticated"] or self.state["status"] != "ready":
                    raise DirectError("請先連線、完成認證與屬性讀取。")
                if self.state["properties"][prop.name]["status"] != "read":
                    raise DirectError("必須先成功讀取此屬性，才能送出設定。")
                if prop.allowed_mask is not None and self.state["properties"][prop.name]["value"] & ~prop.allowed_mask:
                    raise DirectError("裝置包含未知保留位元，尚未開放修改此欄位，以保留原設定。")
                if prop.name == "pir_threshold" and not (
                    self.state["properties"]["pir_sensitivity"]["status"] == "read"
                    and self.state["properties"]["pir_sensitivity"]["value"] == 0):
                    raise DirectError("請先讀取並將 PIR 紅外線靈敏度設為自訂（0），再調整閾值。")
        if action in {"calibrate", "radar_calibrate", "calibration_apply", "calibration_restore"}:
            state = self.snapshot()
            if not state["authenticated"] or state["status"] != "ready":
                raise DirectError("請先連線並讀取裝置。")
            if action in {"calibrate", "radar_calibrate"} and value.get("empty_room") is not True:
                raise DirectError("請先確認裝置已固定，並準備離開感應範圍。")
            if action == "calibrate" and not state["calibration"]["available"]:
                raise DirectError("請等候兩種能量、閾值與存在狀態的即時資料。")
            if action in {"calibrate", "radar_calibrate"} and state["streaming"].get("requested_mode") != "radar":
                raise DirectError("請先恢復雷達串流，再進行雷達校正。")
            if action == "calibration_apply" and state["calibration"]["phase"] != "preview":
                raise DirectError("請先完成空房掃描並檢視閾值建議。")
            if action == "calibration_restore" and not self.cal_backup:
                raise DirectError("沒有待還原的閾值備份。")
        with self.lock:
            active = self.state["calibration"]["phase"] in {"countdown", "waiting_empty", "scanning", "radar_wait"}
            if active and action not in {"calibration_cancel", "disconnect"}:
                raise DirectError("校正正在進行；請先取消，或等待掃描結束。")
        with self.lock:
            if self.state["busy"]:
                raise DirectError("另一個直連操作正在進行，請稍候。")
            if self.loop is None or self.commands is None:
                raise DirectError("直連工作程序尚未就緒。")
            operation_id = uuid4().hex[:12]
            self.state["busy"] = True
            self.state["last_operation"] = {"id": operation_id, "action": action, "status": "pending", "at": now_iso()}
            self.loop.call_soon_threadsafe(self.commands.put_nowait, {**value, "id": operation_id})
            return operation_id

    async def _run(self) -> None:
        self.loop, self.task, self.commands = asyncio.get_running_loop(), asyncio.current_task(), asyncio.Queue()
        self.started.set()
        try:
            while not self.stop_requested.is_set():
                try:
                    command = await asyncio.wait_for(self.commands.get(), timeout=1)
                except asyncio.TimeoutError:
                    if self.client and not self.client.connected:
                        await self._disconnect()
                        self._update(status="disconnected", error="裝置已中斷直連；可單擊按鍵後重新連線。")
                    elif self.client and self.state["authenticated"]:
                        try:
                            await self._idle()
                        except DirectError as error:
                            self._diagnostic("transport_error", message=str(error))
                            await self._disconnect()
                            self._update(status="error", error=str(error))
                    continue
                try:
                    if command["action"] == "connect":
                        await self._connect()
                    elif command["action"] == "disconnect":
                        await self._disconnect()
                    elif command["action"] == "refresh":
                        await self._refresh()
                    elif command["action"] == "set":
                        await self._set(command["property"], command["value"])
                    elif command["action"] == "stream":
                        await self._select_stream(command["mode"])
                    else:
                        await self._calibration_action(command["action"])
                    self._operation(command, "done")
                except Exception as error:
                    phase = self.snapshot()["status"]
                    public = str(error) if isinstance(error, (DirectError, TransportError)) else f"直連在 {phase} 階段失敗（{type(error).__name__}）；確認供電、距離並單擊按鍵後重試。"
                    self._diagnostic("operation_error", action=command["action"], phase=phase, message=public)
                    self._update(error=public)
                    self._operation(command, "error", public)
                    if not self.client or not self.client.connected:
                        await self._disconnect()
                        self._update(status="error", error=public)
                    elif command["action"] != "stream" or self.state["status"] != "ready":
                        self._update(status="error")
        except asyncio.CancelledError:
            pass
        finally:
            await self._disconnect()
            self.loop, self.commands, self.task = None, None, None

    def _operation(self, command: dict, status: str, error: str | None = None) -> None:
        with self.lock:
            self.state["busy"] = False
            self.state["last_operation"] = {"id": command["id"], "action": command["action"],
                "status": status, "at": now_iso(), "error": error}


    async def _connect(self) -> None:
        await self._disconnect()
        assert self.device and self.device.auth_token
        self.recording_dir.mkdir(parents=True, exist_ok=True)
        path = self.recording_dir / f"es5-direct-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.jsonl"
        self.log = path.open("x", encoding="utf-8")
        self._update(recording=str(path), status="connecting", error=None, firmware=None)
        try:
            sdk = LinptechClient(self.device, transport=self.transport)
            # Public callbacks are queued. Ignore obsolete observations and
            # those belonging to a client the application already replaced.
            sdk.on_update = lambda value: self._accept(value) if (
                self.client is sdk and sdk.get_property(value.name) is value) else None
            self.client = sdk
            self._diagnostic("phase", phase="connecting", api="LinptechClient")
            await sdk.connect()
            self._update(status="reading", authenticated=True)
            await self._refresh()
            await self._start_reporting()
        except BaseException:
            await self._disconnect()
            raise

    def _accept(self, observation: PropertyValue) -> None:
        """Map an SDK observation to UI state; never decode or validate wire data."""
        if not isinstance(observation, PropertyValue):
            raise TypeError("Dashboard observations must be PropertyValue instances.")
        prop = observation.definition
        if prop.name not in self.state["properties"]:
            return
        with self.lock:
            row = self.state["properties"][prop.name]
            if self._observations.get(prop.name) is observation:
                return
            if row["updated_at"] and observation.updated_at and observation.updated_at < row["updated_at"]:
                return
            self._observations[prop.name] = observation
            row.update(status=observation.status, value=observation.value,
                updated_at=observation.updated_at, error=observation.error)
            if prop.name == "firmware_revision" and observation.valid:
                self.state["firmware"] = observation.value
        if not observation.valid:
            return
        record = {"name": prop.name, "label": prop.label, "siid": prop.siid, "piid": prop.piid,
                  "value": observation.value, "updated_at": observation.updated_at,
                  "source": observation.source, "authenticated": True}
        if self.log:
            self.log.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.log.flush()
        if self.on_value:
            self.on_value(record)
        # The typed public observation distinguishes updates from snapshots,
        # including packets received within the same Windows clock tick.
        live = observation.operation == 4
        if self.client:
            self._update(client_diagnostics=self.client.diagnostics())
        clock = self.loop.time() if self.loop else 0
        if live and prop.name in {"motion_energy", "presence_energy"} and hex_series(observation.value) is not None:
            self.last_energy_clock = clock
            with self.lock:
                if self.state["streaming"]["requested_mode"] == "radar":
                    self.state["streaming"].update(active=True, radar_active=True,
                        mode="radar", updated_at=observation.updated_at, radar_updated_at=observation.updated_at)
        if live and prop.name == "pir_energy":
            self.last_pir_clock = clock
            with self.lock:
                samples = self.state["pir"]["samples"]
                samples.append({"value": observation.value, "at": observation.updated_at})
                del samples[:-256]
                if self.state["streaming"]["requested_mode"] == "pir":
                    self.state["streaming"].update(pir_active=True, mode="pir", pir_updated_at=observation.updated_at)
        if self.scan:
            self.scan.accept(prop.name, observation.value, clock)


    async def _read(self, prop: Property) -> Any:
        if self.client is None:
            raise DirectError("請先連線並完成認證。")
        try:
            observed = await self.client.read(prop.name)
        except DirectError:
            self._accept(self.client.get_property(prop.name))
            raise
        self._accept(observed)
        return observed.value

    async def _refresh(self) -> None:
        if self.client is None:
            raise DirectError("請先連線並完成認證。")
        self._update(status="reading", error=None)
        failures = successes = 0
        optional = [p for p in PROPERTIES if p.optional and not p.notification_only]
        optional = optional[self.optional_cursor:] + optional[:self.optional_cursor]
        core = [p for p in PROPERTIES if not p.optional and not p.notification_only]
        budget_end = None
        for prop in core + optional:
            if prop.optional:
                clock = asyncio.get_running_loop().time()
                if budget_end is None:
                    budget_end = clock + self.OPTIONAL_REFRESH_SECONDS
                # Budget applies between complete SDK transactions. The SDK
                # owns operation timeouts and closes on cancellation; an outer
                # wait_for must not cancel a read and then claim it survived.
                remaining = budget_end - clock
                if remaining <= 0:
                    break
                self.optional_cursor = ([p.name for p in PROPERTIES if p.optional and not p.notification_only].index(prop.name) + 1) % len(optional)
            try:
                await self._read(prop)
                successes += 1
                failures = 0
            except (DirectError, asyncio.TimeoutError) as error:
                if not self.client.connected:
                    raise DirectError("讀取未完成，SDK 已結束直連；請重新連線。") from None
                if isinstance(error, PropertyUnavailable):
                    failures = 0
                    with self.lock:
                        self.state["properties"][prop.name].update(status="unavailable", checked_at=now_iso(), error=str(error))
                    continue
                failures = failures + 1 if not prop.optional else 0
                self._diagnostic("property_error", property=prop.name, message=str(error))
                with self.lock:
                    invalid = self.state["properties"][prop.name]["status"] == "invalid_value"
                    self.state["properties"][prop.name].update(status="invalid_value" if invalid else "error", error=str(error))
                if failures >= 3:
                    raise DirectError("連續三個屬性未能讀取；目前韌體的 BLE Spec 格式仍需實機封包確認。") from None
        self._update(status="ready" if successes else "error", client_diagnostics=self.client.diagnostics())
        self._diagnostic("refresh", successful_properties=successes)


    async def _idle(self) -> None:
        assert self.client is not None
        if not self.client.connected:
            raise DirectError("裝置已中斷直連。")
        self._update(client_diagnostics=self.client.diagnostics())
        clock = asyncio.get_running_loop().time()
        if clock - self.last_energy_clock > 8:
            with self.lock:
                self.state["streaming"].update(active=False, radar_active=False)
        if clock - self.last_pir_clock > 8:
            with self.lock:
                self.state["streaming"]["pir_active"] = False
        if clock >= self.next_keepalive:
            self._accept(await self.client.keepalive())
            self.next_keepalive = clock + 120
        await self._calibration_tick(clock)
        if self.state["status"] == "ready" and clock >= self.next_poll:
            mode = self.state["streaming"]["requested_mode"]
            names = (("occupancy_status",) if self.scan else
                (("motion_energy", "presence_energy", "human_sensor_bitmaps", "occupancy_status")
                 if mode == "radar" else ("occupancy_status",)))
            name = names[self.poll_index % len(names)]
            self.poll_index += 1
            self.next_poll = clock + 2
            row = self.state["properties"][name]
            age = ((datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
                   if row["updated_at"] else float("inf"))
            # Explicit, bounded application polling replaces private read-op
            # negotiation inspection. Healthy push data does not need polling.
            if self.scan or (row["status"] == "read" and age > 8):
                try:
                    await self._read(CATALOG[name])
                except PropertyUnavailable:
                    self._diagnostic("poll_unavailable", property=name)

    async def _start_reporting(self) -> None:
        self.next_keepalive = asyncio.get_running_loop().time() + 120
        await self._select_stream("radar")

    def _stream_state(self, mode: str) -> None:
        with self.lock:
            self.state["streaming"].update(mode=mode, requested_mode=mode if mode != "unknown" else "off",
                requested=mode in ("radar", "pir"), acknowledged=mode != "unknown", active=False,
                radar_active=False, pir_active=False)
        self.last_energy_clock = self.last_pir_clock = -1e9

    async def _select_stream(self, mode: str) -> None:
        if mode not in ("radar", "pir", "off") or self.client is None:
            raise DirectError("請先連線，再選擇有效串流模式。")
        original = self.client.stream_mode
        self._update(status="stream_switching", error=None)
        self._stream_state("unknown")
        try:
            await self.client.set_stream(mode)
        except Exception:
            # The SDK owns stop/start and rollback. Reflect its actual ACK state.
            restored = self.client.connected and self.client.stream_mode in ("off", "radar", "pir")
            self._stream_state(self.client.stream_mode if restored else "unknown")
            self._diagnostic("stream_switch_failed", requested=mode, previous=original, restored=restored)
            self._update(status="ready" if restored else "error")
            raise
        self._stream_state(self.client.stream_mode)
        self._diagnostic("stream_selected", mode=mode)
        self._update(status="ready", client_diagnostics=self.client.diagnostics())

    def _cal_state(self, **values: Any) -> None:
        with self.lock:
            self.state["calibration"].update(values)

    async def _calibration_action(self, action: str) -> None:
        assert self.client is not None
        if action == "calibration_cancel":
            self.scan, self.cal_kind = None, None
            self._cal_state(phase="cancelled", message="已取消；空房掃描不會自動修改閾值。")
            return
        if action in {"calibrate", "radar_calibrate"}:
            self.cal_kind = action
            self.cal_deadline = asyncio.get_running_loop().time() + 10
            self.scan = None
            self._cal_state(phase="countdown", remaining=10, elapsed=0, proposed=None,
                message="請在 10 秒內離開全部感應區段，保持感測器固定。")
            return
        if action == "calibration_restore":
            await self._restore_calibration()
            self._cal_state(phase="restored", message="備份閾值與靈敏度已回讀確認。", restore_required=False)
            return
        if action == "calibration_apply":
            proposed = deepcopy(self.state["calibration"]["proposed"])
            assert self.scan is not None
            original = self.scan.original
            for name, value in original.items():
                if await self._read(CATALOG[name]) != value:
                    raise DirectError("掃描後設定已變動；請重新掃描，保留目前設定。")
            self.cal_backup = deepcopy(original)
            self._cal_state(backup_available=True, restore_required=True)
            self._diagnostic("calibration_backup", properties=original)
            if self.on_backup:
                self.on_backup(deepcopy(original))
            self._update(status="writing")
            try:
                for name, value in {**proposed, "radar_motion_sens": 0, "radar_presence_sens": 0}.items():
                    self._accept(await self.client.set(name, value))
                if not await self.client.commit_radar_thresholds(empty_room=True):
                    raise DirectError("閾值已送出，但裝置未確認掃描完成命令。")
                # Sensitivity presets may also affect thresholds; verify again.
                for name, value in proposed.items():
                    if await self._read(CATALOG[name]) != value:
                        raise DirectError("校正閾值的最終回讀不一致。")
            except BaseException as error:
                if isinstance(error, Exception):
                    await self._restore_calibration()
                else:
                    # Cancellation may follow a persistent write and closes
                    # the SDK session. Keep the backup instead of claiming an
                    # unconfirmed rollback on a connection already released.
                    self._cal_state(phase="error", restore_required=True,
                        message="套用已中止，原設定備份已保留；請重新連線並還原確認。")
                raise
            self._update(status="ready")
            self._cal_state(phase="applied", message="兩組閾值已由裝置回讀確認；仍需走動與離開測試效果。", restore_required=False)
            self.scan = None
            self._diagnostic("calibration_applied")

    async def _restore_calibration(self) -> None:
        assert self.client is not None and self.cal_backup
        self._cal_state(restore_required=True)
        try:
            # Restore modes before thresholds, because presets can overwrite them.
            for name in ("radar_motion_sens", "radar_presence_sens", "motion_threshold", "presence_threshold"):
                self._accept(await self.client.set(name, self.cal_backup[name]))
            # Later preset writes may affect earlier values: verify the full
            # restored group independently after every write has completed.
            for name, value in self.cal_backup.items():
                if await self._read(CATALOG[name]) != value:
                    raise DirectError("備份的最終回讀不一致。")
            self._update(status="ready")
            self._cal_state(restore_required=False)
        except BaseException as error:
            self._cal_state(phase="error", restore_required=True,
                message="未能確認還原，請重新連線後按還原備份。原值保存在直連紀錄的 calibration_backup。")
            if not isinstance(error, Exception):
                raise
            raise DirectError("校正未完成且還原尚未確認；請重新連線後還原備份。") from None

    async def _calibration_tick(self, clock: float) -> None:
        assert self.client is not None
        phase = self.state["calibration"]["phase"]
        if phase == "countdown":
            remaining = max(0, math.ceil(self.cal_deadline - clock))
            self._cal_state(remaining=remaining)
            if remaining:
                return
            if self.cal_kind == "radar_calibrate":
                acknowledged = await self.client.radar_calibrate(empty_room=True)
                self.cal_deadline = clock + 20
                self._cal_state(phase="radar_wait", remaining=20,
                    message="已送出雷達重新校正命令，請保持空房至少 20 秒。裝置未提供完成旗標。", acknowledged=acknowledged)
                self._diagnostic("radar_calibration_requested", acknowledged=acknowledged)
                return
            names = ("motion_threshold", "presence_threshold", "radar_motion_sens", "radar_presence_sens")
            original = {name: await self._read(CATALOG[name]) for name in names}
            if any(hex_series(original[n]) is None for n in names[:2]):
                raise DirectError("未取得完整 32 格閾值，已停止掃描。")
            self.scan = EmptyRoomScan(original, clock)
            self._cal_state(phase="waiting_empty", message="等待裝置回報無人後，收集連續 30 秒空房能量。")
        elif phase == "radar_wait":
            remaining = max(0, math.ceil(self.cal_deadline - clock))
            self._cal_state(remaining=remaining)
            if not remaining:
                self._cal_state(phase="command_sent", message="空房等候時間已結束。命令已送出；請觀察底噪與有人／無人變化驗證，這不是裝置完成回報。")
        elif self.scan and phase in {"waiting_empty", "scanning"}:
            if clock - self.scan.started > 180:
                self.scan = None
                self._cal_state(phase="error", message="3 分鐘內未取得連續空房資料；閾值未變更，請排除持續有人或斷流後重試。")
                return
            progress = self.scan.progress(clock)
            if progress["ready"]:
                self._cal_state(phase="preview", proposed=self.scan.proposals(), **progress,
                    message="掃描完成。建議值只會提高既有閾值以抑制干擾；檢視後按套用才會寫入。")
            else:
                self._cal_state(phase="scanning" if self.scan.since is not None else "waiting_empty", **progress,
                    message="正在收集空房能量；出現有人或資料中斷時，30 秒計時會重新開始。")


    def _sync_client_property(self, client: LinptechClient, name: str) -> None:
        """Synchronize failed-operation cache without waiting for queued callbacks."""
        if self.client is not client:
            return
        # Preserve the primary failure even if an application callback or log
        # fails while publishing the SDK's authoritative cached observation.
        with suppress(Exception):
            self._accept(client.get_property(name))
        with suppress(Exception):
            self._update(client_diagnostics=client.diagnostics())

    async def _set(self, name: str, value: Any) -> None:
        client = self.client
        if client is None:
            raise DirectError("請先連線並完成認證。")
        self._update(status="writing", error=None)
        try:
            observed = await client.set(name, value)
        except BaseException:
            self._sync_client_property(client, name)
            raise
        if self.client is not client:
            raise DirectError("直連已變更，請重新讀取。")
        self._accept(observed)
        self._diagnostic("setting_verified", property=name, value=value)
        self._update(status="ready", client_diagnostics=client.diagnostics())

    async def _disconnect(self) -> None:
        sdk, self.client = self.client, None
        try:
            if sdk:
                await sdk.aclose()
        finally:
            if sdk:
                self._update(client_diagnostics=sdk.diagnostics())
            if self.log:
                self.log.close()
            self.log = None
            with self.lock:
                self._observations.clear()
                for prop in self.state["properties"].values():
                    if prop["status"] == "read":
                        prop["status"] = "cached"
                self.state.update(status="disconnected", authenticated=False)
                self.state["streaming"].update(active=False, requested=False, mode="off", requested_mode="off",
                    radar_active=False, pir_active=False)
                if self.state["calibration"].get("restore_required"):
                    self.state["calibration"].update(phase="error",
                        message="連線已結束，原設定還原尚未確認；請重新連線並還原備份。")
                elif self.state["calibration"]["phase"] in {"countdown", "waiting_empty", "scanning", "radar_wait", "preview"}:
                    self.state["calibration"].update(phase="cancelled", message="連線已結束，校正未套用。")
            self.scan, self.cal_kind = None, None
