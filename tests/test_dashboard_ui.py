"""Dashboard display semantics with synthetic data; no browser or BLE access.

The small JavaScript helpers run in Node when available. Core Python installs
without Node still exercise packet-history integrity below.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from linptech_ble.config import DeviceConfig
from linptech_ble.dashboard import DashboardState

from conftest import ADDRESS, TEST_KEY


APP = Path(__file__).parents[1] / "linptech_ble" / "web" / "app.js"
NOW = "2026-10-23T01:04:00+00:00"
SEEN = "2026-10-23T01:03:59+00:00"


@pytest.fixture(scope="module")
def javascript_helpers():
    """Load pure helpers only, avoiding fetch, timers, DOM and local devices."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; install it to run dashboard JavaScript semantics")

    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const boundary = source.indexOf('let latest = null;');
if (boundary < 0) throw new Error('Pure dashboard helper boundary is missing');
const context = vm.createContext({ Intl, Date, Number, Math });
vm.runInContext(source.slice(0, boundary), context);
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = cases.map(item => {
  context.payload = item;
  return vm.runInContext(
    'payload.helper === "signal" ? signalState(payload.device, payload.state, payload.options || {}) : temperatureSeries(payload.device)',
    context
  );
});
process.stdout.write(JSON.stringify(results));
"""

    def run(cases):
        completed = subprocess.run(
            [node, "-e", script, str(APP)],
            input=json.dumps(cases), text=True, capture_output=True, check=False,
            timeout=15, encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    return run


def signal_case(*, rssi=-55, device=None, state=None, options=None):
    return {
        "helper": "signal",
        "device": {
            "model": "KS2BB", "rssi": rssi, "last_seen": SEEN,
            "freshness": "recent", "last_error": None, "verified": False,
            **(device or {}),
        },
        "state": {"mode": "live", "server_time": NOW, "stale_after": 180, **(state or {})},
        "options": options or {},
    }


def test_signal_quality_thresholds_are_actual_recent_radio_rssi(javascript_helpers):
    levels = [(-1, "strong", 4), (-60, "strong", 4), (-61, "fair", 3),
              (-75, "fair", 3), (-76, "weak", 2), (-90, "weak", 2),
              (-91, "poor", 1), (-110, "poor", 1)]
    results = javascript_helpers([signal_case(rssi=rssi) for rssi, _, _ in levels])
    for result, (rssi, quality, level) in zip(results, levels, strict=True):
        assert (result["quality"], result["level"]) == (quality, level)
        assert result["rssi"] == rssi
        assert result["lastSeen"] == SEEN
        assert result["status"] == "近期廣播"


@pytest.mark.parametrize("rssi", [None, 0, 1, "-55", False])
def test_invalid_or_missing_rssi_does_not_draw_live_signal_bars(javascript_helpers, rssi):
    result, = javascript_helpers([signal_case(rssi=rssi)])
    assert result["quality"] == "unknown"
    assert result["level"] == 0


@pytest.mark.parametrize(("case", "status"), [
    (signal_case(options={"online": False}), "主機離線"),
    (signal_case(options={"frozen": True}), "畫面暫停"),
    (signal_case(state={"mode": "replay"}), "歷史廣播"),
    (signal_case(device={"last_seen": None, "freshness": "waiting"}), "等待廣播"),
    (signal_case(device={"last_seen": "2026-10-23T01:00:00+00:00", "freshness": "stale"}), "廣播過舊"),
])
def test_historical_unreceived_and_frozen_signal_is_explicitly_not_live(javascript_helpers, case, status):
    result, = javascript_helpers([case])
    assert (result["quality"], result["level"], result["status"]) == ("unknown", 0, status)


def test_radio_state_does_not_inherit_occupancy_or_gatt_freshness(javascript_helpers):
    stale_radio = signal_case(device={
        "last_seen": "2026-10-23T01:00:00+00:00", "freshness": "stale",
        "binary_sensors": {"occupancy": {"value": True, "source": "gatt", "updated_at": SEEN}},
    })
    recent_radio = signal_case(device={
        "binary_sensors": {"occupancy": {
            "value": False, "freshness": "stale", "updated_at": "2026-10-23T00:00:00+00:00",
        }},
    })
    old, recent = javascript_helpers([stale_radio, recent_radio])
    assert (old["quality"], old["level"]) == ("unknown", 0)
    assert (recent["quality"], recent["level"]) == ("strong", 4)


def test_error_packet_does_not_look_like_healthy_live_reception(javascript_helpers):
    result, = javascript_helpers([signal_case(device={"last_error": "解密失敗"})])
    assert (result["quality"], result["level"]) == ("unknown", 0)


def test_temperature_series_keeps_real_timestamps_gaps_and_equal_readings(javascript_helpers):
    points = [
        {"timestamp": "2026-10-23T01:10:00Z", "value": 28.5},
        {"timestamp": "2026-10-23T01:00:00Z", "value": 0},
        {"timestamp": "2026-10-23T01:01:00Z", "value": 0},
        {"timestamp": "2026-10-23T09:05:00+08:00", "value": 28.1},
        {"timestamp": "invalid", "value": 100},
        {"timestamp": None, "value": 100},
        {"timestamp": "2026-10-23T01:02:00Z", "value": None},
        {"timestamp": "2026-10-23T01:03:00Z", "value": "28.1"},
        {"timestamp": "2026-10-23T01:04:00Z", "value": True},
    ]
    result, = javascript_helpers([{"helper": "temperature", "device": {"history": {"temperature": points}}}])
    assert [{"timestamp": p["timestamp"], "value": p["value"]} for p in result] == [points[1], points[2], points[3], points[0]]
    assert len(result) == 4


def test_temperature_series_never_fills_from_latest_value_or_other_fields(javascript_helpers):
    results = javascript_helpers([
        {"helper": "temperature", "device": None},
        {"helper": "temperature", "device": {"history": {"humidity": [{"timestamp": SEEN, "value": 60}]}}},
        {"helper": "temperature", "device": {
            "measurements": {"temperature": {"value": 28.9, "updated_at": SEEN}}, "history": {},
        }},
        {"helper": "temperature", "device": {"history": {"temperature": [{"timestamp": SEEN, "value": 28.9}]}}},
    ])
    assert results[:3] == [[], [], []]
    assert results[3] == [{"timestamp": SEEN, "value": 28.9}]


def test_temperature_history_records_real_equal_readings_without_replaying_other_packets():
    state = DashboardState([DeviceConfig(ADDRESS, "KS2BB", "Synthetic", TEST_KEY)])

    def ingest(second, status="decoded", measurements=None):
        state.ingest({
            "address": ADDRESS, "timestamp": f"2026-10-23T01:00:{second:02d}+00:00",
            "status": status, "rssi": -55, "bindkey_verified": True,
            "measurements": measurements or [], "binary_sensors": [], "events": [],
        })

    temperature = [{"key": "temperature", "value": 28.9, "unit": "°C"}]
    ingest(1, measurements=temperature)
    ingest(2, measurements=temperature)
    ingest(3, measurements=[{"key": "battery", "value": 100, "unit": "%"}])
    ingest(4, "duplicate", [{"key": "temperature", "value": 99}])
    ingest(5, "decryption_failed", [{"key": "temperature", "value": 99}])
    result = state.snapshot(now=datetime(2026, 10, 23, 1, 0, 5, tzinfo=timezone.utc))["devices"][0]
    assert result["history"]["temperature"] == [
        {"timestamp": "2026-10-23T01:00:01+00:00", "value": 28.9},
        {"timestamp": "2026-10-23T01:00:02+00:00", "value": 28.9},
    ]
    assert result["measurements"]["temperature"]["updated_at"] == "2026-10-23T01:00:02+00:00"
    assert result["last_seen"] == "2026-10-23T01:00:05+00:00"
