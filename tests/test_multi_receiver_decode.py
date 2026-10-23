"""Multi-receiver decoding is independent of RSSI, ordering and callback threads."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from linptech_ble import Decoder, DeviceConfig, decode_service_info, record_from_service_info
from conftest import ADDRESS, TEST_KEY, object_bytes


def info(record, source):
    return SimpleNamespace(address=record.address, name=record.name, rssi=record.rssi,
        service_data={key: bytes.fromhex(value) for key, value in record.service_data.items()},
        manufacturer_data={}, service_uuids=record.service_uuids, source=source)


def test_external_source_and_explicit_override_are_preserved(make_record):
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    service = info(record, "bedroom")
    assert record_from_service_info(service).receiver == "bedroom"
    assert record_from_service_info(service, receiver="hall").receiver == "hall"
    service.source = None
    assert record_from_service_info(service).receiver == "local"
    decoder = Decoder([DeviceConfig(ADDRESS, "ES5BB", "Radar", TEST_KEY)])
    assert decode_service_info(service, decoder, receiver="office")["receiver"] == "office"


@pytest.mark.parametrize("source", [False, "", "x" * 65, object()])
def test_invalid_source_is_rejected_without_echo(make_record, source):
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    with pytest.raises(ValueError, match="Invalid Bluetooth service"):
        record_from_service_info(info(record, source))


def test_out_of_order_proxy_duplicate_does_not_refire_or_rewind_cache(make_record):
    decoder = Decoder([DeviceConfig(ADDRESS, "KS2BB", "Button", TEST_KEY)])
    record = make_record("KS2BB", object_bytes(0x560C, b"\x01"))
    first = replace(record, timestamp="2026-10-21T12:00:04+00:00", receiver="room")
    late = replace(record, timestamp="2026-10-21T12:00:00+00:00", receiver="hall", rssi=-90)
    assert decoder.decode(first)["status"] == "decoded"
    duplicate = decoder.decode(late)
    assert duplicate["status"] == "duplicate" and not duplicate["events"]
    assert duplicate["receiver"] == "hall" and duplicate["rssi"] == -90
    assert decoder.decode(replace(record, timestamp="2026-10-21T12:00:08+00:00"))["status"] == "duplicate"
    assert decoder.decode(replace(record, timestamp="2026-10-21T12:00:14+00:00"))["status"] == "decoded"


def test_simultaneous_three_receiver_feeds_emit_one_event_per_device(make_record):
    addresses = [f"AA:BB:CC:DD:EE:{i:02X}" for i in range(1, 9)]
    decoder = Decoder([DeviceConfig(address, "KS2BB", "Button", TEST_KEY) for address in addresses])
    records = [replace(make_record("KS2BB", object_bytes(0x560D, b"\x01"), address=address), receiver=source, rssi=rssi)
        for address in addresses for source, rssi in [("room", -50), ("hall", -70), ("office", -90)]]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(decoder.decode, records))
    assert sum(len(result["events"]) for result in results) == 8
    assert sum(result["status"] == "duplicate" for result in results) == 16
    assert {result["receiver"] for result in results} == {"room", "hall", "office"}


def test_cache_cannot_verify_packet_after_caller_replaces_key(make_record):
    device = DeviceConfig(ADDRESS, "ES5BB", "Radar", TEST_KEY)
    decoder = Decoder([device])
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    assert decoder.decode(record)["bindkey_verified"]
    device.bindkey = "ab" * 16
    result = decoder.decode(replace(record, receiver="another-proxy"))
    assert result["status"] == "decryption_failed" and not result["bindkey_verified"]
