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
from .property_catalog import CATALOG, PAIR_CATALOG, PROPERTIES, Property
from .es5_radar import EmptyRoomScan, REPORT_MASK, REPORT_MINUTES, bitmap_data, duration_reporting, hex_series, radar_bins
from .standard_auth import DirectError, DirectTimeout, PropertyUnavailable, StandardSession, encode_value, uuid16
from .transport import BluetoothConfig, TransportError, connected_client


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
    OPTIONAL_REFRESH_SECONDS = 12
    OPTIONAL_PROPERTY_TIMEOUT = 4

    def __init__(self, device: DeviceConfig | None, *, recording_dir: Path = Path("captures"), on_value: Any = None, on_backup: Any = None, transport: BluetoothConfig | None = None) -> None:
        self.device = device
        self.transport = transport or BluetoothConfig()
        self.client_context: Any = None
        self.recording_dir = recording_dir
        self.on_value = on_value
        self.on_backup = on_backup
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {
            "status": "disconnected", "authenticated": False,
            "token_configured": bool(device and device.auth_token), "firmware": None,
            "error": None, "busy": False, "last_operation": None,
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
        self.client: Any = None
        self.session: StandardSession | None = None
        self.thread: threading.Thread | None = None
        self.log: Any = None
        self.stop_requested = threading.Event()
        self.started = threading.Event()
        self.next_poll = 0.0
        self.poll_index = 0
        self.protocol_frames = 0
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
                    if self.client and not self.client.is_connected:
                        await self._disconnect()
                        self._update(status="disconnected", error="裝置已中斷直連；可單擊按鍵後重新連線。")
                    elif self.session and self.state["authenticated"]:
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
                    if not self.session or not self.client or not self.client.is_connected:
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
        self._update(recording=str(path))
        self.protocol_frames = 0
        self._update(status="connecting", error=None)
        self._diagnostic("phase", phase="connecting")
        self.client_context = connected_client(self.transport, self.device.address, self.device.name)
        self.client = await self.client_context.__aenter__()
        firmware = None
        if self.client.services.get_characteristic(uuid16(4)):
            with suppress(Exception):
                firmware = (await asyncio.wait_for(self.client.read_gatt_char(uuid16(4)), 3)).decode("ascii", errors="replace").rstrip("\0")
        self._update(status="authenticating", firmware=firmware)
        self._diagnostic("phase", phase="authenticating", firmware=firmware)
        self.session = StandardSession(self.client, bytes.fromhex(self.device.auth_token),
            on_property=self._accept, on_frame=self._protocol_frame,
            on_transport=lambda **fields: self._diagnostic("auth_transport", **fields))
        try:
            await asyncio.wait_for(self.session.authenticate(), timeout=35)
        except Exception:
            await self._disconnect()
            raise
        self._update(status="reading", authenticated=True)
        self._diagnostic("phase", phase="authenticated")
        await self._refresh()
        await self._start_reporting()

    def _protocol_frame(self, plaintext: bytes) -> None:
        # Only authenticated BLE Spec responses, never auth characteristics,
        # random challenges, tokens, proofs, session keys, or encrypted auth data.
        self.protocol_frames += 1
        if self.log and self.protocol_frames <= 100:
            self.log.write(json.dumps({"kind": "spec_rx", "at": now_iso(), "hex": plaintext.hex()}) + "\n")
            self.log.flush()

    def _accept(self, result: dict[str, Any]) -> None:
        prop = PAIR_CATALOG.get((result["siid"], result["piid"]))
        if prop is None or result["status"] != 0:
            return
        value = result["value"]
        # Check the declared type even on read; a wrong protocol cannot enable writes.
        code, _ = encode_value(value, prop.format)
        if code != result["type_code"]:
            raise DirectError(f"屬性 {prop.siid}.{prop.piid} 的型別與 ES5BB 規格不符。")
        if prop.writable:
            prop.validate(value, reading=True)
        if (prop.minimum is not None and value < prop.minimum) or (prop.maximum is not None and value > prop.maximum):
            raise DirectError("屬性讀值超出 ES5BB 規格範圍。")
        if prop.allowed_values and value not in prop.allowed_values:
            # Preserve an authenticated unknown enumeration for inspection,
            # without publishing it as a normal duration measurement.
            with self.lock:
                self.state["properties"][prop.name].update(status="invalid_value", value=value,
                    updated_at=now_iso(), error="未定義的持續時間列舉值；保留原始值，不當作有效分鐘數。")
            self._diagnostic("unknown_property_enum", property=prop.name, raw_value=value)
            return
        record = {"name": prop.name, "label": prop.label, "siid": prop.siid, "piid": prop.piid,
                  "value": value, "updated_at": now_iso(), "source": "gatt", "authenticated": True}
        with self.lock:
            self.state["properties"][prop.name].update(status="read", value=value,
                    updated_at=record["updated_at"], error=None)
        if self.log:
            self.log.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.log.flush()
        if self.on_value:
            self.on_value(record)
        clock = self.loop.time() if self.loop else 0
        if prop.name in {"motion_energy", "presence_energy"} and hex_series(value) is not None:
            self.last_energy_clock = clock
            with self.lock:
                if self.state["streaming"]["requested_mode"] == "radar":
                    self.state["streaming"].update(active=True, radar_active=True,
                        mode="radar", updated_at=record["updated_at"], radar_updated_at=record["updated_at"])
        if prop.name == "pir_energy":
            self.last_pir_clock = clock
            with self.lock:
                samples = self.state["pir"]["samples"]
                samples.append({"value": value, "at": record["updated_at"]})
                del samples[:-256]
                if self.state["streaming"]["requested_mode"] == "pir":
                    self.state["streaming"].update(pir_active=True, mode="pir", pir_updated_at=record["updated_at"])
        if self.scan:
            self.scan.accept(prop.name, value, clock)

    async def _read(self, prop: Property) -> Any:
        if self.session is None:
            raise DirectError("請先連線並完成認證。")
        if prop.notification_only:
            raise DirectError("此欄位只接收通知；請啟用對應串流。")
        result = await self.session.read(prop.siid, prop.piid, subscribe=True)
        self._accept(result)
        if self.state["properties"][prop.name]["status"] == "invalid_value":
            raise DirectError("裝置回報未定義的列舉值；已保留原始值，不視為有效讀值。")
        return result["value"]

    async def _refresh(self) -> None:
        if self.session is None:
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
                # Do not start a new wire transaction in the final fraction of
                # the budget. Windows' coarse loop clock can otherwise fire
                # tiny wait_for timers early and issue a burst of doomed reads.
                remaining = budget_end - clock
                minimum_window = min(.25, self.OPTIONAL_PROPERTY_TIMEOUT, self.OPTIONAL_REFRESH_SECONDS / 4)
                if remaining < minimum_window:
                    break
                self.optional_cursor = ([p.name for p in PROPERTIES if p.optional and not p.notification_only].index(prop.name) + 1) % len(optional)
            try:
                if prop.optional:
                    await asyncio.wait_for(self._read(prop), min(self.OPTIONAL_PROPERTY_TIMEOUT, budget_end - asyncio.get_running_loop().time()))
                else:
                    await self._read(prop)
                successes += 1
                failures = 0
            except (DirectError, asyncio.TimeoutError) as error:
                if isinstance(error, asyncio.TimeoutError):
                    error = DirectTimeout("選用屬性本輪讀取逾時；可稍後重新整理。")
                if getattr(self.session, "error", None):
                    raise self.session.error
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
        self._update(status="ready" if successes else "error")
        self._diagnostic("refresh", successful_properties=successes)

    async def _idle(self) -> None:
        assert self.session is not None
        if self.session.error:
            raise self.session.error
        # Firmware answering GET without subscriptions needs periodic reads.
        clock = asyncio.get_running_loop().time()
        if clock - self.last_energy_clock > 8:
            with self.lock:
                self.state["streaming"].update(active=False, radar_active=False)
        if clock - self.last_pir_clock > 8:
            with self.lock:
                self.state["streaming"]["pir_active"] = False
        if clock >= self.next_keepalive:
            await self._read(CATALOG["detect_region"])
            self.next_keepalive = clock + 120
        await self._calibration_tick(clock)
        if self.state["status"] == "ready" and clock >= self.next_poll:
            mode = self.state["streaming"]["requested_mode"]
            names = (("occupancy_status",) if self.scan else
                (("motion_energy", "presence_energy", "human_sensor_bitmaps", "occupancy_status") if mode == "radar"
                else ("human_sensor_bitmaps", "occupancy_status")))
            name = names[self.poll_index % len(names)]
            self.poll_index += 1
            self.next_poll = clock + 2
            prop = CATALOG[name]
            if self.scan or (self.state["properties"][name]["status"] == "read" and self.session.read_ops.get((prop.siid, prop.piid)) == 1):
                await self._read(CATALOG[name])

    async def _start_reporting(self) -> None:
        assert self.session is not None
        # Subscribe even if the initial snapshots are empty. Energy starts only
        # after the official fixed START_REPORT command, not a guessed GET loop.
        self.next_keepalive = asyncio.get_running_loop().time() + 120
        accepted = await self.session.command(1)
        with self.lock:
            self.state["streaming"].update(requested=True, requested_mode="radar",
                mode="radar" if accepted else "unknown", acknowledged=accepted)
        self._diagnostic("report_requested", acknowledged=accepted)

    async def _activate_stream(self, mode: str) -> None:
        assert self.session is not None
        if mode == "pir":
            # Official plugin subscribes prop.6.18 before issuing command 4.
            snapshot = await self.session.subscribe(6, 18)
            if snapshot is not None:
                self._accept(snapshot)
        if mode != "off" and not await self.session.command({"radar": 1, "pir": 4}[mode]):
            raise DirectError("裝置尚未確認新的串流命令；不將畫面標示為即時回報。")
        with self.lock:
            self.state["streaming"].update(mode=mode, requested_mode=mode,
                requested=mode != "off", acknowledged=True, active=False,
                radar_active=False, pir_active=False)
        self.last_energy_clock = self.last_pir_clock = -1e9

    async def _select_stream(self, mode: str) -> None:
        if mode not in ("radar", "pir", "off") or self.session is None:
            raise DirectError("請先連線，再選擇有效串流模式。")
        original = self.state["streaming"]["requested_mode"]
        self._update(status="stream_switching", error=None)
        with self.lock:
            self.state["streaming"].update(active=False, radar_active=False, pir_active=False)
        try:
            # Fixed stop → start sequence; never combine vendor command bits.
            if not await self.session.command(0):
                raise DirectError("裝置未確認停止原串流，未嘗試同時啟用兩種串流。")
            await self._activate_stream(mode)
        except Exception:
            restored = False
            try:
                if await self.session.command(0):
                    await self._activate_stream(original if original in ("radar", "pir") else "off")
                    restored = True
            except Exception:
                pass
            if not restored:
                with self.lock:
                    self.state["streaming"].update(mode="unknown", requested_mode="off", requested=False,
                        active=False, radar_active=False, pir_active=False, acknowledged=False)
            self._diagnostic("stream_switch_failed", requested=mode, previous=original, restored=restored)
            self._update(status="ready" if restored else "error")
            raise
        self._diagnostic("stream_selected", mode=mode)
        self._update(status="ready")

    def _cal_state(self, **values: Any) -> None:
        with self.lock:
            self.state["calibration"].update(values)

    async def _calibration_action(self, action: str) -> None:
        assert self.session is not None
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
            self._cal_state(backup_available=True)
            self._diagnostic("calibration_backup", properties=original)
            if self.on_backup:
                self.on_backup(deepcopy(original))
            self._update(status="writing")
            try:
                for name, value in {**proposed, "radar_motion_sens": 0, "radar_presence_sens": 0}.items():
                    result = await self.session.set(CATALOG[name].siid, CATALOG[name].piid, value, CATALOG[name].format)
                    self._accept(result)
                if not await self.session.command(512):
                    raise DirectError("閾值已送出，但裝置未確認掃描完成命令。")
                # Sensitivity presets may also affect thresholds; verify again.
                for name, value in proposed.items():
                    if await self._read(CATALOG[name]) != value:
                        raise DirectError("校正閾值的最終回讀不一致。")
            except Exception:
                await self._restore_calibration()
                raise
            self._update(status="ready")
            self._cal_state(phase="applied", message="兩組閾值已由裝置回讀確認；仍需走動與離開測試效果。", restore_required=False)
            self.scan = None
            self._diagnostic("calibration_applied")

    async def _restore_calibration(self) -> None:
        assert self.session is not None and self.cal_backup
        try:
            # Restore modes before thresholds, because presets can overwrite them.
            for name in ("radar_motion_sens", "radar_presence_sens", "motion_threshold", "presence_threshold"):
                prop = CATALOG[name]
                result = await self.session.set(prop.siid, prop.piid, self.cal_backup[name], prop.format)
                self._accept(result)
            self._update(status="ready")
        except Exception:
            self._cal_state(phase="error", restore_required=True,
                message="未能確認還原，請重新連線後按還原備份。原值保存在直連紀錄的 calibration_backup。")
            raise DirectError("校正未完成且還原尚未確認；請重新連線後還原備份。") from None

    async def _calibration_tick(self, clock: float) -> None:
        assert self.session is not None
        phase = self.state["calibration"]["phase"]
        if phase == "countdown":
            remaining = max(0, math.ceil(self.cal_deadline - clock))
            self._cal_state(remaining=remaining)
            if remaining:
                return
            if self.cal_kind == "radar_calibrate":
                acknowledged = await self.session.command(256)
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

    async def _set(self, name: str, value: Any) -> None:
        if self.session is None:
            raise DirectError("請先連線並完成認證。")
        prop = CATALOG[name]
        prop.validate(value)
        if name == "pir_threshold" and await self._read(CATALOG["pir_sensitivity"]) != 0:
            raise DirectError("PIR 紅外線靈敏度目前不是自訂（0）；尚未寫入閾值。")
        if prop.allowed_mask is not None and await self._read(prop) & ~prop.allowed_mask:
            raise DirectError("裝置包含未知保留位元，尚未寫入上報門檻，以保留原設定。")
        self._update(status="writing", error=None)
        result = await self.session.set(prop.siid, prop.piid, value, prop.format)
        self._accept(result)
        self._diagnostic("setting_verified", property=name, value=value)
        self._update(status="ready")

    async def _disconnect(self) -> None:
        if self.session:
            with suppress(Exception):
                if self.client and self.client.is_connected:
                    await asyncio.wait_for(self.session.command(0), timeout=4)
            with suppress(Exception):
                await self.session.close()
        if self.client_context:
            with suppress(Exception):
                await self.client_context.__aexit__(None, None, None)
        elif self.client:
            with suppress(Exception):
                await asyncio.wait_for(self.client.disconnect(), timeout=4)
        if self.log:
            self.log.close()
        self.session, self.client, self.log, self.client_context = None, None, None, None
        with self.lock:
            for prop in self.state["properties"].values():
                if prop["status"] == "read":
                    prop["status"] = "cached"
            self.state.update(status="disconnected", authenticated=False)
            self.state["streaming"].update(active=False, requested=False, mode="off", requested_mode="off",
                radar_active=False, pir_active=False)
            if self.state["calibration"]["phase"] in {"countdown", "waiting_empty", "scanning", "radar_wait", "preview"}:
                self.state["calibration"].update(phase="cancelled", message="連線已結束，校正未套用。")
        self.scan, self.cal_kind = None, None
