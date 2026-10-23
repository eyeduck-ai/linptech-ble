"""Inventory, routing and reception semantics in the dashboard's pure helpers."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest


APP = Path(__file__).parents[1] / "linptech_ble" / "web" / "app.js"
NOW = "2026-10-23T12:05:00Z"
RECENT = "2026-10-23T12:04:59Z"
ADDRESS = "AA:BB:CC:DD:EE:01"
OTHER = "AA:BB:CC:DD:EE:02"
THIRD = "AA:BB:CC:DD:EE:03"


@pytest.fixture(scope="module")
def helpers():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; install it to test dashboard JavaScript helpers")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const boundary = source.indexOf('let latest = null;');
if (boundary < 0) throw new Error('Missing pure-helper boundary');
const context = vm.createContext({ Intl, Date, Number, Math });
vm.runInContext(source.slice(0, boundary), context);
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = cases.map(item => {
  context.payload = item;
  return vm.runInContext(`(() => {
    const { helper, devices, criteria, state, device, receiver, value } = payload;
    if (helper === 'filter') return filterDevices(devices, criteria, state);
    if (helper === 'controller') return controllerFor(state, device) ?? null;
    if (helper === 'radio') return radioSelection(device, state, receiver);
    if (helper === 'csv') return csvCell(value);
    if (helper === 'quality') return qualityForRssi(value);
    throw new Error('Unknown helper');
  })()`, context);
});
process.stdout.write(JSON.stringify(result));
"""

    def run(cases):
        completed = subprocess.run([node, "-e", script, str(APP)], input=json.dumps(cases),
                                   text=True, encoding="utf-8", capture_output=True,
                                   timeout=15, check=False)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    return run


def device(address=ADDRESS, name="Bedroom", model="KS2BB", **values):
    return {"address": address, "name": name, "model": model, "last_seen": RECENT,
            "last_error": None, "rssi": -65, **values}


def filter_case(devices, **criteria):
    return {"helper": "filter", "devices": devices, "criteria": criteria,
            "state": {"server_time": NOW, "stale_after": 180}}


def test_filter_supports_multiple_instances_and_combines_model_query_and_address(helpers):
    devices = [device(), device(OTHER, "Kitchen"), device(THIRD, "Bedroom motion", "HS3BB")]
    result = helpers([
        filter_case(devices, model="KS2BB"),
        filter_case(devices, model="KS2BB", query="  KITCHEN  "),
        filter_case(devices, model="KS2BB", address=OTHER),
        filter_case(devices, query="ee:03"),
        filter_case(devices, model="HS3BB", address=OTHER),
    ])
    assert [[d["address"] for d in group] for group in result] == [
        [ADDRESS, OTHER], [OTHER], [OTHER], [THIRD], [],
    ]


def test_filter_returns_full_device_values_without_mutating_inventory(helpers):
    devices = [device(measurements={"temperature": {"value": 22.5, "updated_at": RECENT}}),
               device(OTHER, "Kitchen")]
    before = deepcopy(devices)
    result, = helpers([filter_case(devices, query="Bedroom")])
    assert result == [before[0]]
    assert devices == before


def test_radio_filters_do_not_convert_silence_into_weak_signal(helpers):
    devices = [device(rssi=-80), device(OTHER, "Quiet", last_seen=None, rssi=None),
               device(THIRD, "Old", last_seen="2026-10-23T12:00:00Z", rssi=-100)]
    result = helpers([filter_case(devices, radio=radio) for radio in ("weak", "waiting", "stale", "recent")])
    assert [[d["address"] for d in group] for group in result] == [[ADDRESS], [OTHER], [THIRD], [ADDRESS]]


def test_error_filter_keeps_error_distinct_from_received_radio_strength(helpers):
    devices = [device(last_error="Synthetic decryption failure"), device(OTHER, "Other")]
    result = helpers([filter_case(devices, radio="error"), filter_case(devices, radio="recent")])
    assert [d["address"] for d in result[0]] == [ADDRESS]
    assert [d["address"] for d in result[1]] == [ADDRESS, OTHER]


