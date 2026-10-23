from __future__ import annotations

import json
from dataclasses import replace

import pytest

from linptech_ble.config import DeviceConfig, load_config, normalize_address, save_config, validate_bindkey
from linptech_ble.models import AdvertisementRecord, MIBEACON_UUID, is_candidate, mibeacon_header

from conftest import ADDRESS, TEST_KEY, object_bytes


@pytest.mark.parametrize("address", ["aa:bb:cc:dd:ee:01", "AA-BB-CC-DD-EE-01", "aabbccddee01"])
def test_mac_normalization(address):
    assert normalize_address(address) == ADDRESS


@pytest.mark.parametrize("key", ["", "abc", "z" * 32, "0" * 31, "0" * 33, None, 123])
def test_invalid_bindkey_never_echoes_secret(key):
    with pytest.raises(ValueError) as error:
        validate_bindkey(key)
    if isinstance(key, str) and key:
        assert key not in str(error.value)


def test_config_roundtrip_and_repr_redacts_key(tmp_path):
    devices = [DeviceConfig("aa-bb-cc-dd-ee-01", "ES5BB", "合成測試資料", TEST_KEY.upper())]
    path = tmp_path / "secrets.json"
    save_config(path, devices)
    loaded = load_config(path)
    assert loaded == devices
    assert loaded[0].address == ADDRESS
    assert loaded[0].bindkey == TEST_KEY
    assert TEST_KEY not in repr(loaded)
    assert TEST_KEY.upper() not in repr(loaded)


def test_config_rejects_duplicate_normalized_mac(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps({"version": 1, "devices": [
        {"address": ADDRESS, "model": "ES5BB", "name": "One", "bindkey": TEST_KEY},
        {"address": ADDRESS.lower(), "model": "KS2BB", "name": "Two", "bindkey": TEST_KEY},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert TEST_KEY not in str(error.value)


def test_save_rejects_duplicate_mac(tmp_path):
    devices = [DeviceConfig(ADDRESS, "ES5BB", "One", TEST_KEY), DeviceConfig(ADDRESS, "MS1BB", "Two", TEST_KEY)]
    with pytest.raises(ValueError):
        save_config(tmp_path / "duplicate.json", devices)


def test_invalid_config_errors_do_not_leak_key(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "devices": [{"address": ADDRESS, "model": "ES5BB", "name": "One", "bindkey": TEST_KEY + "00"}]}), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert TEST_KEY not in str(error.value)


def test_advertisement_record_roundtrip_and_short_uuid(make_record):
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    data = record.to_dict()
    data["service_data"] = {"fe95": data["service_data"][MIBEACON_UUID]}
    data["service_uuids"] = ["fe95"]
    assert AdvertisementRecord.from_dict(data) == record


def test_public_header_and_candidate_detection(make_record):
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    assert mibeacon_header(record) == {
        "product_id": 0x6A0F,
        "product_id_hex": "0x6A0F",
        "model": "ES5BB",
        "version": 5,
        "encrypted": True,
        "registered": True,
        "has_objects": True,
        "mesh": False,
        "packet_counter": 1,
    }
    assert is_candidate(replace(record, name=None)) is True


def test_invalid_record_error_does_not_echo_input():
    with pytest.raises(ValueError) as error:
        AdvertisementRecord.from_dict({"timestamp": TEST_KEY, "address": TEST_KEY, "rssi": -40})
    assert TEST_KEY not in str(error.value)
