"""The public inventory distinguishes protocol claims from device evidence."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

import pytest

from linptech_ble.broadcast_objects import object_catalog
from linptech_ble.capabilities import capabilities, capabilities_schema
from linptech_ble.property_catalog import PROPERTIES, PROFILES


def detail(model):
    return capabilities(model)["details"]["models"][model]


def properties(model):
    return {prop["name"]: prop for prop in detail(model)["direct"]["properties"]}


def test_legacy_inventory_remains_compatible_and_unknown_models_remain_empty():
    result = capabilities()
    assert result["version"] == 1
    assert result["broadcast"] == object_catalog()
    assert result["direct"]["ES5BB"]["properties"] == [asdict(prop) for prop in PROPERTIES]
    for name, props in PROFILES.items():
        assert result["direct"][name]["properties"] == [prop.public() for prop in props]
    assert result["direct"]["HS3BB"]["model_confirmation_required"] is True
    assert capabilities("unknown") == {"version": 1, "broadcast": {}, "direct": {},
                                        "details": {"schema_version": 1, "models": {}}}


@pytest.mark.parametrize("model,pid,suffix,count", [
    ("ES5BB", 0x6A0F, "linp.sensor_occupy.es5b", 56),
    ("MS1BB", 0x1889, "linp.magnet.m1", 0),
    ("KS2BB", 0x520B, "linp.sensor_ht.ks2bb", 2),
    ("HS3BB", 0x50D4, "linp.motion.hs1bb2", 6),
])
def test_model_filter_and_confirmed_identity_have_no_per_device_identifiers(model, pid, suffix, count):
    result = capabilities(model)
    assert set(result["details"]["models"]) == set(result["broadcast"]) == set(result["direct"]) == {model}
    info = detail(model)
    assert info["identity"]["pid"] == pid
    assert info["identity"]["pid_hex"] == f"0x{pid:04X}"
    assert info["identity"]["protocol_model"] == suffix
    assert len(info["direct"]["properties"]) == count
    assert "candidate" not in json.dumps(info)
    # This is metadata, with no address, auth token, bindkey value, serial or
    # current property value that could accidentally expose a configured home.
    assert set(info["credentials"]["broadcast"]) == {"kind", "bytes"}
    for prop in info["direct"]["properties"]:
        assert "value" not in prop and "updated_at" not in prop
    assert "address" not in info["identity"] and "serial_number" not in info["identity"]


def test_evidence_groups_do_not_upgrade_unobserved_battery_or_compatibility_objects():
    ks2 = detail("KS2BB")["advertisement"]
    assert set(ks2["observed"]) == {"0x4801", "0x4802", "0x560C", "0x560D", "0x560E"}
    assert "0x5003" in ks2["protocol_defined_not_observed"]
    assert set(ks2["compatibility_only"]) == {"0x4C01", "0x4C08"}
    es5 = detail("ES5BB")["advertisement"]
    assert set(es5["observed"]) == {"0x4805", "0x484E", "0x5413", "0x5C03"}
    assert es5["independent_pir"] == "not_observed"
    assert set(es5["protocol_defined_not_observed"]) == {"0x4851", "0x4852", "0x5661"}
    ms1 = detail("MS1BB")["advertisement"]
    assert "0x4A12" in ms1["observed"] and "0x4A13" in ms1["protocol_defined_not_observed"]


def test_declared_miot_permissions_never_become_assumed_gatt_read_success():
    es5 = properties("ES5BB")
    assert es5["illuminance"]["specification"]["gatt_access"] == []
    assert es5["battery_level"]["specification"]["gatt_access"] is None
    assert es5["occupancy_status"]["specification"]["gatt_access"] == ["notify"]
    assert es5["occupancy_status"]["validation"]["read"] == "not_verified"
    assert es5["pir_energy"]["specification"]["gatt_access"] == ["notify"]
    assert es5["pir_energy"]["sdk_access"] == {"get": False, "set": False, "notify": True, "notification_only": True}
    assert es5["motion_energy"]["validation"]["notify"] == "samples_observed"
    assert es5["motion_energy"]["validation"]["read"] == "not_verified"
    assert es5["pir_energy"]["validation"]["notify"] == "no_samples"
    assert es5["pir_energy"]["validation"]["confidence"] == "unconfirmed"
    assert es5["pir_energy"]["validation"]["sample_count"] == 0


def test_hardware_verification_does_not_open_unknown_or_out_of_spec_writes():
    assert detail("MS1BB")["direct"]["settings_exposed"] is False
    assert detail("MS1BB")["credentials"]["direct"] is None
    for prop in properties("KS2BB").values():
        assert prop["validation"]["write"] == "verified_readback_restored"
    hs3 = properties("HS3BB")
    for key in ("indicator_light", "detection_interval"):
        assert hs3[key]["validation"]["write"] == "verified_readback_restored"
    for key in ("no_motion_time", "pir_single_report"):
        assert hs3[key]["validation"]["read"] == "verified"
        assert hs3[key]["validation"]["write"] == "not_verified"
    for key in ("no_motion_timeout_1", "no_motion_timeout_2"):
        assert hs3[key]["validation"]["read"] == "out_of_spec_value"
        assert hs3[key]["validation"]["write"] == "not_verified"
    es5 = properties("ES5BB")
    assert es5["manual_off_detection_seconds"]["sdk_access"]["set"] is False
    assert es5["cmd_to_device"]["sdk_access"]["set"] is False
    assert es5["pir_threshold"]["validation"]["write"] == "not_verified"
    assert "fresh_valid_read" in detail("HS3BB")["direct"]["write_requirements"]


def test_pir_controls_and_null_responses_do_not_become_fabricated_energy_or_current_firmware():
    validation = properties("ES5BB")["pir_energy"]["validation"]
    evidence = validation["notify_evidence"]
    assert validation["notify"] == "no_samples" and validation["sample_count"] == 0
    assert validation["report"] == "docs/PIR_INVESTIGATION.md"
    assert evidence["radar_group_count"] == 33
    assert evidence["pir_bitmap_flag_counts"] == {"0": 31, "1": 2}
    assert evidence["null_property_responses"] == 2
    assert evidence["null_response_operation"] == 3 and evidence["null_response_type"] == "0xF0"
    assert evidence["raw_header_count"] == 162 and evidence["raw_message_kinds"] == [0]
    assert evidence["mic_errors"] == evidence["session_errors"] == 0
    assert evidence["final_original_settings_matched"] == 24 and evidence["stop_acknowledged"] is True
    assert evidence["modes_tested"] == [2, 1] and evidence["subscription_variants_tested"] is True
    assert evidence["official_es5_debug_chart_entry_visible"] is False
    assert evidence["native_sdk_subscription_wire_verified"] is False
    assert evidence["permanent_unsupported_proven"] is False
    assert evidence["firmware"] == {"historical_version": "2.1.2_0156", "current_query": "not_obtained"}


def test_es5_investigation_reports_all_valid_reads_but_does_not_upgrade_unverified_writes_or_gets():
    props = properties("ES5BB")
    read_names = {name for name, prop in props.items() if prop["validation"]["read"] == "verified"}
    assert read_names == {
        "ai_anti_interference", "connection_wake_time", "detect_mode", "detect_region", "function_mode",
        "indicator_light", "interest_enable", "interest_region", "manual_off_activation_lux",
        "manual_off_deactivation_delta_lux", "manual_off_deactivation_lux", "manual_off_deactivation_seconds",
        "manual_off_illumination_delta", "motion_threshold", "no_one_determine_time", "no_one_report_mask",
        "pir_sensitivity", "pir_threshold", "presence_threshold", "radar_motion_sens", "radar_presence_sens",
        "sensitivity_level", "sleep_determine_time", "someone_report_mask",
    }
    assert len(read_names) == 24
    assert props["detect_mode"]["validation"]["write"] == "verified_readback_restored"
    assert props["detect_mode"]["validation"]["report"] == "docs/PIR_INVESTIGATION.md"
    for name in ("occupancy_status", "power_mode"):
        assert props[name]["validation"]["notify"] == "samples_observed"
        assert props[name]["validation"]["read"] == "not_verified"
        assert props[name]["validation"]["report"] == "docs/PIR_INVESTIGATION.md"
    for name in ("indicator_light", "pir_threshold", "sensitivity_level", "function_mode", "ai_anti_interference"):
        assert props[name]["validation"]["write"] == "not_verified"
    assert props["connection_wake_time"]["validation"]["report"] == "docs/PIR_INVESTIGATION.md"
    assert props["radar_frame_rate"]["validation"]["read"] == "not_verified"


def test_wake_evidence_and_mesh_do_not_invent_universal_hardware_requirements():
    for model in ("MS1BB", "KS2BB"):
        wake = detail(model)["wake"]
        assert wake["status"] == "unknown" and wake["action"] is None and wake["required"] is None
    hs3 = detail("HS3BB")["wake"]
    assert hs3["status"] == "observed_on_tested_device" and hs3["controlled_test_pending"] is False
    assert hs3["action"] == "short_press_once" and hs3["required"] is None
    observation = hs3["controlled_observation"]
    assert observation["seconds"] == 180 and observation["advertisement_type"] == 3
    assert observation["button_confirmation"] == "not_received"
    assert observation["gatt_attempted"] is False and observation["settings_written"] is False
    assert observation["phase"] == "earlier_pre_button_window"
    sequence = hs3["observed_sequence"]
    assert sequence["comparison"] == "separate_scan_windows"
    assert sequence["before"]["advertisement_type"] == 3
    assert sequence["before"]["connectable"] is False and sequence["before"]["button_pressed"] is False
    assert sequence["after"]["advertisement_type"] == 0 and sequence["after"]["connectable"] is True
    assert sequence["after"]["native_age_seconds"] == 0.016
    assert sequence["authentication"]["status"] == "verified"
    assert sequence["authentication"]["valid_read_names"] == ["indicator_light", "detection_interval"]
    assert sequence["write_count"] == 0 and sequence["connection_closed"] is True
    assert sequence["exclusive_cause_established"] is False and sequence["other_settings_changed"] is False
    assert detail("ES5BB")["wake"]["status"] == "tested_workflow"
    for model in ("ES5BB", "MS1BB", "KS2BB", "HS3BB"):
        runtime = detail(model)["runtime"]
        assert runtime["xiaomi_gateway_required"] is False
        assert runtime["mesh"]["status"] == "not_supported_by_profiles"
        assert runtime["transport_validation"]["esphome_bluetooth_proxy"] == "simulation_only"


def test_independent_results_and_schema_are_json_safe():
    first = capabilities("KS2BB")
    first["details"]["models"]["KS2BB"]["advertisement"]["observed"].clear()
    assert detail("KS2BB")["advertisement"]["observed"]
    schema = capabilities_schema()
    assert json.loads(json.dumps(capabilities()))["details"]["schema_version"] == 1
    assert json.loads(json.dumps(schema))["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    schema["properties"]["details"]["properties"]["schema_version"]["const"] = 123
    assert capabilities_schema()["properties"]["details"]["properties"]["schema_version"]["const"] == 1


def test_standards_schema_validation_when_jsonschema_is_available():
    jsonschema = pytest.importorskip("jsonschema")
    schema = capabilities_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(capabilities())
    validator.validate(capabilities("HS3BB"))
    validator.validate(capabilities("unknown"))
    malformed = capabilities("HS3BB")
    malformed["details"]["models"]["HS3BB"]["direct"]["properties"][0]["validation"]["read"] = "all_firmware_supported"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(malformed)
    malformed_pir = capabilities("ES5BB")
    pir = next(p for p in malformed_pir["details"]["models"]["ES5BB"]["direct"]["properties"] if p["name"] == "pir_energy")
    pir["validation"]["sample_count"] = 1
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(malformed_pir)


def test_inventory_does_not_start_application_io_or_read_home_configuration():
    root = Path(__file__).resolve().parents[1]
    code = '''
import asyncio, pathlib, socket, threading, sys
def forbidden(*args, **kwargs):
    raise AssertionError("Capability inspection attempted application I/O")
socket.socket = forbidden
threading.Thread.start = forbidden
original_read = pathlib.Path.read_text
def public_spec_only(path, *args, **kwargs):
    assert path.parent.name == "specs" and path.name in {"linp.sensor_occupy.es5b.json", "linp.magnet.m1.json", "linp.sensor_ht.ks2bb.json", "linp.motion.hs1bb2.json"}
    return original_read(path, *args, **kwargs)
pathlib.Path.read_text = public_spec_only
from linptech_ble.capabilities import capabilities, capabilities_schema
assert len(capabilities()["details"]["models"]) == 4
assert capabilities_schema()["properties"]["version"]["const"] == 1
for name in ("linptech_ble.dashboard", "linptech_ble.es5_direct", "linptech_ble.sensor_direct", "linptech_ble.transport", "paho", "aioesphomeapi"):
    assert name not in sys.modules, name
print("safe-inventory")
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "safe-inventory"
