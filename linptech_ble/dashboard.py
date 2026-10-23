"""Loopback-only sensor dashboard. Bindkeys stay in the Python process."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import isfinite
from pathlib import Path
import threading
import time
from statistics import median
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import webbrowser

from .config import DeviceConfig, load_config, normalize_address
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
from .dashboard_inventory import DashboardInventory

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
RADIO_HISTORY_LIMIT = 600
RADIO_WINDOW_SECONDS = 300


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def age_seconds(timestamp: str | None, now: datetime) -> float | None:
    then = _sample_time(timestamp)
    if then is None:
        return None
    return max(0, (now - then).total_seconds())


def _sample_time(timestamp: Any) -> datetime | None:
    """Reject malformed sample times rather than replacing them with wall time."""
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _radio_summary(points: list[dict[str, Any]], now: datetime, receiver: str | None) -> dict[str, Any]:
    recent = [(stamp, point) for point in points if (stamp := _sample_time(point["timestamp"])) is not None
              and 0 <= (now - stamp).total_seconds() <= RADIO_WINDOW_SECONDS]
    recent.sort(key=lambda pair: pair[0])
    values = [point["rssi"] for _, point in recent]
    last = recent[-1] if recent else None
    return {
        "receiver": receiver, "scope": "receiver" if receiver else "all_receivers",
        "window_seconds": RADIO_WINDOW_SECONDS, "count": len(values),
        "latest": last[1]["rssi"] if last else None,
        "median": median(values) if values else None,
        "min": min(values) if values else None, "max": max(values) if values else None,
        "last_sample": last[1]["timestamp"] if last else None,
        "last_sample_age_seconds": (now - last[0]).total_seconds() if last else None,
        "max_gap_seconds": max(((right[0] - left[0]).total_seconds()
                                for left, right in zip(recent, recent[1:])), default=None),
    }


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
        self.direct_devices: dict[str, DirectController] = {}
        self.transport = public_transport(BluetoothConfig())
        self.mqtt: HomeAssistantBridge | None = None
        self.inventory: DashboardInventory | None = None
        # Account sessions and QR data are intentionally absent from snapshots,
        # SSE, session exports, and MQTT. Only /api/cloud exposes redacted state.
        self.cloud_credentials: Any = None
        self.devices = {
            device.address: {
                "address": device.address, "model": device.model, "name": device.name,
                "key_configured": bool(device.bindkey), "token_configured": bool(device.auth_token), "verified": False,
                "last_seen": None, "last_decoded": None, "last_status": None,
                "last_error": None, "rssi": None, "packets": 0, "decoded": 0,
                "duplicates": 0, "errors": 0, "measurements": {}, "binary_sensors": {},
                "last_event": None, "history": {}, "objects": {},
                "radio_history": deque(maxlen=RADIO_HISTORY_LIMIT),
            }
            for device in sorted(devices, key=lambda item: MODEL_ORDER.get(item.model, 99))
        }

    def set_scanner(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.scanner_status, self.scanner_error = status, error

    def direct_controller(self, model: str, address: str | None = None) -> DirectController | None:
        """Resolve only configured targets; the omitted-address route is legacy."""
        if address is None:
            return self.direct if model == "ES5BB" else self.sensor_direct.get(model)
        address = normalize_address(address)
        device = self.devices.get(address)
        if device is None or device["model"] != model:
            raise DirectError("請選擇設定檔內型號相符的裝置。")
        return self.direct_devices.get(address)

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
            rssi = packet.get("rssi")
            valid_rssi = type(rssi) in {int, float} and -127 < rssi < 0 and isfinite(rssi)
            if valid_rssi and _sample_time(timestamp) is not None:
                receiver = packet.get("receiver")
                receiver = receiver if isinstance(receiver, str) and 0 < len(receiver) <= 64 else "local"
                device["radio_history"].append({"timestamp": timestamp, "rssi": rssi, "receiver": receiver})
            device.update(last_seen=timestamp, last_status=status)
            if valid_rssi:
                device["rssi"] = rssi
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
            address = record.get("address")
            if address is None:
                device = next((item for item in self.devices.values() if item["model"] == "ES5BB"), None)
            else:
                try:
                    device = self.devices.get(normalize_address(address))
                except ValueError:
                    return
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
                public = deepcopy({key: value for key, value in device.items() if key not in {"history", "radio_history"}})
                public["history"] = {key: list(points) for key, points in device["history"].items()}
                points = list(device["radio_history"])
                receivers = dict.fromkeys(point["receiver"] for point in points)
                public["radio"] = {
                    "window_seconds": RADIO_WINDOW_SECONDS, "history_limit": RADIO_HISTORY_LIMIT,
                    "history": deepcopy(points), "summary": _radio_summary(points, now, None),
                    "receivers": {receiver: _radio_summary([p for p in points if p["receiver"] == receiver], now, receiver)
                                  for receiver in receivers},
                }
                for entry in public["binary_sensors"].values():
                    entry["age_seconds"] = age_seconds(entry["updated_at"], now)
                    entry["freshness"] = "recent" if entry["age_seconds"] is not None and entry["age_seconds"] <= self.stale_after else "stale"
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
                "direct_devices": {address: controller.snapshot() for address, controller in self.direct_devices.items()},
                "transport": dict(self.transport),
                "mqtt": self.mqtt.status() if self.mqtt else {"enabled": False, "status": "disabled"},
                "management": {"enabled": self.inventory is not None and self.mode == "live"},
                "discovery": self.inventory.discovery.snapshot() if self.inventory else {
                    "status": "unavailable", "devices": [], "error": None, "remaining_seconds": 0},
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
                            on_raw_record=lambda record: self.state.inventory.discovery.observe(record)
                                if self.state.inventory else None,
                            device_source=lambda: self.state.inventory.source() if self.state.inventory else self.devices,
                            callback_lock=self.state.inventory.lock if self.state.inventory else None,
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
        if path in {"/api/cloud", "/api/cloud/qr.png"}:
            self._cloud_get(path)
        elif path in {"/api/devices", "/api/discovery"}:
            inventory = self.server.state.inventory
            value = (inventory.snapshot() if path == "/api/devices" else inventory.discovery.snapshot()) if inventory else {
                "enabled": False, "status": "unavailable", "devices": [], "error": None}
            self._send(200, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path == "/api/capabilities":
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
        if route not in {"/api/es5/direct", "/api/sensor/direct", "/api/devices", "/api/discovery", "/api/cloud"}:
            self._send(404, b"Not found.", "text/plain; charset=utf-8")
            return
        # Custom header + same-origin/Host checks prevent cross-site form posts.
        if self.headers.get("X-Linptech-Control") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._send(403, b"Local JSON control requests only.", "text/plain; charset=utf-8")
            return
        if route == "/api/cloud":
            self._cloud_post()
            return
        if route in {"/api/devices", "/api/discovery"}:
            self._inventory_post(route)
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
            if not isinstance(value, dict) or not value.keys() <= {"action", "property", "value", "empty_room", "mode", "model", "address"}:
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
            if "address" in value:
                value["address"] = normalize_address(value["address"])
                expected = expected | {"address"}
            if value["action"] == "stream" and value.get("mode") not in {"radar", "pir", "off"}:
                raise ValueError()
            if value.keys() != expected:
                raise ValueError()
        except (ValueError, TypeError, UnicodeDecodeError):
            self.close_connection = True
            self._send(400, b"Invalid control request.", "text/plain; charset=utf-8")
            return
        try:
            model = "ES5BB" if route == "/api/es5/direct" else value.pop("model")
            from contextlib import nullcontext
            inventory = self.server.state.inventory
            with inventory.lock if inventory else nullcontext():
                controller = self.server.state.direct_controller(model, value.pop("address", None))
                if controller is None:
                    raise DirectError("離線重播模式或未啟用此裝置的直連。")
                operation_id = controller.submit(value)
            self._send(202, json.dumps({"id": operation_id}).encode(), "application/json; charset=utf-8")
        except DirectError as error:
            self._send(409, json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _inventory_post(self, route: str) -> None:
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError()
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 4096:
                raise ValueError()
            self._post_body_read = True
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            command = json.loads(raw)
            if not isinstance(command, dict):
                raise ValueError()
            inventory = self.server.state.inventory
            if inventory is None or self.server.state.mode != "live":
                raise DirectError("此工作階段不允許修改裝置或搜尋附近裝置。")
            if route == "/api/discovery":
                if command.keys() != {"action"} or command.get("action") not in {"start", "stop"}:
                    raise ValueError()
                result = inventory.discovery.command(command["action"])
            else:
                result = inventory.mutate(command)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        except (ValueError, TypeError, UnicodeDecodeError):
            self._send(400, json.dumps({"error": "裝置設定格式不正確；請檢查 MAC、型號、名稱與憑證長度。"}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except DirectError as error:
            self._send(409, json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _cloud_json(self, status: int, value: dict[str, Any]) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _cloud_get(self, path: str) -> None:
        manager = self.server.state.cloud_credentials
        if manager is None or self.server.state.mode != "live":
            if path == "/api/cloud/qr.png":
                self._cloud_json(404, {"error": "此工作階段沒有可用的登入 QR。"})
            else:
                self._cloud_json(200, {"enabled": False, "dependencies_available": False,
                    "phase": "unavailable", "run_id": None, "revision": 0, "qr_available": False,
                    "devices": [], "results": [], "error": None})
            return
        try:
            if path == "/api/cloud":
                self._cloud_json(200, manager.snapshot())
                return
            query_text = urlsplit(self.path).query
            if len(query_text) > 512:
                raise ValueError()
            query = parse_qs(query_text, keep_blank_values=True)
            if set(query) != {"run", "revision"} or any(len(value) != 1 for value in query.values()):
                raise ValueError()
            run_id, revision = query["run"][0], query["revision"][0]
            if not 1 <= len(run_id) <= 128 or not revision.isascii() or not revision.isdecimal() or len(revision) > 9:
                raise ValueError()
            image = manager.qr_image(run_id, int(revision))
            if image is None:
                self._cloud_json(404, {"error": "登入 QR 已更新或失效，請重新取得目前工作階段。"})
            else:
                if not isinstance(image, bytes) or len(image) > 2_000_000 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError()
                self._send(200, image, "image/png")
        except (ValueError, TypeError):
            self._cloud_json(400, {"error": "登入查詢格式不正確。"})
        except Exception:
            self._cloud_json(503, {"error": "無法讀取雲端登入狀態；請稍後重新開始。"})

    def _cloud_post(self) -> None:
        from .cloud_credentials import CloudCredentialError
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError()
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 16384:
                raise ValueError()
            self._post_body_read = True
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            command = json.loads(raw)
            if not isinstance(command, dict):
                raise ValueError()
            action = command.get("action")
            if action == "start":
                if command.keys() != {"action", "region"} or not isinstance(command["region"], str) or len(command["region"]) > 8:
                    raise ValueError()
            elif action in {"cancel", "save"}:
                expected = {"action", "run_id", "devices"} if action == "save" else {"action", "run_id"}
                if (command.keys() != expected or not isinstance(command.get("run_id"), str)
                        or not 1 <= len(command["run_id"]) <= 128):
                    raise ValueError()
                if action == "save" and (not isinstance(command["devices"], list) or not 1 <= len(command["devices"]) <= 256
                        or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in command["devices"])):
                    raise ValueError()
            else:
                raise ValueError()
            manager = self.server.state.cloud_credentials
            if manager is None or self.server.state.mode != "live":
                self._cloud_json(409, {"error": "此工作階段不允許雲端登入或儲存憑證。"})
                return
            if action == "start":
                result = manager.start(command["region"])
            elif action == "cancel":
                result = manager.cancel(command["run_id"])
            else:
                result = manager.save(command["run_id"], command["devices"])
            self._cloud_json(202, result)
        except CloudCredentialError as error:
            self._cloud_json(409, {"error": str(error)})
        except (ValueError, TypeError, UnicodeDecodeError):
            self._cloud_json(400, {"error": "雲端登入操作格式不正確。"})
        except Exception:
            # Transport and provider exceptions may contain account URLs,
            # cookies, or credentials. Never expose their exception strings.
            self._cloud_json(503, {"error": "雲端登入暫時無法使用；請取消後重新開始。"})


def serve_dashboard(*, config: str | Path, port: int = 8765, open_browser: bool = False,
                    replay_path: str | Path | None = None, stale_after: float = 180,
                    bridge_path: str | Path | None = None) -> None:
    if replay_path is not None and bridge_path is not None:
        raise ValueError("離線重播不能同時載入 bridge 設定；請移除 --bridge-config，避免歷史事件送到 HA。")
    bridge = load_bridge_config(bridge_path)
    devices = load_config(config)
    source = Path(replay_path) if replay_path else None
    if source is not None and not source.is_file():
        raise FileNotFoundError("找不到原始廣播重播檔。")
    state = DashboardState(devices, mode="replay" if source else "live", stale_after=stale_after)
    state.transport = public_transport(bridge.bluetooth)
    def make_controller(device):
        controller_type = ProcessDirectController if device.model == "ES5BB" else ProcessSensorController
        controller = controller_type(device, transport=bridge.bluetooth,
            on_value=lambda record, address=device.address: state.ingest_direct({**record, "address": address}))
        controller.defer_start = True
        return controller
    if source is None:
        for device in devices:
            controller = make_controller(device)
            state.direct_devices[device.address] = controller
            if device.model == "ES5BB":
                if state.direct is None:
                    state.direct = controller
            else:
                state.sensor_direct.setdefault(device.model, controller)
    # Bind before starting BLE so an occupied port cannot leave an orphan scanner.
    server = DashboardServer(state, port)
    worker = MonitorWorker(state, devices, replay_path=source, transport=bridge.bluetooth)
    autos = [AutoConnector(controller, retry_seconds=bridge.es5_retry_seconds,
                          ready=lambda: state.scanner_status == "listening",
                          submit=lambda command, controller=controller: state.inventory.submit_auto(controller, command)
                              if state.inventory else None)
             for address, controller in state.direct_devices.items()
             if state.devices[address]["model"] == "ES5BB" and bridge.es5_auto_connect]
    if source is None:
        def sync_autos():
            wanted = [controller for address, controller in state.direct_devices.items()
                      if state.devices[address]["model"] == "ES5BB" and bridge.es5_auto_connect]
            for auto in list(autos):
                if auto.controller not in wanted:
                    auto.stop()
                    autos.remove(auto)
            existing = {auto.controller for auto in autos}
            for controller in wanted:
                if controller not in existing:
                    auto = AutoConnector(controller, retry_seconds=bridge.es5_retry_seconds,
                        ready=lambda: state.scanner_status == "listening",
                        submit=lambda command, controller=controller: state.inventory.submit_auto(controller, command))
                    autos.append(auto)
                    auto.start()
        state.inventory = DashboardInventory(state, devices, config, controller_factory=make_controller,
            new_telemetry=lambda device: DashboardState([device]).devices[device.address],
            on_controllers_changed=sync_autos)
        from .cloud_credentials import CloudCredentialManager
        state.cloud_credentials = CloudCredentialManager(state.inventory)
        if bridge.mqtt.enabled:
            # The bridge supplies its exact advertised address; inventory changes
            # must never route a queued command to a different first ES5 device.
            def mqtt_submit(command):
                with state.inventory.lock:
                    value = dict(command)
                    address = value.pop("address", None)
                    controller = state.direct_controller("ES5BB", address)
                    if controller is None:
                        raise DirectError("此 ES5BB 已移除或未啟用直連。")
                    return controller.submit(value)
            state.mqtt = HomeAssistantBridge(bridge.mqtt, state.snapshot, mqtt_submit,
                targeted_submit=lambda address, command: mqtt_submit({**command, "address": address}))
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
        for address, controller in state.direct_devices.items():
            if state.devices[address]["model"] == "ES5BB" or (controller.catalog and controller.device.auth_token):
                controller.start()
        if state.mqtt:
            state.mqtt.start()
        for auto in autos:
            auto.start()
        if open_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.25)
    finally:
        server.closing.set()
        if state.cloud_credentials:
            try:
                state.cloud_credentials.close()
            except Exception:
                # Account-session cleanup must not prevent scanner/GATT cleanup.
                pass
        for auto in autos:
            auto.stop()
        if state.mqtt:
            state.mqtt.stop()
        for controller in state.direct_devices.values():
            controller.stop()
        if started:
            worker.stop()
        server.server_close()
