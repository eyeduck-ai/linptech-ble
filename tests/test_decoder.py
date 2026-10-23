"""Behavioral coverage using locally encrypted, synthetic advertisements only."""

from __future__ import annotations

import json
import struct
from dataclasses import replace

import pytest

from linptech_ble.config import DeviceConfig
from linptech_ble.decoder import Decoder
from linptech_ble.models import MIBEACON_UUID

from conftest import ADDRESS, TEST_KEY, object_bytes


def decoder_for(model: str, *, address: str = ADDRESS, key: str | None = TEST_KEY) -> Decoder:
    return Decoder([DeviceConfig(address=address, model=model, name=f"Synthetic {model}", bindkey=key)])


def binary_values(result: dict) -> dict:
    return {item["key"]: item["value"] for item in result["binary_sensors"]}


def assert_no_state(result: dict) -> None:
    assert result.get("measurements", []) == []
    assert result.get("binary_sensors", []) == []
    assert result.get("events", []) == []
    assert TEST_KEY not in json.dumps(result)


@pytest.mark.parametrize("state,expected", [(1, True), (0, False)])
def test_es5_occupancy(make_record, state, expected):
    record = make_record("ES5BB", object_bytes(0x484E, bytes([state])))
    result = decoder_for("ES5BB").decode(record)
    assert result["status"] == "decoded"
    assert binary_values(result)["occupancy"] is expected


@pytest.mark.parametrize("state,expected", [(1, True), (0, False)])
def test_es5_close_range_occupancy(make_record, state, expected):
    result = decoder_for("ES5BB").decode(make_record("ES5BB", object_bytes(0x5413, bytes([state]))))
    assert result["status"] == "decoded"
    assert binary_values(result)["occupancy_close_range"] is expected


@pytest.mark.parametrize("model,object_id", [("ES5BB", 0x5C03), ("MS1BB", 0x4C03), ("KS2BB", 0x5003), ("HS3BB", 0x4C03)])
@pytest.mark.parametrize("percentage", [0, 92, 100])
def test_authenticated_battery_percentages(make_record, model, object_id, percentage):
    result = decoder_for(model).decode(make_record(model, object_bytes(object_id, bytes([percentage]))))
    assert result["status"] == "decoded" and result["bindkey_verified"] is True
    battery = next(item for item in result["measurements"] if item["key"] == "battery")
    assert battery["value"] == percentage and battery["unit"] == "%"
    assert result["events"] == [] and result["binary_sensors"] == []


@pytest.mark.parametrize("value", [b"", b"\x01\x02", b"\x65", b"\xff"])
def test_invalid_es5_battery_cannot_return_partial_state(make_record, value):
    payload = object_bytes(0x484E, b"\x01") + object_bytes(0x5C03, value)
    result = decoder_for("ES5BB").decode(make_record("ES5BB", payload))
    assert result["status"] == "parse_error"
    assert_no_state(result)


def test_es5_battery_extension_requires_the_right_model_and_valid_mic(make_record):
    payload = object_bytes(0x5C03, b"\x5c")
    for model in ("MS1BB", "KS2BB", "HS3BB"):
        result = decoder_for(model).decode(make_record(model, payload))
        assert_no_state(result)
    wrong_key = make_record("ES5BB", payload, key="ffeeddccbbaa99887766554433221100")
    failure = decoder_for("ES5BB").decode(wrong_key)
    assert failure["status"] == "decryption_failed"
    assert_no_state(failure)
    plaintext = decoder_for("ES5BB").decode(make_record("ES5BB", payload, encrypted=False))
    assert_no_state(plaintext)


@pytest.mark.parametrize("state,expected", [(1, True), (2, False)])
def test_ms1_open_and_closed(make_record, state, expected):
    result = decoder_for("MS1BB").decode(make_record("MS1BB", object_bytes(0x4A12, bytes([state]))))
    assert result["status"] == "decoded"
    assert binary_values(result)["opening"] is expected


def test_ks2_temperature_and_humidity_with_units(make_record):
    objects = object_bytes(0x4C01, struct.pack("<f", 23.75)) + object_bytes(0x4C08, struct.pack("<f", 57.5))
    result = decoder_for("KS2BB").decode(make_record("KS2BB", objects))
    assert result["status"] == "decoded"
    measurements = {item["key"]: item for item in result["measurements"]}
    assert measurements["temperature"]["value"] == pytest.approx(23.75)
    assert measurements["temperature"]["unit"] == "°C"
    assert measurements["humidity"]["value"] == pytest.approx(57.5)
    assert measurements["humidity"]["unit"] == "%"


