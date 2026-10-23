"""Import only the requested Linptech devices from the author's local export."""
from __future__ import annotations

import json
from pathlib import Path

from .config import DeviceConfig, load_config, save_config

MODEL_ALIASES = {
    "linp.sensor_occupy.es5b": "ES5BB",
    "linp.magnet.m1": "MS1BB",
    "linp.sensor_ht.ks2bb": "KS2BB",
    "linp.motion.hs1bb2": "HS3BB",
}


def import_keys(source: str | Path, destination: str | Path = "sensors.local.json", *, include_tokens: bool = False) -> dict:
    """Merge matching devices; return a summary that never contains keys/tokens."""
    try:
        payload = json.loads(Path(source).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list):
            raise ValueError()
        imported: dict[str, DeviceConfig] = {}
        missing_keys: list[dict] = []
        for region in payload:
            for home in region.get("homes", []):
                for row in home.get("devices", []):
                    model = MODEL_ALIASES.get(row.get("model"))
                    if model is None:
                        continue
                    key = (row.get("BLE_DATA") or {}).get("beaconkey")
                    if not key:
                        missing_keys.append({"model": model, "address": row.get("mac")})
                        continue
                    token = row.get("token") if include_tokens else None
                    # Some export formats contain cloud tokens of another size;
                    # only a 12-byte Standard Auth token is usable here.
                    if model != "ES5BB" and token and (not isinstance(token, str) or len(token) != 24):
                        token = None
                    item = DeviceConfig(row["mac"], model, row.get("name") or model, key, token or None,
                                        miot_model=row["model"])
                    previous = imported.get(item.address)
                    if previous and (previous.model != item.model or previous.bindkey != item.bindkey or previous.auth_token != item.auth_token):
                        raise ValueError()
                    imported[item.address] = item
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # Export files contain control credentials: never echo their contents.
        raise ValueError("匯出檔格式不符、資料衝突或感測器密鑰無效；未變更設定。") from exc
    if not imported:
        raise ValueError("匯出檔中沒有這四款感測器可用的 BLE KEY；未變更設定。")
    existing = {item.address: item for item in load_config(destination)}
    for address, item in imported.items():
        if address in existing and existing[address].model != item.model:
            raise ValueError("相同 MAC 的既有型號與匯入資料不符；未變更設定。")
        if address in existing and item.auth_token is None:
            item.auth_token = existing[address].auth_token
    existing.update(imported)
    save_config(destination, list(existing.values()))
    return {
        "imported": [{"model": item.model, "address": item.address, "key_present": True,
                      "auth_token_present": bool(item.auth_token)}
                     for item in imported.values()],
        "missing_keys": missing_keys,
        "config": str(destination),
    }
