"""Model boundaries and complete authenticated broadcast object validation."""
from __future__ import annotations

from dataclasses import replace
import json
import struct

import pytest

from linptech_ble.broadcast_objects import object_catalog
from linptech_ble.config import DeviceConfig
from linptech_ble.decoder import Decoder
from linptech_ble.models import MIBEACON_UUID

from conftest import ADDRESS, TEST_KEY, object_bytes


def decode(make_record, model, objects, **options):
    return Decoder([DeviceConfig(ADDRESS, model, "Synthetic", TEST_KEY)]).decode(make_record(model, objects, **options))


def values(result):
    return {item["key"]: item["value"] for item in result["measurements"]}


def binary(result):
    return {item["key"]: item["value"] for item in result["binary_sensors"]}


def test_capability_catalog_distinguishes_capture_spec_identity_and_compatibility():
    catalog = object_catalog()
    assert set(catalog) == {"ES5BB", "MS1BB", "KS2BB", "HS3BB"}
    assert all(obj["object_id_hex"] == f"0x{obj['object_id']:04X}" for entries in catalog.values() for obj in entries)
    assert all(obj["evidence"] == "observed_capture" for obj in catalog["ES5BB"] if obj["object_id"] == 0x5C03)
    assert next(o for o in catalog["KS2BB"] if o["object_id"] == 0x4801)["miot"] == {"siid": 2, "piid": 1001}
    compatibility = next(o for o in catalog["KS2BB"] if o["object_id"] == 0x4C08)
    assert compatibility["evidence"] == "upstream_compatibility_only" and "miot" not in compatibility
    assert all(o.get("spec_mapping") == "confirmed_on_tested_device" for o in catalog["HS3BB"])
    assert not any(o["key"] in {"pir", "pir_energy", "motion_energy", "detect_region"} for o in catalog["ES5BB"])
    assert object_catalog("unknown") == {}


@pytest.mark.parametrize("model,object_id,payload,key,expected", [
    ("KS2BB", 0x4801, struct.pack("<f", 28.4), "temperature", 28.4),
    ("KS2BB", 0x4802, b"\x44", "humidity", 68),
    ("ES5BB", 0x4805, struct.pack("<f", 10), "illuminance", 10),
    ("ES5BB", 0x5C03, b"\x5b", "battery", 91),
    ("MS1BB", 0x4C03, b"\x64", "battery", 100),
    ("HS3BB", 0x4C03, b"\x64", "battery", 100),
    ("HS3BB", 0x4818, b"\x78\x00", "no_motion_duration", 120),
    ("HS3BB", 0x4835, b"\x0a\x00", "custom_no_motion_time", 10),
])
def test_independently_encrypted_captured_wire_shapes(make_record, model, object_id, payload, key, expected):
    result = decode(make_record, model, object_bytes(object_id, payload))
    assert result["status"] == "decoded" and result["bindkey_verified"]
    assert values(result)[key] == pytest.approx(expected)
    obj = result["objects"][0]
    assert obj["evidence"] == "observed_capture" and obj["status"] == "decoded"
    assert obj["raw_hex"] == payload.hex() and obj["length"] == len(payload)


@pytest.mark.parametrize("object_id,event_type", [(0x560C, "press"), (0x560D, "double_press"), (0x560E, "long_press")])
def test_captured_zero_length_ks2_button_events(make_record, object_id, event_type):
    result = decode(make_record, "KS2BB", object_bytes(object_id, b""))
    assert result["status"] == "decoded"
    assert result["events"] == [{"key": "button", "device_id": None, "event_type": event_type, "event_properties": None}]
    assert result["objects"][0]["miot"] == {"siid": 5, "eiid": {0x560C: 1012, 0x560D: 1013, 0x560E: 1014}[object_id]}


def test_multiple_distinct_button_events_preserve_wire_order_and_deduplicate_packet(make_record):
    decoder = Decoder([DeviceConfig(ADDRESS, "KS2BB", "Synthetic", TEST_KEY)])
    record = make_record("KS2BB", object_bytes(0x560C, b"") + object_bytes(0x560E, b""))
    assert [event["event_type"] for event in decoder.decode(record)["events"]] == ["press", "long_press"]
    duplicate = decoder.decode(record)
    assert duplicate["status"] == "duplicate" and duplicate["events"] == []


