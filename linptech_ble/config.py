"""Local device configuration. Bindkeys never appear in repr or validation errors."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

MODELS = {"ES5BB", "MS1BB", "KS2BB", "HS3BB"}
MIOT_MODELS = {"ES5BB": "linp.sensor_occupy.es5b", "MS1BB": "linp.magnet.m1",
               "KS2BB": "linp.sensor_ht.ks2bb", "HS3BB": "linp.motion.hs1bb2"}


def normalize_address(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("MAC 位址必須是字串。")
    value = value.strip().upper().replace("-", ":")
    if re.fullmatch(r"[0-9A-F]{12}", value):
        value = ":".join(value[i:i + 2] for i in range(0, 12, 2))
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value):
        raise ValueError("MAC 位址格式應為 AA:BB:CC:DD:EE:FF。")
    return value


def validate_bindkey(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value.strip()):
        raise ValueError("bindkey 必須是 32 個十六進位字元（16 bytes）；不能使用 TOKEN 代替。")
    return value.strip().lower()


def validate_auth_token(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{24}", value.strip()):
        raise ValueError("直連 token 必須是 24 個十六進位字元（12 bytes），與廣播 bindkey 不同。")
    return value.strip().lower()


@dataclass
class DeviceConfig:
    address: str
    model: str
    name: str
    bindkey: str | None = field(default=None, repr=False)
    auth_token: str | None = field(default=None, repr=False)
    miot_model: str | None = None

    def __post_init__(self) -> None:
        self.address = normalize_address(self.address)
        if self.model not in MODELS:
            raise ValueError("model 必須是 ES5BB、MS1BB、KS2BB 或 HS3BB。")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name 必須是非空白字串。")
        if self.bindkey is not None:
            self.bindkey = validate_bindkey(self.bindkey)
        if self.auth_token is not None:
            self.auth_token = validate_auth_token(self.auth_token)
        if self.miot_model is not None and self.miot_model != MIOT_MODELS[self.model]:
            raise ValueError("miot_model 與此感測器型號不符。")


def load_config(path: str | Path) -> list[DeviceConfig]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("devices"), list):
            raise ValueError("需要 version: 1 與 devices 陣列。")
        devices = [DeviceConfig(**item) for item in payload["devices"]]
        if len({item.address for item in devices}) != len(devices):
            raise ValueError("設定檔的 MAC 位址不得重複。")
        return devices
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("無效的設定檔；請檢查 version、devices、MAC、model、32 字元 bindkey 與可選的 24 字元直連 token。") from exc


def save_config(path: str | Path, devices: list[DeviceConfig]) -> None:
    path = Path(path)
    if len({item.address for item in devices}) != len(devices):
        raise ValueError("設定檔的 MAC 位址不得重複。")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"version": 1, "devices": [asdict(item) for item in devices]}, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".sensors-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
