"""Separate network-service configuration; device keys remain in sensors.local.json."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import json
import math
from pathlib import Path
from typing import Any

from .ha_mqtt import MqttConfig
from .transport import BluetoothConfig, ProxyConfig


@dataclass(frozen=True)
class BridgeConfig:
    bluetooth: BluetoothConfig = field(default_factory=BluetoothConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    es5_auto_connect: bool = False
    es5_retry_seconds: float = 30


def _object(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.keys() <= allowed:
        raise ValueError("設定區段格式或欄位名稱不正確。")
    return value


def load_bridge_config(path: str | Path | None = None) -> BridgeConfig:
    """Validate before starting any BLE, network or MQTT connections.

    Errors intentionally omit source values: a malformed field can itself be a
    password, API key, or an accidentally selected device-credentials document.
    """
    if path is None:
        return BridgeConfig()
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError("找不到 bridge 連線設定檔；請先複製 examples/bridge.example.json 並填入實際設定。")
    try:
        document = _object(json.loads(file.read_text(encoding="utf-8-sig")), {"version", "bluetooth", "mqtt", "es5"})
        if type(document.get("version")) is not int or document["version"] != 1:
            raise ValueError("version 必須為 1。")
        bluetooth = dict(_object(document.get("bluetooth", {}), {f.name for f in fields(BluetoothConfig)}))
        proxy_values = bluetooth.get("proxies", [])
        if not isinstance(proxy_values, list):
            raise ValueError("proxies 必須是陣列。")
        bluetooth["proxies"] = tuple(ProxyConfig(**_object(proxy, {f.name for f in fields(ProxyConfig)})) for proxy in proxy_values)
        transport = BluetoothConfig(**bluetooth)
        mqtt_values = _object(document.get("mqtt", {}), {f.name for f in fields(MqttConfig)})
        mqtt = MqttConfig(**mqtt_values)
        es5 = _object(document.get("es5", {}), {"auto_connect", "retry_seconds"})
        enabled, interval = es5.get("auto_connect", False), es5.get("retry_seconds", 30)
        if type(enabled) is not bool or type(interval) not in (int, float) or not math.isfinite(interval) or not 5 <= interval <= 3600:
            raise ValueError("auto_connect 必須是布林值，retry_seconds 需介於 5～3600 秒。")
        if enabled and transport.backend == "esphome":
            transport.direct_proxy()
        return BridgeConfig(transport, mqtt, enabled, interval)
    except (ValueError, TypeError, KeyError, UnicodeError, RuntimeError):
        raise ValueError("無效的 bridge 設定；請核對 version、bluetooth.proxies／gatt_proxy、API 加密金鑰、mqtt 與 es5 欄位。") from None


def public_transport(config: BluetoothConfig) -> dict[str, Any]:
    """No host credentials, API keys, passwords or device secrets in HTTP/MQTT."""
    return {"backend": config.backend, "proxy_names": [proxy.name for proxy in config.proxies],
            "gatt_proxy": config.gatt_proxy or (config.proxies[0].name if len(config.proxies) == 1 else None)}