def test_controller_routes_same_model_by_exact_address_and_does_not_fall_back(helpers):
    devices = [device(model="ES5BB"), device(OTHER, "Other", "ES5BB")]
    state = {"devices": devices, "direct": {"identity": "legacy"},
             "direct_devices": {ADDRESS: {"identity": "first"}, OTHER: {"identity": "second"}}}
    result = helpers([{"helper": "controller", "state": state, "device": item}
                      for item in [*devices, device(THIRD, model="ES5BB")]])
    assert result == [{"identity": "first"}, {"identity": "second"}, None]


def test_legacy_controller_is_only_available_to_first_instance_of_its_model(helpers):
    devices = [device(), device(OTHER, "Other")]
    state = {"devices": devices, "sensor_direct": {"KS2BB": {"identity": "legacy"}}}
    result = helpers([{"helper": "controller", "state": state, "device": item} for item in devices])
    assert result == [{"identity": "legacy"}, None]


def test_radio_selection_never_combines_receivers_and_keeps_observed_timestamps(helpers):
    history = [{"timestamp": "2026-10-23T12:04:00Z", "rssi": -60, "receiver": "local"},
               {"timestamp": RECENT, "rssi": -90, "receiver": "bedroom-proxy"},
               {"timestamp": "2026-10-23T12:03:00Z", "rssi": -65, "receiver": "local"}]
    sensor = device(radio={"history": history, "receivers": {
        "local": {"count": 2, "median": -62.5}, "bedroom-proxy": {"count": 1, "median": -90}}})
    result = helpers([{"helper": "radio", "device": sensor, "state": {"server_time": NOW}, "receiver": value}
                      for value in ("local", "auto", "unknown-proxy")])
    assert result[0]["points"] == [history[2], history[0]]
    assert result[0]["summary"] == {"count": 2, "median": -62.5}
    assert result[1]["receiver"] == "bedroom-proxy" and result[1]["points"] == [history[1]]
    assert result[2]["points"] == [] and result[2]["summary"]["count"] == 0


def test_radio_selection_rejects_invalid_future_and_out_of_window_points(helpers):
    points = [{"timestamp": stamp, "rssi": strength, "receiver": "local"} for stamp, strength in [
        ("2026-10-23T11:59:59Z", -60), ("2026-10-23T12:05:01Z", -50),
        ("invalid", -55), (RECENT, -127), (RECENT, 0), (RECENT, "-60"),
        (RECENT, -68),
    ]]
    result, = helpers([{"helper": "radio", "device": device(radio={"history": points}),
                        "state": {"server_time": NOW}, "receiver": "local"}])
    assert result["points"] == [points[-1]]


def test_auto_receiver_ignores_future_samples_when_selecting_current_source(helpers):
    points = [{"timestamp": RECENT, "rssi": -68, "receiver": "local"},
              {"timestamp": "2026-10-23T13:00:00Z", "rssi": -40, "receiver": "future-proxy"}]
    result, = helpers([{"helper": "radio", "device": device(radio={"history": points}),
                        "state": {"server_time": NOW}, "receiver": "auto"}])
    assert result["receiver"] == "local" and result["points"] == [points[0]]


@pytest.mark.parametrize(("value", "expected"), [
    (-60, "strong"), (-61, "fair"), (-75, "fair"), (-76, "weak"),
    (-90, "weak"), (-91, "poor"), (-127, "unknown"), (0, "unknown"),
    (None, "unknown"), ("-65", "unknown"),
])
def test_rssi_bands_and_invalid_values(helpers, value, expected):
    result, = helpers([{"helper": "quality", "value": value}])
    assert result == expected


@pytest.mark.parametrize("value", ['=HYPERLINK("synthetic")', "+1+1", "@SUM(1)", "-1+1",
                                   "\tformula", "\rformula", "\nformula", "  =1+1", "\n +1+1"])
def test_csv_defangs_formula_like_strings(helpers, value):
    result, = helpers([{"helper": "csv", "value": value}])
    assert result.startswith('"\'')


def test_csv_quotes_names_and_keeps_negative_numeric_rssi(helpers):
    result = helpers([{"helper": "csv", "value": value} for value in ['Room, "North"', -75, None]])
    assert result == ['"Room, ""North"""', '"-75"', '""']