@pytest.mark.parametrize("object_id,event_type", [(0x560C, "press"), (0x560D, "double_press"), (0x560E, "long_press")])
def test_ks2_button_types(make_record, object_id, event_type):
    result = decoder_for("KS2BB").decode(make_record("KS2BB", object_bytes(object_id, b"\x01")))
    assert result["status"] == "decoded"
    assert len(result["events"]) == 1
    assert {key: result["events"][0][key] for key in ("key", "event_type", "event_properties")} == {
        "key": "button", "event_type": event_type, "event_properties": None,
    }


def test_wrong_key_and_corrupt_mic_do_not_return_cached_state(make_record):
    decoder = decoder_for("ES5BB")
    good = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    assert decoder.decode(good)["status"] == "decoded"
    wrong_key = make_record("ES5BB", object_bytes(0x484E, b"\x00"), key="ffeeddccbbaa99887766554433221100", counter=2)
    failure = decoder.decode(wrong_key)
    assert failure["status"] == "decryption_failed"
    assert_no_state(failure)
    packet = bytearray.fromhex(good.service_data[MIBEACON_UUID])
    packet[-1] ^= 1
    corrupt = replace(good, service_data={MIBEACON_UUID: packet.hex()})
    failure = decoder.decode(corrupt)
    assert failure["status"] == "decryption_failed"
    assert_no_state(failure)
    # A later valid broadcast still works after failed authentication.
    assert decoder.decode(make_record("ES5BB", object_bytes(0x484E, b"\x00"), counter=3))["status"] == "decoded"


def test_devices_keep_separate_keys_and_state(make_record):
    second_address = "AA:BB:CC:DD:EE:02"
    second_key = "ffeeddccbbaa99887766554433221100"
    decoder = Decoder([
        DeviceConfig(ADDRESS, "ES5BB", "First synthetic sensor", TEST_KEY),
        DeviceConfig(second_address, "KS2BB", "Second synthetic sensor", second_key),
    ])
    first = decoder.decode(make_record("ES5BB", object_bytes(0x484E, b"\x01")))
    second = decoder.decode(make_record("KS2BB", object_bytes(0x4C01, struct.pack("<f", 19.25)), address=second_address, key=second_key))
    assert first["status"] == second["status"] == "decoded"
    assert binary_values(first)["occupancy"] is True
    assert "occupancy" not in binary_values(second)
    assert {item["key"]: item["value"] for item in second["measurements"]}["temperature"] == pytest.approx(19.25)


def test_encrypted_broadcast_without_key(make_record):
    record = make_record("MS1BB", object_bytes(0x4A12, b"\x01"))
    result = Decoder().decode(record)
    assert result["status"] == "encrypted_no_key"
    assert_no_state(result)


def test_plaintext_broadcast_needs_no_key(make_record):
    record = make_record("MS1BB", object_bytes(0x4A12, b"\x01"), encrypted=False)
    result = Decoder().decode(record)
    assert result["status"] == "decoded"
    assert binary_values(result)["opening"] is True


@pytest.mark.parametrize("idle_seconds,expected", [(0, True), (120, False)])
def test_hs3_authenticated_idle_motion(make_record, idle_seconds, expected):
    result = decoder_for("HS3BB").decode(make_record("HS3BB", object_bytes(0x4818, struct.pack("<H", idle_seconds))))
    assert result["status"] == "decoded"
    assert result["bindkey_verified"] is True
    assert binary_values(result)["motion"] is expected


def test_hs3_authenticated_motion_and_light(make_record):
    result = decoder_for("HS3BB").decode(make_record("HS3BB", object_bytes(0x4A08, struct.pack("<f", 168.0))))
    assert result["status"] == "decoded"
    assert binary_values(result)["motion"] is True
    assert {x["key"]: x["value"] for x in result["measurements"]}["illuminance"] == 168.0


def test_unknown_product_stays_unsupported(make_record):
    record = make_record("HS3BB", object_bytes(0x4818, b"\x00\x00"))
    raw = bytearray.fromhex(record.service_data[MIBEACON_UUID])
    raw[2:4] = b"\xff\xff"
    result = decoder_for("HS3BB").decode(replace(record, service_data={MIBEACON_UUID: raw.hex()}))
    assert result["status"] == "unsupported_model"
    assert_no_state(result)


@pytest.mark.parametrize("raw", ["", "01", "58590f6a", "not-hex"])
def test_truncated_header_or_invalid_hex_is_malformed(make_record, raw):
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    result = decoder_for("ES5BB").decode(replace(record, service_data={MIBEACON_UUID: raw}))
    assert result["status"] == "malformed"
    assert_no_state(result)


def test_absent_mibeacon_is_not_a_measurement(make_record):
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    result = decoder_for("ES5BB").decode(replace(record, service_data={}))
    assert result["status"] == "no_mibeacon"
    assert_no_state(result)


