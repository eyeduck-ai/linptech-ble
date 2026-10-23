"""Broadcast diagnostics and device events survive dashboard merging honestly."""
from datetime import datetime, timedelta, timezone
import struct

from linptech_ble.config import DeviceConfig
from linptech_ble.dashboard import DashboardState
from linptech_ble.decoder import Decoder

from conftest import ADDRESS, TEST_KEY, object_bytes


def stack(model):
    devices = [DeviceConfig(ADDRESS, model, "Synthetic", TEST_KEY)]
    return DashboardState(devices), Decoder(devices)


def test_unknown_only_authenticated_objects_survive_without_fake_sensor_values(make_record):
    state, decoder = stack("KS2BB")
    packet = decoder.decode(make_record("KS2BB", object_bytes(0x7FFE, b"\x11\x22")))
    assert packet["status"] == "no_measurement" and packet["bindkey_verified"] is True
    state.ingest(packet)
    snapshot = state.snapshot()
    device = snapshot["devices"][0]
    assert device["objects"]["0x7FFE"]["status"] == "unknown_object"
    assert device["objects"]["0x7FFE"]["raw_hex"] == "1122"
    assert device["objects"]["0x7FFE"]["updated_at"] == packet["timestamp"]
    assert device["measurements"] == device["binary_sensors"] == {}
    assert device["last_event"] is None and snapshot["events"] == []


def test_device_reset_event_does_not_masquerade_as_button_or_replay_duplicate(make_record):
    state, decoder = stack("KS2BB")
    record = make_record("KS2BB", object_bytes(0x561C, b""))
    state.ingest(decoder.decode(record))
    state.ingest(decoder.decode(record))
    snapshot = state.snapshot()
    assert len(snapshot["events"]) == 1
    event = snapshot["events"][0]
    assert event["kind"] == "device" and event["key"] == "device_reset"
    assert "按鍵" not in event["text"]
    assert snapshot["devices"][0]["last_event"]["key"] == "device"
    assert snapshot["devices"][0]["last_event"]["event_type"] == "device_reset"


def test_es5_special_event_does_not_create_pir_or_occupancy_data(make_record):
    state, decoder = stack("ES5BB")
    state.ingest(decoder.decode(make_record("ES5BB", object_bytes(0x5661, b"\x05"))))
    snapshot = state.snapshot()
    device = snapshot["devices"][0]
    assert device["binary_sensors"] == {} and device["measurements"] == {}
    assert snapshot["events"][0]["kind"] == "device"
    assert snapshot["events"][0]["key"] == "radar_noise_minor"


def test_hs3_duration_and_timeout_do_not_refresh_each_other_or_motion(make_record):
    state, decoder = stack("HS3BB")
    first = make_record("HS3BB", object_bytes(0x4818, struct.pack("<H", 120)))
    state.ingest(decoder.decode(first))
    original = first.timestamp
    from dataclasses import replace
    later = (datetime.fromisoformat(original) + timedelta(minutes=5)).isoformat()
    timeout = replace(make_record("HS3BB", object_bytes(0x4835, struct.pack("<H", 10)), counter=2), timestamp=later)
    state.ingest(decoder.decode(timeout))
    snapshot = state.snapshot(now=datetime.fromisoformat(later))
    device = snapshot["devices"][0]
    assert device["measurements"]["no_motion_duration"]["value"] == 120
    assert device["measurements"]["no_motion_duration"]["updated_at"] == original
    assert device["measurements"]["custom_no_motion_time"]["value"] == 10
    assert device["measurements"]["custom_no_motion_time"]["updated_at"] == later
    assert device["binary_sensors"]["motion"]["value"] is False
    assert device["binary_sensors"]["motion"]["updated_at"] == original
    assert device["binary_sensors"]["motion"]["freshness"] == "stale"


def test_outside_spec_zero_timeout_is_diagnostic_without_replacing_last_valid_setting(make_record):
    state, decoder = stack("HS3BB")
    state.ingest(decoder.decode(make_record("HS3BB", object_bytes(0x4835, b"\x0a\x00"))))
    state.ingest(decoder.decode(make_record("HS3BB", object_bytes(0x4835, b"\x00\x00"), counter=2)))
    device = state.snapshot()["devices"][0]
    assert device["objects"]["0x4835"]["status"] == "outside_spec_range"
    assert device["measurements"]["custom_no_motion_time"]["value"] == 10
