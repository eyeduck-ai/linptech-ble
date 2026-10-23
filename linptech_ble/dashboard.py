"""Loopback-only sensor dashboard. Bindkeys stay in the Python process."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
import webbrowser

from .config import DeviceConfig, load_config
from .decoder import Decoder
from .models import AdvertisementRecord
from .runtime import capture
from .es5_direct import DirectController
from .es5_worker import ProcessDirectController, ProcessSensorController
from .standard_auth import DirectError
from .bridge_config import load_bridge_config, public_transport
from .transport import BluetoothConfig
from .ha_mqtt import HomeAssistantBridge
from .auto_connect import AutoConnector

STATIC = Path(__file__).with_name("web")
MODEL_ORDER = {model: i for i, model in enumerate(("ES5BB", "MS1BB", "KS2BB", "HS3BB"))}
LABELS = {
    "occupancy": "人體存在", "occupancy_close_range": "近距區間",
    "motion": "移動偵測", "door": "門窗", "opening": "門窗",
    "temperature": "溫度", "humidity": "濕度", "illuminance": "照度", "battery": "電量",
    "door_left_open": "門未關警示", "device_forcibly_removed": "防拆警示",
    "door_stuck": "門卡住", "knock_on_the_door": "敲門偵測", "pry_the_door": "撬門警示",
    "no_motion_duration": "未移動持續時間", "custom_no_motion_time": "自訂無人時間",
    "duration_detected": "有人持續時間", "duration_cleared": "無人持續時間",
}
BUTTONS = {"press": "單擊", "double_press": "雙擊", "long_press": "長按"}
DEVICE_EVENTS = {"device_reset": "裝置重設", "off_light_leave": "關燈後離開", "off_light_exit": "關燈後退出",
                 "off_light_sleep": "關燈後睡眠", "off_light_exit_4": "關燈後退出（事件值 4）",
                 "radar_noise_minor": "雷達輕微干擾", "radar_noise_critical": "雷達嚴重干擾",
                 "radar_noise_normal": "雷達恢復正常", "radar_noise_error": "雷達錯誤"}
ERRORS = {
    "decryption_failed": "解密失敗，請核對 bindkey 或裝置是否重新綁定。",
    "encrypted_no_key": "尚未設定 bindkey。",
    "malformed": "收到格式不完整的廣播。",
    "parse_error": "此封廣播無法解析。",
    "unsupported_model": "尚未支援此產品識別碼。",
    "model_mismatch": "設定的型號與廣播識別碼不一致。",
    "unsupported_protocol": "尚未支援此廣播協定。",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def age_seconds(timestamp: str | None, now: datetime) -> float | None:
    if timestamp is None:
        return None
    then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, (now - then).total_seconds())


def state_text(key: str, value: bool) -> str:
    if key in {"occupancy", "occupancy_close_range"}:
        return "有人" if value else "無人"
    if key == "motion":
        return "偵測到移動" if value else "未偵測到移動"
    if key in {"door", "opening"}:
        return "開啟" if value else "關閉"
    return "觸發" if value else "未觸發"


class DashboardState:
    """Merge packet-local values without inventing values for missing fields."""

    def __init__(self, devices: list[DeviceConfig], *, mode: str = "live", stale_after: float = 180) -> None:
        self.lock = threading.RLock()
        self.mode = mode
        self.started_at = utc_now()
        self.stale_after = stale_after
        self.scanner_status = "starting"
        self.scanner_error: str | None = None
        self.statuses: Counter[str] = Counter()
        self.events: deque[dict[str, Any]] = deque(maxlen=300)
        self.event_count = 0
        self.paths: dict[str, str] = {}
        self.direct: DirectController | None = None
        self.sensor_direct: dict[str, ProcessSensorController] = {}
        self.transport = public_transport(BluetoothConfig())
        self.mqtt: HomeAssistantBridge | None = None
        self.devices = {
            device.address: {
                "address": device.address, "model": device.model, "name": device.name,
                "key_configured": bool(device.bindkey), "verified": False,
                "last_seen": None, "last_decoded": None, "last_status": None,
                "last_error": None, "rssi": None, "packets": 0, "decoded": 0,
                "duplicates": 0, "errors": 0, "measurements": {}, "binary_sensors": {},
                "last_event": None, "history": {}, "objects": {},
            }
            for device in sorted(devices, key=lambda item: MODEL_ORDER.get(item.model, 99))
        }

    def set_scanner(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.scanner_status, self.scanner_error = status, error

    def _event(self, device: dict[str, Any], timestamp: str, kind: str, key: str, text: str) -> None:
        self.event_count += 1
        self.events.appendleft({
            "id": self.event_count, "timestamp": timestamp, "address": device["address"],
            "model": device["model"], "name": device["name"], "kind": kind, "key": key, "text": text,
        })

    def ingest(self, packet: dict[str, Any]) -> None:
        # Copy only the documented decoded fields. Never serialize a DeviceConfig.
        with self.lock:
            device = self.devices.get(packet.get("address"))
            if device is None:
                return
            timestamp = packet["timestamp"]
            status = packet.get("status", "parse_error")
            previous_status = device["last_status"]
            device.update(last_seen=timestamp, rssi=packet.get("rssi"), last_status=status)
            device["packets"] += 1
            self.statuses[status] += 1
            if status in ERRORS:
                device["errors"] += 1
                device["last_error"] = ERRORS[status]
                if previous_status != status:
                    self._event(device, timestamp, "error", status, ERRORS[status])
                return
            device["verified"] = device["verified"] or bool(packet.get("bindkey_verified"))
            if status == "duplicate":
                device["duplicates"] += 1
                return
            for item in packet.get("objects", []):
                identity = item.get("object_id_hex")
                if isinstance(identity, str) and (len(device["objects"]) < 64 or identity in device["objects"]):
                    device["objects"][identity] = {**deepcopy(item), "updated_at": timestamp}
            if status != "decoded":
                return
            device["decoded"] += 1
            device["last_decoded"] = timestamp
            device["last_error"] = None
            device["verified"] = device["verified"] or bool(packet.get("bindkey_verified"))
            for collection in ("measurements", "binary_sensors"):
                for entry in packet.get(collection, []):
                    key = str(entry["key"])
                    # These four devices currently use the default device_id.
                    # Keep a suffix if future packets report multiple channels.
                    channel = entry.get("device_id")
                    identity = key if channel is None else f"{key}:{channel}"
                    old = device[collection].get(identity)
                    value = entry.get("value")
                    unit = entry.get("unit")
                    device[collection][identity] = {
                        "key": key, "value": value, "unit": unit,
                        "device_id": channel, "updated_at": timestamp,
                        "source": "advertisement",
                    }
                    changed = old is None or old["value"] != value
                    label = LABELS.get(key, key)
                    if collection == "binary_sensors":
                        if changed:
                            self._event(device, timestamp, "state", key, f"{label} · {state_text(key, bool(value))}")
                    else:
                        if isinstance(value, (float, int)) and not isinstance(value, bool):
                            points = device["history"].setdefault(identity, deque(maxlen=180))
                            points.append({"timestamp": timestamp, "value": value})
                        if changed:
                            self._event(device, timestamp, "measurement", key, f"{label} · {value}{' ' + unit if unit else ''}")
            for event in packet.get("events", []):
                event_type = str(event.get("event_type", "unknown"))
                device["last_event"] = {
                    "key": str(event.get("key", "button")), "event_type": event_type,
                    "label": BUTTONS.get(event_type, DEVICE_EVENTS.get(event_type, event_type)), "timestamp": timestamp,
                }
                is_button = event_type in BUTTONS and str(event.get("key", "button")) == "button"
                label = BUTTONS.get(event_type, DEVICE_EVENTS.get(event_type, event_type))
                self._event(device, timestamp, "button" if is_button else "device", event_type,
                            f"{'按鍵' if is_button else '裝置事件'} · {label}")

    def ingest_direct(self, record: dict[str, Any]) -> None:
        mapping = {"occupancy_status": ("binary_sensors", "occupancy", None),
                   "occupancy_close_range": ("binary_sensors", "occupancy_close_range", None),
                   "illuminance": ("measurements", "illuminance", "lx"),
                   "battery_level": ("measurements", "battery", "%")}
        if record["name"] not in mapping:
            return
        with self.lock:
            device = next((item for item in self.devices.values() if item["model"] == "ES5BB"), None)
            if device is None:
                return
            collection, key, unit = mapping[record["name"]]
            value = bool(record["value"]) if collection == "binary_sensors" else record["value"]
            old = device[collection].get(key)
            device[collection][key] = {"key": key, "value": value, "unit": unit, "device_id": None,
                                       "updated_at": record["updated_at"], "source": "gatt"}
            if old is None or old["value"] != value:
                text = state_text(key, value) if collection == "binary_sensors" else f"{value} {unit}"
                self._event(device, record["updated_at"], "state" if collection == "binary_sensors" else "measurement",
                            key, f"{LABELS[key]} · {text}（直連）")

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        with self.lock:
            devices = []
            for device in self.devices.values():
                public = deepcopy({key: value for key, value in device.items() if key != "history"})
                public["history"] = {key: list(points) for key, points in device["history"].items()}
                for entry in public["binary_sensors"].values():
                    entry["age_seconds"] = age_seconds(entry["updated_at"], now)
                    entry["freshness"] = "recent" if entry["age_seconds"] <= self.stale_after else "stale"
                age = age_seconds(device["last_seen"], now)
                public["age_seconds"] = age
                public["freshness"] = "waiting" if age is None else ("recent" if age <= self.stale_after else "stale")
                devices.append(public)
            return {
                "version": 1, "mode": self.mode, "started_at": self.started_at,
                "server_time": now.isoformat(), "stale_after": self.stale_after,
                "scanner": {"status": self.scanner_status, "error": self.scanner_error},
                "totals": {"packets": sum(self.statuses.values()), "statuses": dict(self.statuses),
                           "events": self.event_count, "verified": sum(d["verified"] for d in devices)},
                "devices": devices, "events": list(self.events),
                "recording": dict(self.paths),
                "direct": self.direct.snapshot() if self.direct else None,
                "sensor_direct": {model: controller.snapshot() for model, controller in self.sensor_direct.items()},
                "transport": dict(self.transport),
                "mqtt": self.mqtt.status() if self.mqtt else {"enabled": False, "status": "disabled"},
            }


class MonitorWorker:
    def __init__(self, state: DashboardState, devices: list[DeviceConfig], *, replay_path: Path | None = None,
                 transport: BluetoothConfig | None = None, retry_seconds: float = 5) -> None:
        self.state, self.devices, self.replay_path = state, devices, replay_path
        self.transport = transport or BluetoothConfig()
        self.retry_seconds = retry_seconds
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="linptech-ble", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        loop, task = self.loop, self.task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker may have finished between checking and cancelling.
        self.thread.join(timeout=8)

    def _run(self) -> None:
        asyncio.run(self._monitor())

    async def _monitor(self) -> None:
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        if self.stopping.is_set():
            self.state.set_scanner("stopped")
            return
        try:
            if self.replay_path is not None:
                self.state.set_scanner("replaying")
                decoder = Decoder(self.devices)
                with self.replay_path.open(encoding="utf-8-sig") as source:
                    for line in source:
                        if line.strip():
                            record = AdvertisementRecord.from_dict(json.loads(line))
                            self.state.ingest(decoder.decode(record))
                            await asyncio.sleep(0.05)
                self.state.set_scanner("replay_complete")
            else:
                while not self.stopping.is_set():
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
                    events = Path("captures") / f"dashboard-{stamp}.events.jsonl"
                    raw = Path("captures") / f"dashboard-{stamp}.raw.jsonl"
                    with self.state.lock:
                        self.state.paths = {"events": str(events), "raw": str(raw)}
                    self.state.set_scanner("starting")
                    try:
                        options = {"transport": self.transport} if self.transport.backend != "local" else {}
                        await capture(
                            mode="monitor", seconds=0, output=events, raw_output=raw,
                            devices=self.devices, configured_only=True, quiet=True,
                            on_record=self.state.ingest,
                            on_started=lambda: self.state.set_scanner("listening"), **options,
                        )
                        self.state.set_scanner("stopped")
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if self.transport.backend != "esphome":
                            raise
                        self.state.set_scanner("error", "ESPHome 代理連線中斷，正在重試；請檢查網路、代理供電與 API 設定。")
                        await asyncio.sleep(self.retry_seconds)
        except asyncio.CancelledError:
            self.state.set_scanner("stopped")
        except Exception as error:
            message = ("無法讀取重播檔；請使用原始廣播 JSONL。" if self.replay_path else
                       "藍牙接收中斷；請確認本機藍牙已開啟、驅動正常，並重新啟動監控台。")
            self.state.set_scanner("error", message)
            print(f"{message} ({type(error).__name__})", flush=True)
        finally:
            self.loop, self.task = None, None


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, state: DashboardState, port: int = 8765) -> None:
        self.state = state
        self.closing = threading.Event()
        super().__init__(("127.0.0.1", port), DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: DashboardServer

    def log_message(self, *_: Any) -> None:
        pass

    def _allowed(self) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host", "").lower() not in hosts or (
            origin is not None and origin.lower() not in {f"http://{host}" for host in hosts}
        ):
            self._send(403, b"Local requests only.", "text/plain; charset=utf-8")
            return False
        return True

    def _headers(self, status: int, mime: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def _send(self, status: int, body: bytes, mime: str, *, filename: str | None = None) -> None:
        self._discard_unread_post_body()
        self._headers(status, mime)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _discard_unread_post_body(self) -> None:
        # Closing a Windows TCP socket with unread POST data can reset it and
        # discard an otherwise valid 400/403 response. Consume only a small,
        # explicitly framed body before closing; never wait for arbitrary input.
        if self.command != "POST" or getattr(self, "_post_body_read", False):
            return
        self._post_body_read = True
        if self.headers.get("Transfer-Encoding"):
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except (ValueError, TypeError):
            return
        if not 0 < size <= 4096:
            return
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + .25
        try:
            while size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(size)
                if not chunk:
                    break
                size -= len(chunk)
        except (OSError, TimeoutError):
            pass
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                pass

    def do_GET(self) -> None:
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        if path == "/api/capabilities":
            from .capabilities import capabilities
            body = json.dumps(capabilities(), ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path in {"/api/state", "/api/export"}:
            body = json.dumps(self.server.state.snapshot(), ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8",
                       filename="linptech-session.json" if path == "/api/export" else None)
        elif path == "/api/stream":
            self._headers(200, "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(5)
            try:
                while not self.server.closing.is_set():
                    payload = json.dumps(self.server.state.snapshot(), ensure_ascii=False, allow_nan=False)
                    self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    if self.server.closing.wait(1):
                        break
            except (OSError, TimeoutError):
                pass
        else:
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                      "/styles.css": ("styles.css", "text/css"), "/icon.svg": ("icon.svg", "image/svg+xml")}
            if path not in assets:
                self._send(404, b"Not found.", "text/plain; charset=utf-8")
                return
            name, mime = assets[path]
            self._send(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")

    def do_POST(self) -> None:
        self.close_connection = True
        self._post_body_read = False
        if not self._allowed():
            return
        route = urlsplit(self.path).path
        if route not in {"/api/es5/direct", "/api/sensor/direct"}:
            self._send(404, b"Not found.", "text/plain; charset=utf-8")
            return
        # Custom header + same-origin/Host checks prevent cross-site form posts.
        if self.headers.get("X-Linptech-Control") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._send(403, b"Local JSON control requests only.", "text/plain; charset=utf-8")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 2048:
                raise ValueError()
            self._post_body_read = True
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or not value.keys() <= {"action", "property", "value", "empty_room", "mode", "model"}:
                raise ValueError()
            if not isinstance(value.get("action"), str) or ("property" in value and not isinstance(value["property"], str)):
                raise ValueError()
            expected = ({"action", "property", "value"} if value["action"] == "set" else
                        {"action", "empty_room"} if value["action"] in {"calibrate", "radar_calibrate"} else
                        {"action", "mode"} if value["action"] == "stream" else {"action"})
            if route == "/api/sensor/direct":
                if value.get("model") not in {"MS1BB", "KS2BB", "HS3BB"} or value["action"] not in {"connect", "disconnect", "refresh", "set"}:
                    raise ValueError()
                expected = expected | {"model"}
            if value["action"] == "stream" and value.get("mode") not in {"radar", "pir", "off"}:
                raise ValueError()
            if value.keys() != expected:
                raise ValueError()
        except (ValueError, TypeError, UnicodeDecodeError):
            self.close_connection = True
            self._send(400, b"Invalid control request.", "text/plain; charset=utf-8")
            return
        try:
            controller = self.server.state.direct if route == "/api/es5/direct" else self.server.state.sensor_direct.get(value.pop("model"))
            if controller is None:
                raise DirectError("離線重播模式或未啟用 ES5BB 直連。")
            operation_id = controller.submit(value)
            self._send(202, json.dumps({"id": operation_id}).encode(), "application/json; charset=utf-8")
        except DirectError as error:
            self._send(409, json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


def serve_dashboard(*, config: str | Path, port: int = 8765, open_browser: bool = False,
                    replay_path: str | Path | None = None, stale_after: float = 180,
                    bridge_path: str | Path | None = None) -> None:
    if replay_path is not None and bridge_path is not None:
        raise ValueError("離線重播不能同時載入 bridge 設定；請移除 --bridge-config，避免歷史事件送到 HA。")
    bridge = load_bridge_config(bridge_path)
    devices = load_config(config)
    if not devices:
        raise ValueError("設定檔內沒有裝置；請先使用 add-device 或 import-keys。")
    source = Path(replay_path) if replay_path else None
    if source is not None and not source.is_file():
        raise FileNotFoundError("找不到原始廣播重播檔。")
    state = DashboardState(devices, mode="replay" if source else "live", stale_after=stale_after)
    state.transport = public_transport(bridge.bluetooth)
    if source is None:
        state.direct = ProcessDirectController(next((device for device in devices if device.model == "ES5BB"), None),
                                        on_value=state.ingest_direct, transport=bridge.bluetooth)
        state.sensor_direct = {model: ProcessSensorController(next(device for device in devices if device.model == model),
                                  transport=bridge.bluetooth)
                               for model in ("MS1BB", "KS2BB", "HS3BB") if any(device.model == model for device in devices)}
        if bridge.mqtt.enabled:
            state.mqtt = HomeAssistantBridge(bridge.mqtt, state.snapshot, state.direct.submit)
    # Bind before starting BLE so an occupied port cannot leave an orphan scanner.
    server = DashboardServer(state, port)
    worker = MonitorWorker(state, devices, replay_path=source, transport=bridge.bluetooth)
    auto = AutoConnector(state.direct, retry_seconds=bridge.es5_retry_seconds,
                         ready=lambda: state.scanner_status == "listening") if state.direct and bridge.es5_auto_connect else None
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"即時監控台：{url}\n按 Ctrl+C 停止網頁服務與藍牙監聽。", flush=True)
    if source:
        print("目前為離線重播；畫面保留原始封包時間，不代表現在的感測狀態。", flush=True)
    elif bridge.bluetooth.backend == "esphome":
        print("接收來源：ESPHome Bluetooth Proxy；HA 經 MQTT 接收，代理廣播訂閱由本程式負責。", flush=True)
    started = False
    try:
        worker.start()
        started = True
        if state.direct:
            state.direct.start()
        for controller in state.sensor_direct.values():
            if controller.catalog and controller.device.auth_token:
                controller.start()
        if state.mqtt:
            state.mqtt.start()
        if auto:
            auto.start()
        if open_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.25)
    finally:
        server.closing.set()
        if auto:
            auto.stop()
        if state.mqtt:
            state.mqtt.stop()
        if state.direct:
            state.direct.stop()
        for controller in state.sensor_direct.values():
            controller.stop()
        if started:
            worker.stop()
        server.server_close()
