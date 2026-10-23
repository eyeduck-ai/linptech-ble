"""HA discovery, event integrity and command boundaries without a real broker."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from linptech_ble.es5_direct import CATALOG
from linptech_ble.ha_mqtt import HomeAssistantBridge, MqttConfig


NOW = datetime(2026, 10, 22, 1, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
MACS = {model: f"AA:BB:CC:DD:EE:0{i}" for i, model in enumerate(("ES5BB", "MS1BB", "KS2BB", "HS3BB"), 1)}


class FakeClient:
    def __init__(self):
        self.messages = []
        self.subscriptions = []
        self.rc = 0

    def publish(self, topic, payload, qos=0, retain=False):
        self.messages.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return SimpleNamespace(rc=self.rc)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))


def sensor(model, **kwargs):
    return {"model": model, "address": MACS[model], "name": model, "measurements": {}, "binary_sensors": {}, **kwargs}


def entry(value, *, at=STAMP, source="advertisement"):
    return {"value": value, "updated_at": at, "source": source}


def direct_snapshot():
    properties = {}
    for name, prop in CATALOG.items():
        value = False if prop.format == "bool" else max(0, prop.minimum or 0)
        properties[name] = {"status": "read", "value": value, "updated_at": STAMP}
    for name in ("motion_energy", "presence_energy", "motion_threshold", "presence_threshold"):
        properties[name]["series"] = [0] * 32
    properties["detect_mode"]["value"] = 2
    return {"authenticated": True, "status": "ready", "busy": False,
            "token_configured": True, "properties": properties,
            "bitmaps": {"pir": 0, "occupied": 0, "floor_noise": 100}}


@pytest.fixture
def fixture():
    state = {"mode": "live", "started_at": "test-session", "scanner": {"status": "listening"},
             "devices": [sensor(model) for model in MACS], "events": [], "direct": direct_snapshot()}
    submitted = []
    bridge = HomeAssistantBridge(MqttConfig(enabled=True, host="127.0.0.1"), lambda: deepcopy(state), submitted.append)
    client = FakeClient()
    bridge._client = client
    bridge._on_connect(client, None, None, 0)
    return state, bridge, client, submitted


def matches(client, suffix):
    return [message for message in client.messages if message["topic"].endswith(suffix)]


def latest(client, suffix):
    return matches(client, suffix)[-1]["payload"]


def message(bridge, name, value, *, retained=False, model="ES5BB"):
    mac = MACS[model].replace(":", "").lower()
    bridge._on_message(None, None, SimpleNamespace(topic=f"linptech_ble/{mac}/command/{name}",
                                                  payload=value.encode(), retain=retained))


def test_config_rejects_unsafe_settings_without_echoing_credentials():
    for kwargs in ({"enabled": True}, {"host": "mqtt://x"}, {"port": True}, {"topic_prefix": "a/#"},
                   {"client_id": "bad/name"}, {"poll_interval": 0}, {"stale_after": float("nan")},
                   {"tls": False, "ca_cert": "private.pem"}):
        with pytest.raises(ValueError):
            MqttConfig(**kwargs)
    config = MqttConfig(username="secret-user", password="secret-pass", ca_cert="secret-ca", tls=True)
    assert all(secret not in repr(config) for secret in ("secret-user", "secret-pass", "secret-ca"))


def test_discovery_has_mac_stable_ids_four_devices_and_all_availability(fixture):
    state, bridge, client, _ = fixture
    bridge._tick(now=NOW)
    configs = [json.loads(m["payload"]) for m in matches(client, "/config")]
    assert {config["device"]["model"] for config in configs} == set(MACS)
    assert len({config["unique_id"] for config in configs}) == len(configs)
    assert all(config["availability_mode"] == "all" and len(config["availability"]) == 2 for config in configs)
    assert all(message["retain"] and message["qos"] == 1 for message in matches(client, "/config"))
    assert any(config.get("event_types") == ["press", "double_press", "long_press"] for config in configs)
    assert all(config.get("retain") is False and config.get("optimistic") is False
               for config in configs if "command_topic" in config and "payload_press" not in config)
    original = {config["unique_id"] for config in configs}
    state["devices"][0]["name"] = "A renamed room"
    bridge._resync.set()
    bridge._tick(now=NOW)
    assert {json.loads(m["payload"])["unique_id"] for m in matches(client, "/config")} == original


def test_resync_orders_global_offline_then_discovery_then_current_states_then_online(fixture):
    state, bridge, client, _ = fixture
    state["devices"][0]["measurements"]["battery"] = entry(0)
    bridge._tick(now=NOW)
    assert client.messages[0]["topic"] == bridge.availability_topic
    assert client.messages[0]["payload"] == "offline"
    assert client.messages[-1]["topic"] == bridge.availability_topic
    assert client.messages[-1]["payload"] == "online"
    configs = [i for i, message in enumerate(client.messages) if message["topic"].endswith("/battery/config")]
    state_index = next(i for i, message in enumerate(client.messages) if message["topic"].endswith("01/battery/state"))
    assert configs[0] < state_index


def test_zero_false_and_field_timestamps_are_preserved_no_fabricated_missing_values(fixture):
    state, bridge, client, _ = fixture
    device = state["devices"][0]
    device["measurements"] = {"battery": entry(0), "illuminance": entry(0)}
    device["binary_sensors"] = {"occupancy": entry(False)}
    bridge._tick(now=NOW)
    assert latest(client, "01/battery/state") == "0"
    assert latest(client, "01/illuminance/state") == "0"
    assert latest(client, "01/occupancy/state") == "OFF"
    assert json.loads(latest(client, "01/battery/attributes"))["updated_at"] == STAMP
    assert latest(client, "02/battery/availability") == "offline"
    assert not matches(client, "02/battery/state")


def test_state_expiry_does_not_clear_occupancy_or_refresh_it_with_battery(fixture):
    state, bridge, client, _ = fixture
    device = state["devices"][0]
    device["binary_sensors"]["occupancy"] = entry(True)
    device["measurements"]["battery"] = entry(92)
    bridge._tick(now=NOW)
    state_count = len(matches(client, "01/occupancy/state"))
    bridge._tick(now=NOW + timedelta(seconds=181))
    assert latest(client, "01/occupancy/availability") == "offline"
    assert latest(client, "01/battery/availability") == "online"
    assert latest(client, "01/occupancy/state") == "ON"
    assert len(matches(client, "01/occupancy/state")) == state_count
    bridge._tick(now=NOW + timedelta(days=2))
    assert latest(client, "01/battery/availability") == "offline"


def test_proxy_disconnect_unavailable_and_direct_gatt_is_independent_of_scanner(fixture):
    state, bridge, client, _ = fixture
    device = state["devices"][0]
    device["measurements"]["illuminance"] = entry(10, source="gatt")
    device["measurements"]["battery"] = entry(92)
    state["scanner"]["status"] = "error"
    bridge._tick(now=NOW)
    assert latest(client, "01/battery/availability") == "offline"
    assert latest(client, "01/illuminance/availability") == "online"
    state["direct"]["authenticated"] = False
    bridge._tick(now=NOW)
    assert latest(client, "01/illuminance/availability") == "offline"


def test_advanced_arrays_pir_zero_and_distance_masks_have_correct_units(fixture):
    _, bridge, client, _ = fixture
    bridge._tick(now=NOW)
    assert latest(client, "01/pir/state") == "OFF"
    assert latest(client, "01/occupied_mask/state") == "0"
    bins = json.loads(latest(client, "01/occupied_mask/attributes"))
    assert bins["bins"] == [False] * 32 and bins["bin_meters"] == .25
    energy = json.loads(latest(client, "01/motion_energy/attributes"))
    assert energy["bins"] == [0] * 32
    assert latest(client, "01/motion_energy/state") == "0"
    assert all("unit_of_measurement" not in json.loads(message["payload"])
               for message in matches(client, "/motion_energy/config"))


def test_unknown_pir_and_stale_advanced_data_are_unavailable_not_false(fixture):
    state, bridge, client, _ = fixture
    state["direct"]["bitmaps"]["pir"] = None
    bridge._tick(now=NOW)
    assert latest(client, "01/pir/availability") == "offline"
    assert not matches(client, "01/pir/state")
    bridge._tick(now=NOW + timedelta(seconds=9))
    assert latest(client, "01/motion_energy/availability") == "offline"
    assert latest(client, "01/occupied_mask/availability") == "offline"


def test_radar_only_mode_does_not_claim_pir_clear(fixture):
    state, bridge, client, _ = fixture
    state["direct"]["properties"]["detect_mode"]["value"] = 0
    bridge._tick(now=NOW)
    assert latest(client, "01/pir/availability") == "offline"
    assert not matches(client, "01/pir/state")


def test_button_snapshot_bootstrap_duplicate_reconnect_ha_birth_never_replays(fixture):
    state, bridge, client, _ = fixture
    def event(identity, kind="press"):
        return {"id": identity, "timestamp": STAMP, "address": MACS["KS2BB"],
                "model": "KS2BB", "kind": "button", "key": kind}
    state["events"] = [event(1)]
    bridge._tick(now=NOW)
    assert not matches(client, "03/button/state")
    state["events"].insert(0, event(2, "double_press"))
    bridge._tick(now=NOW)
    bridge._tick(now=NOW)
    buttons = matches(client, "03/button/state")
    assert len(buttons) == 1 and buttons[0]["retain"] is False and buttons[0]["qos"] == 0
    assert json.loads(buttons[0]["payload"]) == {"event_type": "double_press", "event_id": 2, "timestamp": STAMP}
    bridge._on_disconnect(client, None, None, 1)
    state["events"].insert(0, event(3))
    bridge._tick(now=NOW)
    bridge._on_connect(client, None, None, 0)
    bridge._tick(now=NOW)
    bridge._on_message(client, None, SimpleNamespace(topic="homeassistant/status", payload=b"online", retain=False))
    bridge._tick(now=NOW)
    assert len(matches(client, "03/button/state")) == 1
    state["events"].insert(0, event(4, "long_press"))
    bridge._tick(now=NOW)
    assert len(matches(client, "03/button/state")) == 2


def test_aged_buttons_and_replay_mode_do_not_publish(fixture):
    state, bridge, client, _ = fixture
    bridge._tick(now=NOW)
    state["events"] = [{"id": 1, "timestamp": (NOW - timedelta(minutes=1)).isoformat(), "address": MACS["KS2BB"],
                        "model": "KS2BB", "kind": "button", "key": "press"}]
    bridge._tick(now=NOW)
    state["mode"] = "replay"
    state["events"][0].update(id=2, timestamp=STAMP)
    bridge._tick(now=NOW)
    assert not matches(client, "03/button/state")


def test_allowed_commands_are_enqueued_then_passed_to_existing_controller(fixture):
    _, bridge, _, submitted = fixture
    message(bridge, "no_one_determine_time", "31")
    message(bridge, "interest_enable", "OFF")
    message(bridge, "interest_region", "0")
    message(bridge, "detect_region", "4294967295")
    message(bridge, "refresh", "PRESS")
    assert submitted == []  # No controller/network work in MQTT callback.
    bridge._tick(now=NOW)
    assert submitted == [
        {"action": "set", "property": "no_one_determine_time", "value": 31},
        {"action": "set", "property": "interest_enable", "value": False},
        {"action": "set", "property": "interest_region", "value": 0},
        {"action": "set", "property": "detect_region", "value": 4294967295},
        {"action": "refresh"},
    ]


@pytest.mark.parametrize("name,payload,retained", [
    ("pir_sensitivity", "2", True), ("pir_sensitivity", "4", False),
    ("pir_sensitivity", "2.0", False), ("pir_sensitivity", "true", False),
    ("pir_sensitivity", "-1", False), ("detect_region", "0", False),
    ("interest_enable", "false", False), ("motion_threshold", "AA,AA", False),
    ("calibrate", "PRESS", False), ("calibration_apply", "PRESS", False),
    ("radar_calibrate", "PRESS", False), ("cmd_to_device", "256", False),
    ("refresh", "true", False), ("connect", "PRESS\n", False),
])
def test_invalid_or_retained_commands_never_execute(fixture, name, payload, retained):
    _, bridge, _, submitted = fixture
    message(bridge, name, payload, retained=retained)
    bridge._tick(now=NOW)
    assert submitted == []
    assert bridge.status()["commands_rejected"] == 1


def test_commands_require_current_session_read_authenticated_and_matching_es5(fixture):
    state, bridge, _, submitted = fixture
    for mutation in (
        lambda: state["direct"].update(authenticated=False),
        lambda: state["direct"]["properties"]["pir_sensitivity"].update(status="unread"),
        lambda: state["direct"]["properties"]["pir_sensitivity"].update(updated_at=None),
        lambda: state["direct"].update(busy=True),
    ):
        state["direct"] = direct_snapshot()
        mutation()
        message(bridge, "pir_sensitivity", "2")
        bridge._tick(now=NOW)
    state["direct"] = direct_snapshot()
    message(bridge, "pir_sensitivity", "2", model="KS2BB")
    bridge._tick(now=NOW)
    assert submitted == [] and bridge.status()["commands_rejected"] == 5


def test_static_settings_thresholds_and_commands_remain_valid_in_current_session(fixture):
    state, bridge, client, submitted = fixture
    bridge._tick(now=NOW)
    later = NOW + timedelta(hours=2)
    bridge._tick(now=later)
    assert latest(client, "01/pir_sensitivity/availability") == "online"
    assert latest(client, "01/detect_region/availability") == "online"
    assert latest(client, "01/motion_threshold/availability") == "online"
    assert latest(client, "01/presence_threshold/availability") == "online"
    assert json.loads(latest(client, "01/motion_threshold/attributes"))["updated_at"] == STAMP
    assert latest(client, "01/motion_energy/availability") == "offline"
    assert latest(client, "01/pir/availability") == "offline"
    message(bridge, "pir_sensitivity", "2")
    bridge._tick(now=later)
    assert submitted == [{"action": "set", "property": "pir_sensitivity", "value": 2}]
    state["direct"]["authenticated"] = False
    bridge._tick(now=later)
    assert latest(client, "01/pir_sensitivity/availability") == "offline"
    assert latest(client, "01/motion_threshold/availability") == "offline"


def test_failed_snapshot_gates_global_availability_until_successful_tick(fixture, monkeypatch):
    state, bridge, client, _ = fixture
    bridge._tick(now=NOW)
    assert latest(client, "/bridge/availability") == "online"
    normal_snapshot = bridge._snapshot
    def broken_snapshot():
        raise RuntimeError("secret-endpoint-or-password")
    bridge._snapshot = broken_snapshot
    # Run exactly one worker iteration, including its real exception handler.
    def stop_wait(_timeout):
        bridge._stop.set()
        return True
    monkeypatch.setattr(bridge._stop, "wait", stop_wait)
    bridge._run()
    assert latest(client, "/bridge/availability") == "offline"
    assert "secret-endpoint-or-password" not in json.dumps(bridge.status())
    bridge._snapshot = normal_snapshot
    bridge._tick(now=NOW)
    assert latest(client, "/bridge/availability") == "online"


def test_disconnect_epoch_discards_queued_commands(fixture):
    _, bridge, client, submitted = fixture
    message(bridge, "pir_sensitivity", "2")
    bridge._on_disconnect(client, None, None, 1)
    bridge._on_connect(client, None, None, 0)
    bridge._tick(now=NOW)
    assert submitted == []


def test_controller_failure_does_not_echo_exception_or_snapshot_secrets(fixture):
    state, bridge, client, _ = fixture
    state["secret"] = "forbidden-bindkey-value"
    state["direct"]["auth_token"] = "forbidden-auth-token"
    def fail(command):
        raise ValueError("forbidden-password")
    bridge._submit = fail
    message(bridge, "pir_sensitivity", "2")
    bridge._tick(now=NOW)
    serialized = json.dumps(client.messages) + json.dumps(bridge.status())
    assert "forbidden" not in serialized
    assert bridge.status()["commands_rejected"] == 1


def test_failed_publish_does_not_cache_or_retry_a_button(fixture):
    state, bridge, client, _ = fixture
    bridge._tick(now=NOW)
    client.rc = 4
    state["events"] = [{"id": 1, "timestamp": STAMP, "address": MACS["KS2BB"],
                        "model": "KS2BB", "kind": "button", "key": "press"}]
    bridge._tick(now=NOW)
    client.rc = 0
    bridge._tick(now=NOW)
    assert len(matches(client, "03/button/state")) == 1


def test_ms1_door_alias_has_only_one_stable_entity(fixture):
    state, bridge, client, _ = fixture
    state["devices"][1]["binary_sensors"]["door"] = entry(False)
    bridge._tick(now=NOW)
    assert latest(client, "02/opening/state") == "OFF"
    assert not matches(client, "/door/config")


def test_second_es5_cannot_receive_first_es5_gatt_data_or_commands(fixture):
    state, bridge, client, submitted = fixture
    state["devices"].append({**sensor("ES5BB"), "address": "AA:BB:CC:DD:EE:99"})
    bridge._tick(now=NOW)
    assert not matches(client, "99/pir/state")
    bridge._on_message(None, None, SimpleNamespace(topic="linptech_ble/aabbccddee99/command/pir_sensitivity",
                                                  payload=b"2", retain=False))
    bridge._tick(now=NOW)
    assert submitted == []


def test_reported_durations_discover_with_explicit_units_and_independent_expiry(fixture):
    state, bridge, client, _ = fixture
    state["devices"][0]["measurements"].update(duration_detected=entry(120), duration_cleared=entry(5))
    state["devices"][3]["measurements"].update(no_motion_duration=entry(0), custom_no_motion_time=entry(10))
    bridge._tick(now=NOW)
    assert latest(client, "01/duration_detected/state") == "120"
    assert latest(client, "04/no_motion_duration/state") == "0"
    config = json.loads(latest(client, "/custom_no_motion_time/config"))
    assert config["device_class"] == "duration" and config["unit_of_measurement"] == "min"
    assert "command_topic" not in config  # A broadcast timeout is not a write capability.
    assert json.loads(latest(client, "/no_motion_duration/config"))["unit_of_measurement"] == "s"
    bridge._tick(now=NOW + timedelta(seconds=181))
    assert latest(client, "04/no_motion_duration/availability") == "offline"
    assert latest(client, "04/no_motion_duration/state") == "0"


def test_non_es5_gatt_values_follow_their_own_authenticated_session(fixture):
    state, bridge, client, _ = fixture
    state["direct"]["authenticated"] = False
    state["scanner"]["status"] = "error"
    state["sensor_direct"] = {"KS2BB": {"authenticated": True, "status": "ready"}}
    state["devices"][2]["measurements"]["temperature"] = entry(0, source="gatt")
    bridge._tick(now=NOW)
    assert latest(client, "03/temperature/state") == "0"
    assert latest(client, "03/temperature/availability") == "online"
    state["direct"]["authenticated"] = True
    state["sensor_direct"]["KS2BB"]["authenticated"] = False
    bridge._tick(now=NOW)
    assert latest(client, "03/temperature/availability") == "offline"


def test_optional_pir_energy_preserves_signed_value_without_invented_units_and_expires(fixture):
    state, bridge, client, _ = fixture
    state["direct"]["properties"]["pir_energy"].update(value=-17)
    bridge._tick(now=NOW)
    assert latest(client, "01/pir_energy/state") == "-17"
    config = json.loads(latest(client, "/pir_energy/config"))
    assert "unit_of_measurement" not in config and "command_topic" not in config
    bridge._tick(now=NOW + timedelta(seconds=9))
    assert latest(client, "01/pir_energy/availability") == "offline"
    state["direct"]["properties"]["pir_energy"]["updated_at"] = (NOW + timedelta(seconds=9)).isoformat()
    state["direct"]["properties"]["detect_mode"]["value"] = 0
    bridge._tick(now=NOW + timedelta(seconds=9))
    assert latest(client, "01/pir_energy/availability") == "offline"


def test_readonly_optional_property_is_not_promoted_to_a_mqtt_control(fixture):
    _, bridge, client, submitted = fixture
    bridge._tick(now=NOW)
    config = json.loads(latest(client, "/manual_off_detection_seconds/config"))
    assert "command_topic" not in config
    message(bridge, "manual_off_detection_seconds", "1")
    bridge._tick(now=NOW)
    assert submitted == []


def test_device_events_have_separate_nonretained_stream_and_no_replay(fixture):
    state, bridge, client, _ = fixture
    state["events"] = [{"id": 1, "timestamp": STAMP, "address": MACS["KS2BB"], "model": "KS2BB", "kind": "device", "key": "device_reset"}]
    bridge._tick(now=NOW)  # Historical reset is not replayed.
    assert not matches(client, "/device_event/state")
    state["events"].insert(0, {"id": 2, "timestamp": STAMP, "address": MACS["HS3BB"], "model": "HS3BB", "kind": "device", "key": "device_reset"})
    state["events"].insert(0, {"id": 3, "timestamp": STAMP, "address": MACS["ES5BB"], "model": "ES5BB", "kind": "device", "key": "radar_noise_critical"})
    bridge._tick(now=NOW)
    assert not matches(client, "/button/state")
    events = matches(client, "/device_event/state")
    assert [json.loads(m["payload"])["event_type"] for m in events] == ["device_reset", "radar_noise_critical"]
    assert all(m["retain"] is False and m["qos"] == 0 for m in events)
    bridge._resync.set()
    bridge._tick(now=NOW)
    assert len(matches(client, "/device_event/state")) == 2


def test_unrecognized_events_or_misclassified_button_do_not_trigger_mqtt_actions(fixture):
    state, bridge, client, _ = fixture
    bridge._tick(now=NOW)
    state["events"] = [
        {"id": 1, "timestamp": STAMP, "address": MACS["KS2BB"], "model": "KS2BB", "kind": "device", "key": "press"},
        {"id": 2, "timestamp": STAMP, "address": MACS["HS3BB"], "model": "HS3BB", "kind": "button", "key": "press"},
        {"id": 3, "timestamp": STAMP, "address": MACS["ES5BB"], "model": "ES5BB", "kind": "device", "key": "unknown-secret-event"},
    ]
    bridge._tick(now=NOW)
    assert not matches(client, "/button/state") and not matches(client, "/device_event/state")
    assert "unknown-secret-event" not in json.dumps(client.messages)
