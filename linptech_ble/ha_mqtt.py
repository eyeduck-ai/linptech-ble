"""Optional Home Assistant MQTT Discovery bridge; no credentials leave this process.

Only explicitly selected public fields are published. MQTT callbacks enqueue
commands; the publishing worker invokes the existing direct-controller guards.
Button events are live, non-retained QoS 0 notifications, never an offline queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import queue
import re
import threading
import time
from typing import Any, Callable

from .config import normalize_address
from .broadcast_objects import ES5_EVENTS
from .es5_direct import CATALOG
from .es5_radar import radar_bins


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    tls: bool = False
    ca_cert: str | None = field(default=None, repr=False)
    topic_prefix: str = "linptech_ble"
    discovery_prefix: str = "homeassistant"
    client_id: str = "linptech-ble"
    poll_interval: float = 1.0
    stale_after: float = 180
    battery_stale_after: float = 86400

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.tls) is not bool:
            raise ValueError("MQTT enabled/tls 必須是布林值。")
        if not isinstance(self.host, str) or (self.enabled and not self.host.strip()) or any(c in self.host for c in "/\r\n\x00"):
            raise ValueError("MQTT host 必須是主機名稱或 IP。")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("MQTT port 必須介於 1–65535。")
        for value in (self.topic_prefix, self.discovery_prefix):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", value):
                raise ValueError("MQTT topic prefix 只能包含英數、底線、連字號與非空白路徑。")
        if not isinstance(self.client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.client_id):
            raise ValueError("MQTT client_id 格式無效。")
        for value in (self.poll_interval, self.stale_after, self.battery_stale_after):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("MQTT 更新與過期秒數必須大於零。")
        for value in (self.username, self.password, self.ca_cert):
            if value is not None and not isinstance(value, str):
                raise ValueError("MQTT 認證與 CA 路徑必須是字串。")
        if self.ca_cert and not self.tls:
            raise ValueError("MQTT ca_cert 需要啟用 tls。")


MEASUREMENTS = {
    "battery": ("Battery", "%", "battery"),
    "temperature": ("Temperature", "°C", "temperature"),
    "humidity": ("Humidity", "%", "humidity"),
    "illuminance": ("Illuminance", "lx", "illuminance"),
    "no_motion_duration": ("No motion duration", "s", "duration"),
    "custom_no_motion_time": ("Reported no motion timeout", "min", "duration"),
    "duration_detected": ("Occupied duration", "min", "duration"),
    "duration_cleared": ("Vacant duration", "min", "duration"),
}
BINARY = {
    "occupancy": ("Occupancy", "occupancy"),
    "occupancy_close_range": ("Close range occupancy", "occupancy"),
    "motion": ("Motion", "motion"),
    "door": ("Opening", "opening"), "opening": ("Opening", "opening"),
    "door_left_open": ("Door left open", "problem"),
    "device_forcibly_removed": ("Tamper", "tamper"),
    "door_stuck": ("Door stuck", "problem"),
    "knock_on_the_door": ("Knock", "vibration"),
    "pry_the_door": ("Pry alarm", "tamper"),
}
MODEL_FIELDS = {
    "ES5BB": ("battery", "illuminance", "occupancy", "occupancy_close_range", "duration_detected", "duration_cleared"),
    "MS1BB": ("battery", "opening"),
    "KS2BB": ("battery", "temperature", "humidity"),
    "HS3BB": ("battery", "illuminance", "motion", "no_motion_duration", "custom_no_motion_time"),
}
BUTTON_TYPES = ("press", "double_press", "long_press")
DEVICE_EVENT_TYPES = {
    "ES5BB": tuple(event for _, event in ES5_EVENTS),
    "KS2BB": ("device_reset",),
    "HS3BB": ("device_reset",),
}
# Raw threshold strings and calibration commands intentionally stay in the web
# workflow, where empty-room confirmation, preview and rollback are available.
WRITABLE = {name: prop for name, prop in CATALOG.items()
            if prop.writable and prop.format in {"bool", "uint8", "uint16", "uint32"}}
READ_PROPERTIES = {name: prop for name, prop in CATALOG.items()
                   if prop.format in {"bool", "uint8", "uint16", "uint32", "int32", "float"}
                   and name not in {"cmd_to_device", "occupancy_status", "occupancy_close_range", "illuminance", "battery_level"}}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _age(timestamp: Any, now: datetime) -> float | None:
    if not isinstance(timestamp, str):
        return None
    try:
        then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        age = (now - then).total_seconds()
        return age if age >= -5 else None
    except (ValueError, TypeError):
        return None


class HomeAssistantBridge:
    def __init__(self, config: MqttConfig, snapshot: Callable[[], dict[str, Any]],
                 submit: Callable[[dict[str, Any]], str] | None = None) -> None:
        self.config, self._snapshot, self._submit = config, snapshot, submit
        self._client: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._resync = threading.Event()
        self._lock = threading.RLock()
        self._commands: queue.Queue = queue.Queue(maxsize=64)
        self._epoch = 0
        self._discovery: set[str] = set()
        self._cache: dict[str, str] = {}
        self._event_start: str | None = None
        self._event_high = 0
        self._event_initialized = False
        self._status = {"enabled": config.enabled, "status": "disabled" if not config.enabled else "stopped",
                        "error": None, "published": 0, "commands_accepted": 0, "commands_rejected": 0}

    @property
    def availability_topic(self) -> str:
        return f"{self.config.topic_prefix}/bridge/availability"

    def status(self) -> dict[str, Any]:
        """Public metadata only; no broker credentials, config, packet or exception text."""
        with self._lock:
            return dict(self._status)

    def start(self) -> None:
        if not self.config.enabled or (self._thread and self._thread.is_alive()):
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("請先安裝 MQTT 選用套件：pip install -e .[ha]") from exc
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.config.client_id,
                             clean_session=True, protocol=mqtt.MQTTv311)
        if self.config.username is not None:
            client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls:
            client.tls_set(ca_certs=self.config.ca_cert)
        client.will_set(self.availability_topic, "offline", qos=1, retain=True)
        client.on_connect, client.on_disconnect, client.on_message = self._on_connect, self._on_disconnect, self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.max_queued_messages_set(128)
        self._client = client
        self._stop.clear()
        self._set_status(status="connecting", error=None)
        # connect_async + loop_start reconnect without blocking the dashboard.
        client.connect_async(self.config.host, self.config.port, keepalive=30)
        client.loop_start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="linptech-mqtt")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        client = self._client
        if client:
            if self._connected.is_set():
                try:
                    result = client.publish(self.availability_topic, "offline", qos=1, retain=True)
                    result.wait_for_publish(timeout=2)
                except Exception:
                    pass
            client.disconnect()
            client.loop_stop()
        self._connected.clear()
        self._set_status(status="stopped" if self.config.enabled else "disabled")

    def _set_status(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            self._set_status(status="error", error="MQTT 連線遭拒，請核對本機 broker 設定。")
            return
        with self._lock:
            self._epoch += 1
        self._connected.set()
        self._resync.set()
        client.subscribe(f"{self.config.discovery_prefix}/status", qos=0)
        client.subscribe(f"{self.config.topic_prefix}/+/command/+", qos=0)
        self._set_status(status="connected", error=None)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        self._connected.clear()
        with self._lock:
            self._epoch += 1
        self._set_status(status="reconnecting", error="MQTT 連線中斷，正在重新連線。")

    def _on_message(self, client, userdata, message) -> None:
        if message.topic == f"{self.config.discovery_prefix}/status":
            if message.payload == b"online":
                self._resync.set()
            return
        # Retained commands must never alter settings on startup or reconnect.
        if message.retain or not self._connected.is_set() or len(message.payload) > 128:
            self._reject()
            return
        try:
            payload = message.payload.decode("utf-8")
            self._commands.put_nowait((self._epoch, time.monotonic(), message.topic, payload))
        except (UnicodeDecodeError, queue.Full):
            self._reject()

    def _reject(self) -> None:
        with self._lock:
            self._status["commands_rejected"] += 1

    def _publish(self, topic: str, payload: str, *, retain: bool = True,
                 qos: int = 0, force: bool = False) -> bool:
        if not self._connected.is_set():
            return False
        if retain and not force and self._cache.get(topic) == payload:
            return True
        try:
            result = self._client.publish(topic, payload, qos=qos, retain=retain)
            if result.rc != 0:
                return False
        except Exception:
            self._set_status(error="MQTT 發佈失敗，等待重新連線。")
            return False
        if retain:
            self._cache[topic] = payload
        with self._lock:
            self._status["published"] += 1
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # Do not propagate snapshot/controller/network exception text;
                # third-party exceptions can include endpoints or credentials.
                self._set_status(error="MQTT 同步失敗，將於下一輪重試。")
                # A failed snapshot/sync must not leave retained occupancy
                # marked available indefinitely. A successful tick reopens it.
                self._publish(self.availability_topic, "offline", qos=1)
            self._stop.wait(self.config.poll_interval)

    def _entity(self, device: dict[str, Any], key: str, component: str, name: str,
                *, force: bool = False, **extra: Any) -> tuple[str, str]:
        mac = normalize_address(device["address"]).replace(":", "").lower()
        identity = f"linptech_{mac}"
        base = f"{self.config.topic_prefix}/{mac}/{key}"
        topic = f"{self.config.discovery_prefix}/{component}/{identity}/{key}/config"
        if force or topic not in self._discovery:
            payload = {"name": name, "unique_id": f"{identity}_{key}",
                       "device": {"identifiers": [identity], "manufacturer": "Linptech",
                                  "model": device["model"], "name": device["name"]},
                       "availability": [{"topic": self.availability_topic}, {"topic": f"{base}/availability"}],
                       "availability_mode": "all", **extra}
            if component != "button":
                payload["state_topic"] = f"{base}/state"
            if component in {"sensor", "binary_sensor", "number", "switch"}:
                payload["json_attributes_topic"] = f"{base}/attributes"
            if self._publish(topic, _json(payload), qos=1, force=True):
                self._discovery.add(topic)
        return base, mac

    def _value(self, base: str, value: Any, available: bool, timestamp: str | None,
               *, attributes: dict[str, Any] | None = None, binary: bool = False,
               force: bool = False) -> None:
        # Set unavailability without publishing false, zero or a fabricated clear.
        if available:
            self._publish(f"{base}/attributes", _json({"updated_at": timestamp, **(attributes or {})}), force=force)
            self._publish(f"{base}/state", ("ON" if value else "OFF") if binary else str(value), force=force)
        self._publish(f"{base}/availability", "online" if available else "offline", force=force)

    @staticmethod
    def _transport_available(snapshot: dict[str, Any]) -> bool:
        if snapshot.get("mode") != "live":
            return False
        transport = snapshot.get("transport")
        if isinstance(transport, dict) and type(transport.get("available")) is bool:
            return transport["available"]
        return snapshot.get("scanner", {}).get("status") == "listening"

    def _entry_available(self, entry: dict[str, Any], now: datetime,
                         passive: bool, direct_ready: bool, *, battery=False) -> bool:
        age = _age(entry.get("updated_at"), now)
        return (direct_ready if entry.get("source") == "gatt" else passive) and age is not None and age <= (
            self.config.battery_stale_after if battery else self.config.stale_after)

    def _tick(self, *, now: datetime | None = None) -> None:
        snapshot = self._snapshot()
        now = now or datetime.now(timezone.utc)
        # Consume the snapshot watermark while disconnected too. Reconnecting
        # publishes the present state, never buttons pressed during the outage.
        force = self._resync.is_set()
        if force:
            self._resync.clear()
            self._cache.clear()
            self._discovery.clear()
        connected = self._connected.is_set()
        self._buttons(snapshot, now, drop=force or not connected)
        if not connected:
            self._drain_commands(None, now)
            return
        if force:
            # Gate retained states during resynchronization. Old per-field
            # availability must not briefly resurrect a stale occupancy value.
            self._publish(self.availability_topic, "offline", qos=1, force=True)
        passive = self._transport_available(snapshot)
        direct = snapshot.get("direct") or {}
        ready = direct.get("authenticated") is True and direct.get("status") == "ready"
        sensor_direct = snapshot.get("sensor_direct") or {}
        direct_device = next((d for d in snapshot.get("devices", []) if d.get("model") == "ES5BB"), None)
        for device in snapshot.get("devices", []):
            if device.get("model") not in MODEL_FIELDS:
                continue
            measurements, binaries = device.get("measurements", {}), device.get("binary_sensors", {})
            session = sensor_direct.get(device["model"]) or {}
            device_ready = ready if device["model"] == "ES5BB" else session.get("authenticated") is True and session.get("status") == "ready"
            fields = set(MODEL_FIELDS[device["model"]]) | (set(measurements) & set(MEASUREMENTS)) | (set(binaries) & set(BINARY))
            if "door" in fields:
                fields.remove("door")
                fields.add("opening")
            for key in sorted(fields):
                binary = key in BINARY
                entry = (binaries if binary else measurements).get(key, {})
                if key == "opening" and not entry:
                    entry = binaries.get("door", {})
                if binary:
                    label, cls = BINARY[key]
                    options = {"device_class": cls, "payload_on": "ON", "payload_off": "OFF"}
                else:
                    label, unit, cls = MEASUREMENTS[key]
                    options = {"device_class": cls, "unit_of_measurement": unit, "state_class": "measurement"}
                    if key == "battery":
                        options["entity_category"] = "diagnostic"
                base, _ = self._entity(device, key, "binary_sensor" if binary else "sensor", label, force=force, **options)
                value = entry.get("value")
                valid = type(value) is bool if binary else type(value) in (int, float) and math.isfinite(value)
                available = bool(valid and self._entry_available(entry, now, passive, device_ready, battery=key == "battery"))
                self._value(base, value, available, entry.get("updated_at"), binary=binary, force=force,
                            attributes={"source": entry.get("source", "advertisement")})
            if device["model"] == "KS2BB":
                base, _ = self._entity(device, "button", "event", "Button", force=force,
                                       event_types=list(BUTTON_TYPES), device_class="button")
                self._publish(f"{base}/availability", "online" if passive else "offline", force=force)
            if device["model"] in DEVICE_EVENT_TYPES:
                base, _ = self._entity(device, "device_event", "event", "Device event", force=force,
                                       event_types=list(DEVICE_EVENT_TYPES[device["model"]]))
                self._publish(f"{base}/availability", "online" if passive else "offline", force=force)
            # The existing controller owns the first configured ES5BB only.
            # Never mirror its GATT data or route writes to a second ES5BB.
            if device is direct_device:
                self._advanced(device, direct, ready, now, force)
        self._drain_commands(snapshot, now)
        self._publish(self.availability_topic, "online", qos=1, force=force)

    def _buttons(self, snapshot: dict[str, Any], now: datetime, *, drop: bool) -> None:
        events = [event for event in snapshot.get("events", []) if type(event.get("id")) is int]
        high = max((event["id"] for event in events), default=0)
        started = snapshot.get("started_at")
        if not self._event_initialized or self._event_start != started or high < self._event_high:
            drop = True
            self._event_initialized, self._event_start = True, started
        watermark = self._event_high
        self._event_high = high
        if drop or not self._transport_available(snapshot):
            return
        for event in sorted(events, key=lambda item: item["id"]):
            if event["id"] <= watermark:
                continue
            model, kind, event_type = event.get("model"), event.get("kind"), event.get("key")
            if model == "KS2BB" and kind == "button" and event_type in BUTTON_TYPES:
                entity = "button"
            elif kind == "device" and event_type in DEVICE_EVENT_TYPES.get(model, ()):
                entity = "device_event"
            else:
                continue
            age = _age(event.get("timestamp"), now)
            if age is None or age > max(5, self.config.poll_interval * 3):
                continue
            try:
                mac = normalize_address(event["address"]).replace(":", "").lower()
            except (KeyError, ValueError):
                continue
            self._publish(f"{self.config.topic_prefix}/{mac}/{entity}/state", _json({
                "event_type": event["key"], "event_id": event["id"], "timestamp": event["timestamp"]}), retain=False, qos=0)

    def _advanced(self, device: dict[str, Any], direct: dict[str, Any], ready: bool,
                  now: datetime, force: bool) -> None:
        props = direct.get("properties", {})
        bitmap_entry = props.get("human_sensor_bitmaps", {})
        bits = direct.get("bitmaps") or {}
        bitmap_age = _age(bitmap_entry.get("updated_at"), now)
        fresh_bitmap = ready and bitmap_entry.get("status") == "read" and bitmap_age is not None and bitmap_age <= 8
        for name, label, value, binary in (
            ("pir", "PIR", bits.get("pir"), True),
            ("occupied_mask", "Occupied distance mask", bits.get("occupied"), False),
            ("floor_noise", "Radar noise health value", bits.get("floor_noise"), False),
        ):
            options = {"device_class": "motion", "payload_on": "ON", "payload_off": "OFF"} if binary else {}
            base, _ = self._entity(device, name, "binary_sensor" if binary else "sensor", label, force=force, **options)
            valid = type(value) is int and value >= 0
            attributes = {"source": "gatt"}
            if name == "occupied_mask" and valid:
                attributes.update(bins=radar_bins(value), bin_meters=.25)
            # Radar-only mode disables the PIR detector; a cached raw zero is
            # not evidence of an active infrared detector reporting clear.
            pir_enabled = name != "pir" or props.get("detect_mode", {}).get("value") != 0
            self._value(base, bool(value) if binary else value, fresh_bitmap and valid and pir_enabled,
                        bitmap_entry.get("updated_at"), attributes=attributes, binary=binary, force=force)
        for name in ("motion_energy", "presence_energy", "motion_threshold", "presence_threshold"):
            entry = props.get(name, {})
            values = entry.get("series")
            valid = isinstance(values, list) and len(values) == 32 and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 255 for v in values)
            age = _age(entry.get("updated_at"), now)
            # Thresholds are a configuration snapshot for the authenticated
            # session. Unlike energies they are not a continuously polled feed.
            available = ready and entry.get("status") == "read" and valid and age is not None and (not name.endswith("energy") or age <= 8)
            base, _ = self._entity(device, name, "sensor", CATALOG[name].label, force=force)
            self._value(base, max(values) if valid else None, available, entry.get("updated_at"), force=force,
                        attributes={"bins": values if valid else [], "bin_meters": .25, "source": "gatt"})
        for name, prop in READ_PROPERTIES.items():
            entry = props.get(name, {})
            writable = name in WRITABLE and self._submit is not None
            component = "switch" if writable and prop.format == "bool" else ("number" if writable else "sensor")
            options: dict[str, Any] = {"entity_category": "config" if writable else "diagnostic"}
            mac = normalize_address(device["address"]).replace(":", "").lower()
            if writable:
                options.update(command_topic=f"{self.config.topic_prefix}/{mac}/command/{name}", retain=False, optimistic=False)
                if prop.format == "bool":
                    options.update(payload_on="ON", payload_off="OFF")
                else:
                    options.update(min=prop.minimum if prop.minimum is not None else 0,
                                   max=prop.maximum if prop.maximum is not None else 0xFFFFFFFF, step=1, mode="box")
            base, _ = self._entity(device, name, component, prop.label, force=force, **options)
            age = _age(entry.get("updated_at"), now)
            value = entry.get("value")
            valid = type(value) is bool if prop.format == "bool" else type(value) in (int, float) and math.isfinite(value)
            attributes = {"source": "gatt"}
            if prop.choices and type(value) is int and 0 <= value < len(prop.choices):
                attributes["choice"] = prop.choices[value]
            if name in {"detect_region", "interest_region"}:
                attributes.update(bins=radar_bins(value), bin_meters=.25)
            dynamic_available = name != "pir_energy" or (age is not None and age <= 8 and props.get("detect_mode", {}).get("value") != 0)
            self._value(base, value, ready and entry.get("status") == "read" and valid and age is not None and dynamic_available,
                        entry.get("updated_at"), binary=prop.format == "bool", attributes=attributes, force=force)
        if self._submit:
            for action in ("connect", "disconnect", "refresh"):
                mac = normalize_address(device["address"]).replace(":", "").lower()
                base, _ = self._entity(device, action, "button", f"ES5BB {action}", force=force,
                    command_topic=f"{self.config.topic_prefix}/{mac}/command/{action}", payload_press="PRESS", retain=False,
                    entity_category="config")
                available = bool(direct.get("token_configured")) if action == "connect" else ready
                self._publish(f"{base}/availability", "online" if available else "offline", force=force)

    def _drain_commands(self, snapshot: dict[str, Any] | None, now: datetime) -> None:
        while True:
            try:
                epoch, created, topic, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if not snapshot or not self._submit or epoch != self._epoch or not self._connected.is_set() or time.monotonic() - created > 5:
                    raise ValueError
                target = next((d for d in snapshot.get("devices", []) if d.get("model") == "ES5BB"), None)
                devices = {normalize_address(target["address"]).replace(":", "").lower(): target} if target else {}
                parts = topic.removeprefix(self.config.topic_prefix + "/").split("/")
                if not topic.startswith(self.config.topic_prefix + "/") or len(parts) != 3 or parts[1] != "command" or parts[0] not in devices:
                    raise ValueError
                name = parts[2]
                direct = snapshot.get("direct") or {}
                ready = direct.get("authenticated") is True and direct.get("status") == "ready"
                if name in {"connect", "disconnect", "refresh"}:
                    if payload != "PRESS" or (name != "connect" and not ready) or (name == "connect" and not direct.get("token_configured")):
                        raise ValueError
                    command = {"action": name}
                else:
                    prop = WRITABLE.get(name)
                    entry = direct.get("properties", {}).get(name, {})
                    age = _age(entry.get("updated_at"), now)
                    if not prop or not ready or direct.get("busy") or entry.get("status") != "read" or age is None:
                        raise ValueError
                    if prop.format == "bool":
                        if payload not in {"ON", "OFF"}:
                            raise ValueError
                        value = payload == "ON"
                    else:
                        if not re.fullmatch(r"0|[1-9][0-9]{0,9}", payload):
                            raise ValueError
                        value = int(payload)
                    prop.validate(value)
                    command = {"action": "set", "property": name, "value": value}
                self._submit(command)
                with self._lock:
                    self._status["commands_accepted"] += 1
            except Exception:
                self._reject()
