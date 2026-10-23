"""MiBeacon decoding through xiaomi-ble, with explicit failure states."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
from math import isfinite
from struct import error as StructError
import threading
from typing import Any

from .config import DeviceConfig
from .broadcast_objects import decode_objects
from .decoded import DecodedAdvertisement
from .models import AdvertisementRecord, MIBEACON_UUID, mibeacon_header


def json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return json_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return json_value(float(value))
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


class Decoder:
    def __init__(self, devices: list[DeviceConfig] | None = None) -> None:
        self.devices = {device.address: device for device in devices or []}
        # Bounded cache: advertisements can repeat a button event on the radio.
        self._seen: OrderedDict[tuple[str, str, bytes], float] = OrderedDict()
        self._lock = threading.RLock()

    def decode(self, record: AdvertisementRecord) -> DecodedAdvertisement:
        """Decode atomically across receiver callbacks, preserving their metadata.

        Identical packets share one event-deduplication cache across receivers.
        Keep one decoder per credential set; separate instances have no shared
        state. The cache is bounded and is not persistent exactly-once delivery.
        """
        with self._lock:
            return self._decode(record)

    def _decode(self, record: AdvertisementRecord) -> DecodedAdvertisement:
        configured = self.devices.get(record.address)
        bindkey = configured.bindkey if configured else None
        result: DecodedAdvertisement = {
            "timestamp": record.timestamp,
            "address": record.address,
            "name": configured.name if configured else record.name,
            "rssi": record.rssi,
            "receiver": record.receiver,
            "model": None,
            "status": "no_mibeacon",
            "header": None,
            "measurements": [],
            "binary_sensors": [],
            "events": [],
            "unhandled": {},
            "objects": [],
            "bindkey_verified": False,
        }
        try:
            header = mibeacon_header(record)
        except (ValueError, TypeError):
            result.update({"status": "malformed", "reason": "Invalid MiBeacon header"})
            return result
        if header is None:
            return result
        result.update({"header": header, "model": header["model"]})

        # Lazy imports leave scan/header inspection usable without decoder packages.
        from home_assistant_bluetooth import BluetoothServiceInfo
        from xiaomi_ble import XiaomiBluetoothDeviceData
        if header["model"] is None:
            result.update({"status": "unsupported_model", "reason": "Product ID is not in xiaomi-ble; raw capture is required"})
            return result
        if header["mesh"] or header["version"] < 2:
            result.update({"status": "unsupported_protocol", "reason": "Mesh or legacy MiBeacon format is not supported"})
            return result
        if header["encrypted"] and header["version"] < 4:
            result.update({"status": "unsupported_protocol", "reason": "Authenticated MiBeacon V4/V5 is required for encrypted objects"})
            return result
        if configured and configured.model != header["model"]:
            result.update({"status": "model_mismatch", "reason": "Configured model does not match advertised product ID"})
            return result

        raw = record.service_data[MIBEACON_UUID]
        data = bytes.fromhex(raw)
        offset = 5
        frame = int.from_bytes(data[:2], "little")
        if frame & 0x10:
            offset += 6
            if len(data) < offset or data[5:11][::-1] != bytes.fromhex(record.address.replace(":", "")):
                result.update({"status": "malformed", "reason": "Truncated frame or embedded MAC differs from scanner address"})
                return result
        if frame & 0x20:
            if len(data) <= offset:
                result.update({"status": "malformed", "reason": "Missing capability byte"})
                return result
            capability = data[offset]
            offset += 1 + bool(capability & 0x20)
            if len(data) < offset:
                result.update({"status": "malformed", "reason": "Missing extended capability byte"})
                return result
        if not header["has_objects"]:
            result["status"] = "no_measurement"
            return result
        if len(data) < offset + (9 if header["encrypted"] else 3):
            result.update({"status": "malformed", "reason": "Truncated object payload"})
            return result
        if header["encrypted"] and not bindkey:
            result["status"] = "encrypted_no_key"
            return result

        try:
            packet_time = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00")).timestamp()
            # Receiver names/RSSI must not split one button event. Credential
            # scope prevents a replaced key from inheriting old MIC verification.
            key_bytes = bytes.fromhex(bindkey or "") if header["encrypted"] else b""
            scope = hashlib.sha256(key_bytes).digest() if header["encrypted"] else b""
            fingerprint = (record.address, raw, scope)
            previous = self._seen.get(fingerprint)
            if previous is not None and packet_time <= previous + 5:
                # Late deliveries must not replay an event or move its receive
                # high-water mark backwards. Every duplicate still retains its
                # own receiver, RSSI and timestamp in the result above.
                self._seen[fingerprint] = max(previous, packet_time)
                self._seen.move_to_end(fingerprint)
                result["status"] = "duplicate"
                result["bindkey_verified"] = bool(header["encrypted"])
                return result
            # A fresh parser yields values from this packet only. In particular a
            # bad key, unknown object, or battery packet cannot reuse old motion.
            payload = data[offset:]
            if header["encrypted"] and header["version"] >= 4:
                from cryptography.exceptions import InvalidTag
                from cryptography.hazmat.primitives.ciphers.aead import AESCCM

                # The upstream parser can silently retain the first values of a
                # truncated TLV stream. Validate the whole authenticated stream
                # before letting any of its values become a successful result.
                nonce = bytes.fromhex(record.address.replace(":", ""))[::-1] + data[2:5] + data[-7:-4]
                try:
                    payload = AESCCM(key_bytes, tag_length=4).decrypt(
                        nonce, data[offset:-7] + data[-4:], b"\x11"
                    )
                except InvalidTag:
                    result["status"] = "decryption_failed"
                    return result
                result["bindkey_verified"] = True
            objects = []
            if not header["encrypted"] or header["version"] >= 4:
                cursor = 0
                while cursor < len(payload):
                    if cursor + 3 > len(payload):
                        result.update({"status": "malformed", "reason": "Truncated object header"})
                        return result
                    end = cursor + 3 + payload[cursor + 2]
                    if end > len(payload):
                        result.update({"status": "malformed", "reason": "Truncated object value"})
                        return result
                    objects.append((int.from_bytes(payload[cursor:cursor + 2], "little"), payload[cursor + 3:end]))
                    cursor = end
            decoded = decode_objects(header["model"], objects, authenticated=result["bindkey_verified"])
            info = BluetoothServiceInfo(
                name=record.name or record.address,
                address=record.address,
                rssi=record.rssi,
                manufacturer_data={int(k): bytes.fromhex(v) for k, v in record.manufacturer_data.items()},
                service_data={k: bytes.fromhex(v) for k, v in record.service_data.items()},
                service_uuids=record.service_uuids,
                source=record.receiver,
            )
            # Reuse upstream field/event conversion on a fresh local parser,
            # after our own complete MIC/TLV/type/range validation. The local
            # override does not touch DEVICE_TYPES or any upstream globals; it
            # also prevents generic object IDs being interpreted across models.
            from xiaomi_ble import parser as upstream
            model = header["model"]

            class PacketParser(XiaomiBluetoothDeviceData):
                def _start_update(self, _info):
                    self.set_device_type(model)
                    for handler, value in decoded["handlers"]:
                        self.unhandled.update(getattr(upstream, handler)(value, self, model))

            parser = PacketParser()
            update = parser.update(info)
            for key, value in update.entity_values.items():
                if key.key == "signal_strength":
                    continue
                description = update.entity_descriptions.get(key)
                result["measurements"].append({
                    "key": str(key.key), "device_id": key.device_id,
                    "value": json_value(value.native_value),
                    "unit": json_value(description.native_unit_of_measurement) if description else None,
                })
            for key, binary in update.binary_entity_values.items():
                result["binary_sensors"].append({
                    "key": str(key.key), "device_id": key.device_id,
                    "value": binary.native_value,
                })
            result["measurements"].extend(decoded["measurements"])
            for key, event in update.events.items():
                result["events"].append({
                    "key": str(key.key), "device_id": key.device_id,
                    "event_type": event.event_type,
                    "event_properties": json_value(event.event_properties),
                })
            result["unhandled"] = json_value(parser.unhandled)
            result["events"].extend(decoded["events"])
            result["objects"] = decoded["objects"]
            result["status"] = "decoded" if any((result["measurements"], result["binary_sensors"], result["events"], result["unhandled"])) else "no_measurement"
            self._seen[fingerprint] = packet_time
            self._seen.move_to_end(fingerprint)
            if len(self._seen) > 2048:
                self._seen.popitem(last=False)
            return result
        except (ValueError, IndexError, KeyError, TypeError, AssertionError, OverflowError, StructError) as exc:
            result.update({"status": "parse_error", "reason": type(exc).__name__,
                           "measurements": [], "binary_sensors": [], "events": [], "unhandled": {}, "objects": []})
            return result
