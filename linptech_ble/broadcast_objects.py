"""Model-scoped, authenticated MiBeacon object definitions and diagnostics.

Wire facts come from the official MIoT instances and xiaomi-ble 1.17.0's
object handlers. An MIoT property existing in a GATT service is deliberately
not sufficient to add it here. Captured IDs below were independently decrypted
and authenticated before identifying their model/length; no captures or keys
are embedded in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Any


UPSTREAM = "https://github.com/Bluetooth-Devices/xiaomi-ble/blob/v1.17.0/src/xiaomi_ble/parser.py"
SPEC_BASE = "https://miot-spec.org/miot-spec-v2/instance?type="
SPEC_TYPES = {
    "ES5BB": "urn:miot-spec-v2:device:occupancy-sensor:0000A0BF:linp-es5b:1",
    "MS1BB": "urn:miot-spec-v2:device:magnet-sensor:0000A016:linp-m1:1",
    "KS2BB": "urn:miot-spec-v2:device:temperature-humidity-sensor:0000A00A:linp-ks2bb:1",
    # Cloud identity and authenticated GATT confirmed this mapping on the
    # tested device. Other devices still require their own model confirmation.
    "HS3BB": "urn:miot-spec-v2:device:motion-sensor:0000A014:linp-hs1bb2:1",
}
OBSERVED = {
    "ES5BB": {0x4805, 0x484E, 0x5413, 0x5C03},
    "MS1BB": {0x4804, 0x4A12, 0x4C03},
    "KS2BB": {0x4801, 0x4802, 0x560C, 0x560D, 0x560E},
    "HS3BB": {0x4805, 0x4818, 0x4835, 0x4A08, 0x4C03},
}


@dataclass(frozen=True)
class BroadcastObject:
    object_id: int
    key: str
    label: str
    format: str
    lengths: tuple[int, ...]
    handler: str | None = None
    unit: str | None = None
    siid: int | None = None
    piid: int | None = None
    eiid: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[tuple[int, str], ...] = ()
    extension: bool = False
    compatibility: bool = False


O = BroadcastObject
DURATIONS = tuple((value, f"{value} minutes") for value in (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120))
ES5_EVENTS = ((0, "device_reset"), (1, "off_light_leave"), (2, "off_light_exit"), (3, "off_light_sleep"),
              (4, "off_light_exit_4"), (5, "radar_noise_minor"), (6, "radar_noise_critical"),
              (7, "radar_noise_normal"), (8, "radar_noise_error"))
CATALOG = {
    "ES5BB": [
        O(0x4805, "illuminance", "照度", "float", (4,), "obj4805", "lx", 2, 1005, minimum=0, maximum=1000),
        O(0x484E, "occupancy", "人體存在", "uint8", (1,), "obj484e", siid=2, piid=1078, choices=((0, "vacant"), (1, "occupied"))),
        O(0x5413, "occupancy_close_range", "近距區間", "uint8", (1,), "obj5413", siid=5, piid=1019, choices=((0, "vacant"), (1, "occupied"))),
        O(0x5C03, "battery", "電量", "uint8", (1,), unit="%", siid=7, piid=1003, minimum=0, maximum=100, extension=True),
        O(0x4851, "duration_detected", "持續有人時間", "uint8", (1,), "obj4851", "min", 2, 1081, choices=DURATIONS, extension=True),
        O(0x4852, "duration_cleared", "持續無人時間", "uint8", (1,), "obj4852", "min", 2, 1082, choices=DURATIONS, extension=True),
        O(0x5661, "es5_event", "ES5BB 特殊事件", "uint8", (1,), siid=5, eiid=1097, choices=ES5_EVENTS, extension=True),
    ],
    "MS1BB": [
        O(0x4804, "opening", "門窗狀態", "uint8", (1,), "obj4804", siid=2, piid=1004, choices=((1, "open"), (2, "closed"))),
        O(0x4A12, "opening", "門窗事件", "uint8", (1,), "obj4a12", siid=2, eiid=1018, choices=((1, "open"), (2, "closed"))),
        # The official close event conflicts with the upstream button comment.
        # Only its unambiguous closed status 2 is normalized; 1 remains raw.
        O(0x4A13, "opening", "關閉事件（規格與上游註解有差異）", "uint8", (1,), "obj4a12", siid=2, eiid=1019, choices=((2, "closed"),), extension=True),
        O(0x4A0F, "device_forcibly_removed", "防拆事件", "uint8", (1,), "obj4a0f", siid=2, eiid=1015, choices=((1, "triggered"),), extension=True),
        O(0x4A1A, "door_left_open", "門未關事件", "uint8", (1,), "obj4a1a", siid=2, eiid=1026, choices=((1, "triggered"),), extension=True),
        O(0x4C03, "battery", "電量", "uint8", (1,), "obj4c03", "%", 3, 1003, minimum=0, maximum=100),
    ],
    "KS2BB": [
        O(0x4801, "temperature", "溫度", "float", (4,), "obj4801", "°C", 2, 1001, minimum=-30, maximum=100),
        O(0x4802, "humidity", "濕度", "uint8", (1,), "obj4802", "%", 2, 1002, minimum=0, maximum=99),
        # Old parser-compatible object variants are not asserted to be the
        # current firmware's MIoT service/property mapping or observed data.
        O(0x4C01, "temperature", "溫度（相容物件）", "float", (4,), "obj4c01", "°C", minimum=-30, maximum=100, compatibility=True),
        O(0x4C08, "humidity", "濕度（相容物件）", "float", (4,), "obj4c08", "%", minimum=0, maximum=100, compatibility=True),
        O(0x5003, "battery", "電量", "uint8", (1,), "obj5003", "%", 4, 1003, minimum=0, maximum=100),
        O(0x560C, "button", "單擊", "event", (0, 1), "obj560c", siid=5, eiid=1012),
        O(0x560D, "button", "雙擊", "event", (0, 1), "obj560d", siid=5, eiid=1013),
        O(0x560E, "button", "長按", "event", (0, 1), "obj560e", siid=5, eiid=1014),
        O(0x561C, "device", "裝置重設", "event", (0,), siid=5, eiid=1028, extension=True),
    ],
    "HS3BB": [
        O(0x4805, "illuminance", "照度", "float", (4,), "obj4805", "lx", 2, 1005, minimum=0, maximum=1000),
        O(0x4818, "no_motion_duration", "無移動持續時間", "uint16", (2,), "obj4818", "s", 2, 1024),
        O(0x4835, "custom_no_motion_time", "自訂無人時間", "uint16", (2,), unit="min", siid=2, piid=1053, minimum=1, maximum=60, extension=True),
        O(0x4A08, "motion", "移動與照度事件", "float", (4,), "obj4a08", "lx", 2, eiid=1008, minimum=0, maximum=1000),
        O(0x4C03, "battery", "電量", "uint8", (1,), "obj4c03", "%", 3, 1003, minimum=0, maximum=100),
        O(0x4A1C, "device", "裝置重設", "event", (0,), siid=2, eiid=1028, extension=True),
    ],
}
OBJECTS = {model: {obj.object_id: obj for obj in entries} for model, entries in CATALOG.items()}
# Even when a known float object is sent under the wrong model, a malformed
# value must not slip through as a harmless unknown alongside valid occupancy.
LENGTHS = {obj.object_id: obj.lengths for entries in CATALOG.values() for obj in entries}


def object_catalog(model: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Public capability inventory; observed_capture is evidence, not live state."""
    models = [model] if model in OBJECTS else list(OBJECTS) if model is None else []
    return {name: [_metadata(name, obj, b"") for obj in CATALOG[name]] for name in models}