@pytest.mark.parametrize("object_id,key", [(0x4851, "duration_detected"), (0x4852, "duration_cleared")])
def test_es5_duration_has_explicit_minutes_and_unknown_enums_stay_raw(make_record, object_id, key):
    result = decode(make_record, "ES5BB", object_bytes(object_id, b"\x78"))
    measurement = next(item for item in result["measurements"] if item["key"] == key)
    assert measurement["value"] == 120 and measurement["unit"] == "min"
    assert result["objects"][0]["evidence"] == "protocol_only"
    unknown = decode(make_record, "ES5BB", object_bytes(object_id, b"\x04"))
    assert unknown["status"] == "no_measurement" and unknown["measurements"] == []
    assert unknown["objects"][0]["status"] == "unknown_enum" and unknown["objects"][0]["value"] == 4


@pytest.mark.parametrize("object_id", [0x484E, 0x5413])
def test_unknown_occupancy_enum_does_not_become_people_detected(make_record, object_id):
    payload = object_bytes(object_id, b"\xff") + object_bytes(0x4805, struct.pack("<f", 0))
    result = decode(make_record, "ES5BB", payload)
    assert result["status"] == "decoded" and result["binary_sensors"] == []
    assert values(result) == {"illuminance": 0}
    assert result["objects"][0]["status"] == "unknown_enum"


def test_unknown_object_keeps_structured_diagnostic_and_known_measurement(make_record):
    payload = object_bytes(0x7FFE, b"\x12\x34") + object_bytes(0x4802, b"\x44")
    result = decode(make_record, "KS2BB", payload)
    assert result["status"] == "decoded" and values(result) == {"humidity": 68}
    assert result["objects"][0] == {"object_id": 0x7FFE, "object_id_hex": "0x7FFE", "length": 2,
                                    "raw_hex": "1234", "status": "unknown_object"}
    only_unknown = decode(make_record, "KS2BB", object_bytes(0x7FFE, b"\x12\x34"))
    assert only_unknown["status"] == "no_measurement" and only_unknown["objects"]


def test_captured_hs3_zero_timeout_is_preserved_without_inventing_zero_minutes(make_record):
    payload = object_bytes(0x4835, b"\x00\x00") + object_bytes(0x4C03, b"\x64")
    result = decode(make_record, "HS3BB", payload)
    assert values(result) == {"battery": 100}
    assert result["objects"][0]["status"] == "outside_spec_range" and result["objects"][0]["value"] == 0
    assert result["objects"][0]["unit"] == "min"


@pytest.mark.parametrize("duration,expected", [(0, True), (30, True), (31, False), (120, False), (65535, False)])
def test_hs3_motion_derivation_preserves_reported_duration(make_record, duration, expected):
    result = decode(make_record, "HS3BB", object_bytes(0x4818, struct.pack("<H", duration)))
    assert values(result)["no_motion_duration"] == duration
    assert binary(result)["motion"] is expected
    assert result["objects"][0]["miot"] == {"siid": 2, "piid": 1024}


@pytest.mark.parametrize("model,object_id", [("KS2BB", 0x561C), ("HS3BB", 0x4A1C)])
def test_reset_events_have_no_fabricated_sensor_values(make_record, model, object_id):
    result = decode(make_record, model, object_bytes(object_id, b""))
    assert result["status"] == "decoded" and result["measurements"] == [] and result["binary_sensors"] == []
    assert result["events"][0]["key"] == "device" and result["events"][0]["event_type"] == "device_reset"


