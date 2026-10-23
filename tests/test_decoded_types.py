"""Public imports carry useful result types without changing JSON payloads."""
import ast
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from typing import get_args, get_type_hints

import pytest

import linptech_ble
from linptech_ble import DecodedAdvertisement, DecodeStatus, Decoder, DeviceConfig, decode_service_info
from linptech_ble.decoded import BinarySensorValue, Measurement, SensorEvent
from conftest import ADDRESS, TEST_KEY, object_bytes

ROOT = Path(__file__).resolve().parents[1]


def test_public_annotations_and_stub_match_runtime_exports():
    assert get_type_hints(Decoder.decode)["return"] is DecodedAdvertisement
    assert get_type_hints(decode_service_info, localns={"Decoder": Decoder})["return"] is DecodedAdvertisement
    assert get_type_hints(DecodedAdvertisement)["status"] == DecodeStatus
    tree = ast.parse((ROOT / "linptech_ble" / "__init__.pyi").read_text(encoding="utf-8"))
    exported = {name.asname or name.name for node in tree.body if isinstance(node, ast.ImportFrom) for name in node.names}
    assert exported == set(linptech_ble._EXPORTS)


@pytest.mark.parametrize("model,object_id,payload,collection,item_type", [
    ("ES5BB", 0x484E, b"\x01", "binary_sensors", BinarySensorValue),
    ("MS1BB", 0x4804, b"\x02", "binary_sensors", BinarySensorValue),
    ("KS2BB", 0x560C, b"", "events", SensorEvent),
    ("HS3BB", 0x4C03, b"\x50", "measurements", Measurement),
])
def test_decoded_envelopes_keep_plain_dict_json_and_receiver_metadata(make_record, model, object_id, payload, collection, item_type):
    decoder = Decoder([DeviceConfig(ADDRESS, model, "Synthetic", TEST_KEY)])
    record = replace(make_record(model, object_bytes(object_id, payload)), receiver="room")
    result = decoder.decode(record)
    assert type(result) is dict and result["status"] in get_args(DecodeStatus)
    assert DecodedAdvertisement.__required_keys__ <= result.keys()
    assert item_type.__required_keys__ <= result[collection][0].keys()
    assert json.loads(json.dumps(result)) == result
    duplicate = decoder.decode(replace(record, receiver="hall", rssi=-84))
    assert duplicate["status"] == "duplicate" and duplicate["receiver"] == "hall" and duplicate["rssi"] == -84
    assert not duplicate["events"] and not duplicate["measurements"] and not duplicate["binary_sensors"]


def check_consumer(tmp_path, source):
    pytest.importorskip("mypy")
    consumer = tmp_path / "consumer.py"
    consumer.write_text(source, encoding="utf-8")
    return subprocess.run([sys.executable, "-m", "mypy", "--no-incremental", "--cache-dir", str(tmp_path / "cache"), str(consumer)],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)


def test_type_checker_accepts_normal_package_imports_and_typed_decoder_use(tmp_path):
    result = check_consumer(tmp_path, '''
from linptech_ble import AdvertisementRecord, Decoder, DecodedAdvertisement, DecodeStatus, JSONValue
def decode(decoder: Decoder, record: AdvertisementRecord) -> DecodedAdvertisement:
    result = decoder.decode(record)
    status: DecodeStatus = result["status"]
    strength: int = result["rssi"]
    for measurement in result["measurements"]:
        value: JSONValue = measurement["value"]
        unit: str | None = measurement["unit"]
    for sensor in result["binary_sensors"]:
        state: bool | None = sensor["value"]
    return result
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_type_checker_rejects_wrong_fields_values_and_status_through_package_imports(tmp_path):
    result = check_consumer(tmp_path, '''
from linptech_ble import AdvertisementRecord, Decoder
def consume(decoder: Decoder, record: AdvertisementRecord) -> None:
    result = decoder.decode(record)
    result["rssi"] = "strong"
    result["status"] = "decdoed"
    result["measurements"][0]["value"] = object()
    result["binary_sensors"][0]["value"] = 42
    result["events"][0]["event_type"] = None
    result["recevier"] = "room"
''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert "6 errors in 1 file" in result.stdout
    assert "typeddict-unknown-key" in result.stdout and "typeddict-item" in result.stdout