def _metadata(model: str, obj: BroadcastObject, value: bytes) -> dict[str, Any]:
    result = {"object_id": obj.object_id, "object_id_hex": f"0x{obj.object_id:04X}",
              "key": obj.key, "label": obj.label, "format": obj.format, "length": len(value),
              "expected_lengths": list(obj.lengths), "raw_hex": value.hex(), "unit": obj.unit,
              "kind": "event" if obj.eiid else "property", "source": UPSTREAM,
              "evidence": "observed_capture" if obj.object_id in OBSERVED[model] else "protocol_only"}
    if obj.siid is not None:
        result["miot"] = {"siid": obj.siid, **({"piid": obj.piid} if obj.piid else {"eiid": obj.eiid})}
        result["spec_source"] = SPEC_BASE + SPEC_TYPES[model]
        if obj.extension:
            result["source"] = result["spec_source"]
        if model == "HS3BB":
            result["spec_mapping"] = "confirmed_on_tested_device"
    if model == "MS1BB" and obj.object_id == 0x4A12:
        result["mapping_note"] = "實收此事件 ID 同時攜帶開啟與關閉值；保留狀態值，不僅依事件名稱判斷。"
    if model == "MS1BB" and obj.object_id == 0x4A13:
        result["mapping_note"] = "官方定義為關閉事件；上游註解稱按鍵。未實收，不將值 1 轉為按鍵。"
    if obj.choices:
        result["choices"] = [{"value": key, "label": label} for key, label in obj.choices]
    if obj.minimum is not None:
        result["minimum"], result["maximum"] = obj.minimum, obj.maximum
    if obj.compatibility:
        result["evidence"] = "upstream_compatibility_only"
    return result


