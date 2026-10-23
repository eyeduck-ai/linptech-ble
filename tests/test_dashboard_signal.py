"""Reception samples describe observed RSSI, never assumed packet delivery."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from linptech_ble.config import DeviceConfig
from linptech_ble.dashboard import DashboardState
from linptech_ble.decoder import Decoder
from linptech_ble.models import AdvertisementRecord
from linptech_ble.transport import ProxyConfig, _advertisement


ADDRESS = "AA:BB:CC:DD:EE:01"
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def packet(seconds=0, rssi=-60, status="decoded", receiver="local", **extra):
    return {"address": ADDRESS, "timestamp": (NOW + timedelta(seconds=seconds)).isoformat(),
            "status": status, "rssi": rssi, "receiver": receiver, **extra}


def state():
    return DashboardState([DeviceConfig(ADDRESS, "KS2BB", "Synthetic kitchen")])


def radio(dashboard, now=NOW):
    return dashboard.snapshot(now=now)["devices"][0]["radio"]


def test_all_advertisement_statuses_sample_before_decoded_early_returns():
    dashboard = state()
    for offset, status in enumerate(("decoded", "duplicate", "decryption_failed", "no_mibeacon", "malformed")):
        dashboard.ingest(packet(-5 + offset, -60 - offset, status))
    result = radio(dashboard)
    assert len(result["history"]) == result["summary"]["count"] == 5
    assert result["summary"]["latest"] == -64
    assert result["summary"]["median"] == -62
    assert result["summary"]["min"] == -64 and result["summary"]["max"] == -60
    assert result["summary"]["max_gap_seconds"] == 1
    assert result["summary"]["last_sample_age_seconds"] == 1
    assert dashboard.snapshot()["devices"][0]["duplicates"] == 1


@pytest.mark.parametrize("invalid", [None, True, False, 0, 10, -127, -128, float("nan"), float("inf"), "-60"])
def test_unknown_sentinel_or_invalid_rssi_does_not_pollute_json_or_last_valid_value(invalid):
    dashboard = state()
    dashboard.ingest(packet(-1, -68))
    dashboard.ingest(packet(0, invalid, "duplicate"))
    result = dashboard.snapshot(now=NOW)
    assert result["devices"][0]["rssi"] == -68
    assert result["devices"][0]["radio"]["summary"]["count"] == 1
    json.dumps(result, allow_nan=False)


def test_summary_excludes_old_and_future_samples_without_rewriting_replay_times():
    dashboard = state()
    old = packet(-301, -90)
    dashboard.ingest(old)
    dashboard.ingest(packet(-300, -80))
    dashboard.ingest(packet(1, -20))
    result = radio(dashboard)
    assert result["history"][0]["timestamp"] == old["timestamp"]
    assert result["summary"]["count"] == 1
    assert result["summary"]["latest"] == -80
    assert radio(dashboard, NOW + timedelta(hours=1))["summary"]["latest"] is None
    assert radio(dashboard, NOW + timedelta(hours=1))["summary"]["count"] == 0


def test_receiver_statistics_are_separate_and_aggregate_is_explicitly_labelled():
    dashboard = state()
    dashboard.ingest(packet(-20, -50, receiver="living-room"))
    dashboard.ingest(packet(-10, -90, "duplicate", receiver="bedroom"))
    dashboard.ingest(packet(0, -60, "duplicate", receiver="living-room"))
    result = radio(dashboard)
    assert result["summary"]["scope"] == "all_receivers"
    assert result["summary"]["receiver"] is None
    assert result["receivers"]["living-room"]["median"] == -55
    assert result["receivers"]["living-room"]["max_gap_seconds"] == 20
    assert result["receivers"]["bedroom"]["latest"] == -90
    assert result["receivers"]["bedroom"]["max_gap_seconds"] is None
    assert result["receivers"]["bedroom"]["receiver"] == "bedroom"
    assert not any("loss" in field for field in result["summary"])


def test_history_is_bounded_and_stats_sort_real_times_without_fabricated_samples():
    dashboard = state()
    for offset in range(650):
        dashboard.ingest(packet(-650 + offset, -65, "duplicate"))
    result = radio(dashboard)
    assert len(result["history"]) == result["history_limit"] == 600
    assert result["history"][0]["timestamp"] == packet(-600)["timestamp"]
    assert result["summary"]["count"] == 300
    dashboard.ingest(packet(-3, -40))
    dashboard.ingest(packet(-5, -80))
    assert radio(dashboard)["summary"]["last_sample"] == packet(-1)["timestamp"]


def test_gatt_updates_and_unknown_addresses_do_not_fabricate_radio_samples():
    dashboard = DashboardState([DeviceConfig(ADDRESS, "ES5BB", "Synthetic radar")])
    dashboard.ingest_direct({"name": "occupancy_status", "value": 1, "updated_at": NOW.isoformat()})
    dashboard.ingest(packet(address="AA:BB:CC:DD:EE:02"))
    assert radio(dashboard)["history"] == []
    assert radio(dashboard)["summary"]["count"] == 0


def test_malformed_timestamp_is_not_replaced_with_current_time():
    dashboard = state()
    dashboard.ingest(packet(timestamp="not-a-date"))
    assert radio(dashboard)["history"] == []
    assert dashboard.snapshot(now=NOW)["devices"][0]["freshness"] == "waiting"


def test_proxy_receiver_survives_native_adapter_record_decoder_and_replay():
    incoming = SimpleNamespace(address=int(ADDRESS.replace(":", ""), 16), rssi=-73,
        name="Synthetic", address_type=0, manufacturer_data={}, service_data={}, service_uuids=[])
    native_device, advertisement = _advertisement(ProxyConfig("bedroom", "192.0.2.10"), incoming)
    record = AdvertisementRecord.from_bleak(native_device, advertisement)
    assert record.receiver == "bedroom"
    assert Decoder().decode(record)["receiver"] == "bedroom"
    assert AdvertisementRecord.from_dict(record.to_dict()).receiver == "bedroom"
    legacy = record.to_dict()
    del legacy["receiver"]
    assert AdvertisementRecord.from_dict(legacy).receiver == "local"
    local_device = SimpleNamespace(address=ADDRESS, name="Synthetic", details={"platform-native": object()})
    assert AdvertisementRecord.from_bleak(local_device, advertisement).receiver == "local"


def test_legacy_positional_advertisement_constructor_remains_compatible():
    record = AdvertisementRecord(NOW.isoformat(), ADDRESS, "Synthetic", -45, {}, {}, [], None)
    assert record.receiver == "local"
    assert replace(record, receiver="proxy-1").tx_power is None
