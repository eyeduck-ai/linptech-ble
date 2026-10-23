"""Pure decoder adapters and importing the library never own application I/O."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import traceback
from types import SimpleNamespace

import pytest

from linptech_ble.adapters import decode_service_info, record_from_service_info
from linptech_ble.config import DeviceConfig
from linptech_ble.decoder import Decoder
from linptech_ble.models import MIBEACON_UUID

from conftest import ADDRESS, TEST_KEY, object_bytes


def service_info(record):
    return SimpleNamespace(address=record.address, name=record.name, rssi=record.rssi,
        service_data={key: bytes.fromhex(value) for key, value in record.service_data.items()},
        manufacturer_data={int(key): bytes.fromhex(value) for key, value in record.manufacturer_data.items()},
        service_uuids=record.service_uuids, time=12345.0)


def test_plain_package_import_has_no_optional_imports_threads_scanner_or_secret_reads():
    root = Path(__file__).resolve().parents[1]
    code = '''
import json, pathlib, socket, threading, sys
def forbidden(*args, **kwargs):
    raise AssertionError("Import attempted application I/O")
threading.Thread.start = forbidden
socket.socket = forbidden
pathlib.Path.read_text = forbidden
import linptech_ble
assert linptech_ble.__version__ == "0.4.0"
assert "LinptechClient" in dir(linptech_ble)
for name in ("bleak", "xiaomi_ble", "cryptography", "aioesphomeapi", "paho", "linptech_ble.dashboard", "linptech_ble.client"):
    assert name not in sys.modules, name
print("safe-import")
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "safe-import"


@pytest.mark.parametrize("names", ["capabilities, capabilities_schema", "capabilities_schema, capabilities"])
def test_top_level_inventory_exports_are_callable_in_either_import_order(names):
    root = Path(__file__).resolve().parents[1]
    code = f'''
from linptech_ble import {names}
assert callable(capabilities) and callable(capabilities_schema)
assert capabilities("MS1BB")["details"]["models"]["MS1BB"]["direct"]["settings_exposed"] is False
assert capabilities_schema()["properties"]["version"]["const"] == 1
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_bluetooth_service_info_adapter_preserves_bytes_and_explicit_wall_time(make_record):
    record = make_record("KS2BB", object_bytes(0x4802, b"\x44"))
    converted = record_from_service_info(service_info(record), timestamp=record.timestamp)
    assert converted.address == record.address and converted.service_data == record.service_data
    assert converted.timestamp == record.timestamp and converted.rssi == record.rssi
    info = service_info(record)
    converted_datetime = record_from_service_info(info, timestamp=datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert converted_datetime.timestamp == "2026-10-23T00:00:00+00:00"
    assert record_from_service_info(info).timestamp != str(info.time)


def test_adapter_feeds_a_caller_owned_decoder_and_preserves_button_deduplication(make_record):
    record = make_record("KS2BB", object_bytes(0x560C, b""))
    decoder = Decoder([DeviceConfig(ADDRESS, "KS2BB", "Synthetic", TEST_KEY)])
    first = decode_service_info(service_info(record), decoder, timestamp=record.timestamp)
    second = decode_service_info(service_info(record), decoder, timestamp=record.timestamp)
    assert first["events"][0]["event_type"] == "press"
    assert second["status"] == "duplicate" and second["events"] == []
    assert TEST_KEY not in json.dumps(first)


def test_adapter_requires_caller_owned_decoder_instead_of_a_secret_file_path(make_record):
    with pytest.raises(TypeError):
        decode_service_info(service_info(make_record("ES5BB", object_bytes(0x484E, b"\x01"))), "sensors.local.json")


@pytest.mark.parametrize("mutation", [
    lambda info: setattr(info, "address", "not-a-mac-private-value"),
    lambda info: setattr(info, "rssi", True),
    lambda info: setattr(info, "service_data", {MIBEACON_UUID: "secret-invalid-payload"}),
])
def test_adapter_errors_do_not_echo_input_or_secret_values(make_record, mutation):
    info = service_info(make_record("ES5BB", object_bytes(0x484E, b"\x01")))
    mutation(info)
    with pytest.raises(ValueError) as error:
        record_from_service_info(info)
    assert "private-value" not in str(error.value) and "secret-invalid-payload" not in str(error.value)


def test_adapter_formatted_traceback_redacts_invalid_timestamp_cause(make_record):
    marker = "SYNTHETIC_CREDENTIAL_MUST_NOT_ECHO"
    info = service_info(make_record("ES5BB", object_bytes(0x484E, b"\x01")))
    try:
        record_from_service_info(info, timestamp=marker)
    except ValueError as error:
        formatted = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    else:
        pytest.fail("Invalid timestamp unexpectedly accepted")
    assert marker not in formatted
    assert "Invalid Bluetooth service information" in formatted