def decode_objects(model: str, objects: list[tuple[int, bytes]], *, authenticated: bool) -> dict[str, Any]:
    """Validate the whole packet first; then choose only model-specific handlers.

    Unknown objects/enums are diagnostics rather than coerced booleans. Invalid
    known wire lengths/non-finite numbers/value ranges abort the entire packet.
    HS3's captured zero custom timeout is a known spec exception and stays raw.
    """
    result: dict[str, Any] = {"objects": [], "handlers": [], "measurements": [], "events": []}
    for object_id, raw in objects:
        if object_id in LENGTHS and len(raw) not in LENGTHS[object_id]:
            raise ValueError("Invalid known broadcast object length")
        obj = OBJECTS[model].get(object_id)
        if obj is None:
            result["objects"].append({"object_id": object_id, "object_id_hex": f"0x{object_id:04X}",
                "length": len(raw), "raw_hex": raw.hex(), "status": "unknown_object"})
            continue
        meta = _metadata(model, obj, raw)
        result["objects"].append(meta)
        value = struct.unpack("<f", raw)[0] if obj.format == "float" else int.from_bytes(raw, "little") if obj.format != "event" else None
        meta["value"] = value
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Non-finite broadcast measurement")
        if obj.extension and not authenticated:
            meta["status"] = "unauthenticated"
            continue
        if obj.choices and value not in dict(obj.choices):
            meta["status"] = "unknown_enum"
            continue
        if obj.minimum is not None and not obj.minimum <= value <= obj.maximum:
            if model == "HS3BB" and object_id == 0x4835 and value == 0:
                meta["status"] = "outside_spec_range"
                continue
            raise ValueError("Broadcast measurement outside specified range")
        meta["status"] = "decoded"
        if obj.choices:
            meta["value_label"] = dict(obj.choices)[value]
        if model == "KS2BB" and object_id in {0x560C, 0x560D, 0x560E} and raw not in {b"", b"\x00", b"\x01"}:
            # Published KS2 events have no arguments; captured events are zero
            # length. Keep only upstream-compatible 0/1 padding, not arbitrary
            # new argument values interpreted as a user press.
            meta["status"] = "unknown_enum"
            continue
        if obj.handler and not (model == "KS2BB" and obj.key == "button"):
            result["handlers"].append((obj.handler, raw))
        if model == "KS2BB" and obj.key == "button":
            result["events"].append({"key": "button", "device_id": None,
                "event_type": {0x560C: "press", 0x560D: "double_press", 0x560E: "long_press"}[object_id],
                "event_properties": None})
        if (model == "ES5BB" and object_id == 0x5C03) or (model == "HS3BB" and object_id in {0x4818, 0x4835}):
            result["measurements"].append({"key": obj.key, "device_id": None, "value": value, "unit": obj.unit})
        if obj.key == "es5_event" or obj.key == "device":
            event_type = dict(obj.choices)[value] if obj.choices else "device_reset"
            result["events"].append({"key": obj.key, "device_id": None, "event_type": event_type,
                                     "event_properties": {"object_id": meta["object_id_hex"], **({"value": value} if value is not None else {})}})
    return result
