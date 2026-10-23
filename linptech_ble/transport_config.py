"""BLE transport configuration, separate from local device credentials."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import json
import math
from pathlib import Path
from typing import Any

from .transport import BluetoothConfig, ProxyConfig


@dataclass(frozen=True)
class TransportConfig:
    bluetooth: BluetoothConfig = field(default_factory=BluetoothConfig)
    es5_auto_connect: bool = False
    es5_retry_seconds: float = 30

    def __post_init__(self) -> None:
        if not isinstance(self.bluetooth, BluetoothConfig):
            raise ValueError("bluetooth 必須是 BluetoothConfig。")
        interval = self.es5_retry_seconds
        if (type(self.es5_auto_connect) is not bool or type(interval) not in (int, float)
                or not 5 <= interval <= 3600 or not math.isfinite(interval)):
            raise ValueError("auto_connect 必須是布林值，retry_seconds 需介於 5～3600 秒。")
        if self.es5_auto_connect and self.bluetooth.backend == "esphome":
            self.bluetooth.direct_proxy()


def _object(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.keys() <= allowed:
        raise ValueError("設定區段格式或欄位名稱不正確。")
    return value


def load_transport_config(path: str | Path | None = None) -> TransportConfig:
    """Validate configuration without connecting a radio or network service.

    Source values never appear in validation errors: a malformed field may
    contain an API password or an accidentally selected credentials document.
    """
    if path is None:
        return TransportConfig()
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError("找不到連線設定檔；請先複製 examples/transport.example.json 並填入實際設定。")
    try:
        document = _object(json.loads(file.read_text(encoding="utf-8-sig")), {"version", "bluetooth", "es5"})
        if type(document.get("version")) is not int or document["version"] != 1:
            raise ValueError("version 必須為 1。")
        bluetooth = dict(_object(document.get("bluetooth", {}), {item.name for item in fields(BluetoothConfig)}))
        proxy_values = bluetooth.get("proxies", [])
        if not isinstance(proxy_values, list):
            raise ValueError("proxies 必須是陣列。")
        bluetooth["proxies"] = tuple(ProxyConfig(**_object(proxy, {item.name for item in fields(ProxyConfig)}))
                                     for proxy in proxy_values)
        transport = BluetoothConfig(**bluetooth)
        es5 = _object(document.get("es5", {}), {"auto_connect", "retry_seconds"})
        return TransportConfig(transport, es5.get("auto_connect", False), es5.get("retry_seconds", 30))
    except (ValueError, TypeError, KeyError, UnicodeError, RuntimeError, OverflowError):
        raise ValueError("無效的連線設定；請核對 version、bluetooth.proxies／gatt_proxy、API 加密金鑰與 es5 欄位。") from None


def public_transport(config: BluetoothConfig) -> dict[str, Any]:
    """Expose receiver names without host addresses, passwords, or API keys."""
    return {"backend": config.backend, "proxy_names": [proxy.name for proxy in config.proxies],
            "gatt_proxy": config.gatt_proxy or (config.proxies[0].name if len(config.proxies) == 1 else None)}