def test_unknown_object_does_not_return_cached_measurements(make_record):
    decoder = decoder_for("KS2BB")
    good = make_record("KS2BB", object_bytes(0x4C01, struct.pack("<f", 21.0)))
    assert decoder.decode(good)["status"] == "decoded"
    result = decoder.decode(make_record("KS2BB", object_bytes(0x7FFF, b"\x01"), counter=2))
    assert result["status"] == "no_measurement"
    assert_no_state(result)


def test_decoder_output_never_contains_bindkey(make_record):
    result = decoder_for("ES5BB").decode(make_record("ES5BB", object_bytes(0x484E, b"\x01")))
    assert TEST_KEY not in json.dumps(result)


def test_repeated_button_broadcast_is_emitted_once_per_window(make_record):
    decoder = decoder_for("KS2BB")
    record = make_record("KS2BB", object_bytes(0x560C, b"\x01"))
    assert decoder.decode(record)["status"] == "decoded"
    duplicate = decoder.decode(replace(record, timestamp="2026-10-21T12:00:04+00:00"))
    assert duplicate["status"] == "duplicate"
    assert duplicate["bindkey_verified"] is True
    assert_no_state(duplicate)
    # A burst of repeated radio packets must not refire the event every five seconds.
    continued = decoder.decode(replace(record, timestamp="2026-10-21T12:00:06+00:00"))
    assert continued["status"] == "duplicate"
    assert continued["bindkey_verified"] is True
    assert_no_state(continued)
    # Byte counters can wrap: allow the same packet after a quiet interval.
    later = decoder.decode(replace(record, timestamp="2026-10-21T12:00:12+00:00"))
    assert later["status"] == "decoded"
    assert len(later["events"]) == 1


def test_button_deduplication_is_isolated_by_mac(make_record):
    second_address = "AA:BB:CC:DD:EE:02"
    decoder = Decoder([
        DeviceConfig(ADDRESS, "KS2BB", "First", TEST_KEY),
        DeviceConfig(second_address, "KS2BB", "Second", TEST_KEY),
    ])
    first = make_record("KS2BB", object_bytes(0x560D, b"\x01"))
    second = make_record("KS2BB", object_bytes(0x560D, b"\x01"), address=second_address)
    assert decoder.decode(first)["status"] == "decoded"
    assert decoder.decode(second)["status"] == "decoded"
    assert decoder.decode(first)["status"] == "duplicate"


def test_configured_model_does_not_override_product_id(make_record):
    result = decoder_for("ES5BB").decode(make_record("MS1BB", object_bytes(0x4A12, b"\x01")))
    assert result["status"] == "model_mismatch"
    assert_no_state(result)


def test_mesh_broadcast_is_explicitly_unsupported(make_record):
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    data = bytearray.fromhex(record.service_data[MIBEACON_UUID])
    data[0] |= 0x80
    result = decoder_for("ES5BB").decode(replace(record, service_data={MIBEACON_UUID: data.hex()}))
    assert result["header"]["mesh"] is True
    assert result["status"] == "unsupported_protocol"
    assert_no_state(result)


@pytest.mark.parametrize("encrypted", [False, True], ids=["plaintext", "authenticated"])
def test_invalid_float_object_becomes_parse_error_without_cached_state(make_record, encrypted):
    decoder = decoder_for("ES5BB")
    good = make_record("ES5BB", object_bytes(0x484E, b"\x01"), encrypted=encrypted)
    assert decoder.decode(good)["status"] == "decoded"
    # 0x4A08 expects a four-byte float. This is a complete TLV whose value is invalid.
    invalid = make_record("ES5BB", object_bytes(0x4A08, b"\x01"), counter=2, encrypted=encrypted)
    result = decoder.decode(invalid)
    assert result["status"] == "parse_error"
    assert_no_state(result)
    # A malformed object must not prevent subsequent broadcasts from decoding.
    recovered = decoder.decode(make_record("ES5BB", object_bytes(0x484E, b"\x00"), counter=3, encrypted=encrypted))
    assert recovered["status"] == "decoded"
    assert binary_values(recovered)["occupancy"] is False


@pytest.mark.parametrize("encrypted", [False, True], ids=["plaintext", "authenticated"])
def test_valid_object_followed_by_truncated_tlv_returns_no_partial_state(make_record, encrypted):
    # The first object alone is valid, but the next claims four bytes and supplies one.
    payload = object_bytes(0x484E, b"\x01") + b"\x01\x4c\x04\x01"
    decoder = decoder_for("ES5BB")
    record = make_record("ES5BB", payload, encrypted=encrypted)
    result = decoder.decode(record)
    assert result["status"] == "malformed"
    assert_no_state(result)
    # Invalid payloads must never enter the successful-packet deduplication cache.
    repeated = decoder.decode(record)
    assert repeated["status"] == "malformed"
    assert_no_state(repeated)
