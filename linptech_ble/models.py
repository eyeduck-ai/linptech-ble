"""JSON-safe BLE records. No account, authentication or GATT writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .config import normalize_address

MIBEACON_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
MODEL_IDS = {0x6A0F: "ES5BB", 0x1889: "MS1BB", 0x520B: "KS2BB", 0x50D4: "HS3BB"}


def canonical_uuid(value: str) -> str:
    value = str(value).lower()
    if len(value) == 4:
        value = f"0000{value}-0000-1000-8000-00805f9b34fb"
    return str(UUID(value))


@dataclass
class AdvertisementRecord:
    timestamp: str
    address: str
    name: str | None
    rssi: int
    service_data: dict[str, str]
    manufacturer_data: dict[str, str]
    service_uuids: list[str]
    tx_power: int | None = None

    @classmethod
    def from_bleak(cls, device: Any, adv: Any) -> AdvertisementRecord:
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            address=normalize_address(device.address),
            name=adv.local_name or device.name,
            rssi=adv.rssi,
            service_data={canonical_uuid(k): bytes(v).hex() for k, v in adv.service_data.items()},
            manufacturer_data={str(k): bytes(v).hex() for k, v in adv.manufacturer_data.items()},
            service_uuids=[canonical_uuid(k) for k in adv.service_uuids],
            tx_power=adv.tx_power,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AdvertisementRecord:
        if not isinstance(value, dict):
            raise ValueError("廣播紀錄必須是 JSON object。")
        try:
            required = {"timestamp", "address", "name", "rssi", "service_data", "manufacturer_data", "service_uuids"}
            if not required.issubset(value):
                raise ValueError("缺少原始廣播欄位。")
            timestamp = value["timestamp"]
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            name = value.get("name")
            if name is not None and not isinstance(name, str):
                raise ValueError("name 必須是字串或 null。")
            rssi = value["rssi"]
            if not isinstance(rssi, int) or isinstance(rssi, bool):
                raise ValueError("rssi 必須是整數。")
            service_data = {canonical_uuid(k): bytes.fromhex(v).hex() for k, v in value.get("service_data", {}).items()}
            manufacturer_data = {str(int(k)): bytes.fromhex(v).hex() for k, v in value.get("manufacturer_data", {}).items()}
            tx_power = value.get("tx_power")
            if tx_power is not None and (not isinstance(tx_power, int) or isinstance(tx_power, bool)):
                raise ValueError("tx_power 必須是整數或 null。")
            return cls(timestamp, normalize_address(value["address"]), name, rssi, service_data,
                       manufacturer_data, [canonical_uuid(k) for k in value.get("service_uuids", [])], tx_power)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            # Do not echo malformed input, which might be a mistaken secrets file.
            raise ValueError("無效的廣播紀錄；請確認輸入是 scan/monitor 產生的原始 JSONL。") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mibeacon_header(record: AdvertisementRecord) -> dict[str, Any] | None:
    """Read only the public header; flags do not prove payload authenticity."""
    raw = record.service_data.get(MIBEACON_UUID)
    if raw is None:
        return None
    data = bytes.fromhex(raw)
    if len(data) < 5:
        raise ValueError("MiBeacon header 少於 5 bytes。")
    frame = int.from_bytes(data[:2], "little")
    product_id = int.from_bytes(data[2:4], "little")
    return {
        "product_id": product_id,
        "product_id_hex": f"0x{product_id:04X}",
        "model": MODEL_IDS.get(product_id),
        "version": frame >> 12,
        "encrypted": bool(frame & 0x0008),
        "registered": bool(frame & 0x0100),
        "has_objects": bool(frame & 0x0040),
        "mesh": bool(frame & 0x0080),
        "packet_counter": data[4],
    }


def is_candidate(record: AdvertisementRecord) -> bool:
    name = (record.name or "").lower()
    if "linp" in name or any(model.lower() in name for model in MODEL_IDS.values()):
        return True
    try:
        header = mibeacon_header(record)
        return bool(header and header["model"])
    except ValueError:
        return False