def test_es5_special_event_retains_original_enum_and_unknown_value(make_record):
    result = decode(make_record, "ES5BB", object_bytes(0x5661, b"\x06"))
    assert result["events"][0]["event_type"] == "radar_noise_critical"
    assert result["objects"][0]["miot"] == {"siid": 5, "eiid": 1097}
    unknown = decode(make_record, "ES5BB", object_bytes(0x5661, b"\xff"))
    assert unknown["events"] == [] and unknown["objects"][0]["value"] == 255
    assert unknown["objects"][0]["status"] == "unknown_enum"


def test_ms1_close_conflict_never_creates_a_button_event(make_record):
    conflict = decode(make_record, "MS1BB", object_bytes(0x4A13, b"\x01"))
    assert conflict["events"] == [] and conflict["binary_sensors"] == []
    assert conflict["objects"][0]["status"] == "unknown_enum"
    closed = decode(make_record, "MS1BB", object_bytes(0x4A13, b"\x02"))
    assert binary(closed)["opening"] is False and closed["events"] == []
    assert closed["objects"][0]["miot"] == {"siid": 2, "eiid": 1019}


@pytest.mark.parametrize("model,object_id,value", [
    ("KS2BB", 0x4801, struct.pack("<f", float("nan"))),
    ("KS2BB", 0x4801, struct.pack("<f", float("inf"))),
    ("KS2BB", 0x4802, b"\xff"),
    ("KS2BB", 0x5003, b"\x65"),
    ("MS1BB", 0x4C03, b"\xff"),
    ("HS3BB", 0x4C03, b"\x65"),
    ("HS3BB", 0x4805, struct.pack("<f", -1)),
    ("ES5BB", 0x4805, struct.pack("<f", 1001)),
    ("HS3BB", 0x4835, b"\x3d\x00"),
])
def test_invalid_known_values_reject_entire_authenticated_packet(make_record, model, object_id, value):
    # Put a valid object first to exercise atomicity rather than single-value rejection.
    valid = object_bytes(0x484E, b"\x01") if model == "ES5BB" else object_bytes(0x4C03 if model != "KS2BB" else 0x5003, b"\x64")
    result = decode(make_record, model, valid + object_bytes(object_id, value))
    assert result["status"] == "parse_error"
    assert result["measurements"] == result["binary_sensors"] == result["events"] == result["objects"] == []
    assert TEST_KEY not in json.dumps(result)


@pytest.mark.parametrize("model,object_id", [("KS2BB", 0x4802), ("HS3BB", 0x4818), ("MS1BB", 0x4804), ("ES5BB", 0x484E)])
def test_known_object_length_is_exact_not_silently_ignored(make_record, model, object_id):
    result = decode(make_record, model, object_bytes(object_id, b"\x01\x02\x03"))
    assert result["status"] == "parse_error" and result["objects"] == []


def test_model_scope_prevents_cross_model_generic_object_interpretation(make_record):
    result = decode(make_record, "KS2BB", object_bytes(0x484E, b"\x01"))
    assert result["measurements"] == result["binary_sensors"] == result["events"] == []
    assert result["objects"][0]["status"] == "unknown_object"


def test_extensions_require_authentication_and_corrupt_mic_has_no_diagnostics(make_record):
    plaintext = decode(make_record, "ES5BB", object_bytes(0x4851, b"\x01"), encrypted=False)
    assert plaintext["measurements"] == [] and plaintext["objects"][0]["status"] == "unauthenticated"
    record = make_record("HS3BB", object_bytes(0x4835, b"\x0a\x00"))
    data = bytearray.fromhex(record.service_data[MIBEACON_UUID])
    data[-1] ^= 1
    corrupted = replace(record, service_data={MIBEACON_UUID: data.hex()})
    result = Decoder([DeviceConfig(ADDRESS, "HS3BB", "Synthetic", TEST_KEY)]).decode(corrupted)
    assert result["status"] == "decryption_failed" and result["objects"] == []


def test_hs3_support_does_not_mutate_upstream_device_registry(make_record):
    from xiaomi_ble.devices import DEVICE_TYPES
    before = dict(DEVICE_TYPES)
    result = decode(make_record, "HS3BB", object_bytes(0x4818, b"\x00\x00"))
    assert result["status"] == "decoded" and DEVICE_TYPES == before
