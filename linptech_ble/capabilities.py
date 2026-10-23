"""Public, credential-free protocol inventory and its versioned JSON schema.

Evidence describes the project's tested devices, not the caller's live device.
Only packaged public specifications are read; no scanner or controller is owned.
"""
from __future__ import annotations

from dataclasses import asdict
from importlib.resources import files
import json

from .broadcast_objects import SPEC_BASE, SPEC_TYPES, object_catalog
from .property_catalog import PROPERTIES, PROFILES, PROTOCOL_MODELS


_MODELS = {"ES5BB": (0x6A0F, "linp.sensor_occupy.es5b"),
           "MS1BB": (0x1889, "linp.magnet.m1"),
           "KS2BB": (0x520B, "linp.sensor_ht.ks2bb"),
           "HS3BB": (0x50D4, "linp.motion.hs1bb2")}
_REPORT = "docs/HARDWARE_VALIDATION_2026-10-23.md"
_STREAM_FIELDS = {"motion_energy", "presence_energy", "human_sensor_bitmaps", "pir_energy"}
_HISTORICAL_ES5_READS = {"no_one_determine_time", "pir_sensitivity", "pir_threshold", "detect_mode",
                         "motion_threshold", "presence_threshold", "detect_region", "interest_region"}
# Names only from the 24 valid originals/final readbacks in the investigation.
# No device values, captured traffic or credentials are loaded by this module.
_PIR_INVESTIGATION_READS = {
    "ai_anti_interference", "connection_wake_time", "detect_mode", "detect_region", "function_mode",
    "indicator_light", "interest_enable", "interest_region", "manual_off_activation_lux",
    "manual_off_deactivation_delta_lux", "manual_off_deactivation_lux", "manual_off_deactivation_seconds",
    "manual_off_illumination_delta", "motion_threshold", "no_one_determine_time", "no_one_report_mask",
    "pir_sensitivity", "pir_threshold", "presence_threshold", "radar_motion_sens", "radar_presence_sens",
    "sensitivity_level", "sleep_determine_time", "someone_report_mask",
}
_PIR_INVESTIGATION_NEW_EVIDENCE = (_PIR_INVESTIGATION_READS - _HISTORICAL_ES5_READS) | {
    "detect_mode", "occupancy_status", "power_mode", "pir_energy"}
_WRITE_VERIFIED = {"ES5BB": {"no_one_determine_time", "pir_sensitivity", "detect_mode"},
                   "MS1BB": set(), "KS2BB": {"button_mode", "temperature_unit"},
                   "HS3BB": {"indicator_light", "detection_interval"}}
_READ_VERIFIED = {"ES5BB": _PIR_INVESTIGATION_READS,
                  "MS1BB": set(), "KS2BB": {"button_mode", "temperature_unit"},
                  "HS3BB": {"indicator_light", "detection_interval", "no_motion_time", "pir_single_report"}}
_NOTIFY_VERIFIED = {"ES5BB": {"motion_energy", "presence_energy", "human_sensor_bitmaps", "occupancy_status", "power_mode"}}
_LEGACY_VALIDATION = {
    "ES5BB": "Standard Auth and configuration reads verified on hardware. PIR debug-energy tests on 2026-10-23 produced no 6.18 samples despite valid radar/PIR-flag controls, command ACKs and restored settings. Firmware not exposing the debug channel is likely; native SDK subscription details remain unverified, so permanent unsupported status is not established.",
    "MS1BB": "Released v1 has no exposed GATT settings.",
    "KS2BB": "Standard Auth and both settings verified by write/readback/restore on 2026-10-23; results remain firmware-specific.",
    "HS3BB": "Standard Auth, indicator light and detection interval verified by write/readback/restore on 2026-10-23. A/B timeouts returned out-of-spec zero and were not written; other settings are not write-verified.",
}


def _spec_properties(protocol_model: str) -> dict[tuple[int, int], dict]:
    resource = files("linptech_ble").joinpath("specs", protocol_model + ".json")
    spec = json.loads(resource.read_text(encoding="utf-8"))
    return {(service["iid"], prop["iid"]): prop for service in spec["services"]
            for prop in service.get("properties", [])}


def _property_detail(model: str, prop, specification: dict) -> dict:
    name = prop.name
    invalid_read = model == "HS3BB" and name in {"no_motion_timeout_1", "no_motion_timeout_2"}
    notify_only = getattr(prop, "notification_only", False)
    # Published transport permissions and SDK operations are distinct. A GET
    # attempt for a notify property does not establish readable firmware support.
    declared_access = specification.get("gatt-access")
    read = "out_of_spec_value" if invalid_read else "verified" if name in _READ_VERIFIED[model] else "not_verified"
    notify = "samples_observed" if name in _NOTIFY_VERIFIED.get(model, set()) else "no_samples" if name == "pir_energy" else "not_verified"
    write = "verified_readback_restored" if name in _WRITE_VERIFIED[model] else "not_verified" if prop.writable else "not_allowed"
    return {"name": name, "siid": prop.siid, "piid": prop.piid, "format": prop.format,
            "specification": {"miot_access": list(specification.get("access", [])),
                              "gatt_access": list(declared_access) if declared_access is not None else None},
            "sdk_access": {"get": not notify_only, "set": prop.writable,
                           "notify": notify_only or name in _STREAM_FIELDS or getattr(prop, "notify", False),
                           "notification_only": notify_only},
            "validation": {"read": read, "notify": notify, "write": write,
                           "scope": "tested_device" if read != "not_verified" or notify != "not_verified" or write == "verified_readback_restored" else "protocol_definition",
                           "report": _REPORT if model != "ES5BB" else "docs/PIR_INVESTIGATION.md" if name in _PIR_INVESTIGATION_NEW_EVIDENCE else "docs/ES5BB_ADVANCED.md",
                           **({"diagnostic": "Returned zero outside the published 60..3600 second range; no write attempted."} if invalid_read else {}),
                           **({"confidence": "unconfirmed", "command_acknowledged": True,
                               "report": "docs/PIR_INVESTIGATION.md",
                               "observation_seconds": [60, 180], "sample_count": 0,
                               "notify_evidence": {
                                   "date": "2026-10-23", "radar_group_count": 33,
                                   "pir_bitmap_flag_counts": {"0": 31, "1": 2},
                                   "null_property_responses": 2, "null_response_operation": 3,
                                   "null_response_type": "0xF0", "raw_header_count": 162,
                                   "raw_message_kinds": [0], "mic_errors": 0, "session_errors": 0,
                                   "final_original_settings_matched": 24, "stop_acknowledged": True,
                                   "modes_tested": [2, 1], "subscription_variants_tested": True,
                                   "official_ui_pir_source": "6.8 bitmap field 8",
                                   "official_es5_debug_chart_entry_visible": False,
                                   "likely_cause": "firmware_not_exposing_debug_channel",
                                   "native_sdk_subscription_wire_verified": False,
                                   "permanent_unsupported_proven": False,
                                   "firmware": {"historical_version": "2.1.2_0156", "current_query": "not_obtained"}},
                               "diagnostic": "No debug-energy samples from the tested device and flows. Firmware not exposing this channel is likely, but unpublished native SDK subscription details are not fully excluded. Historical firmware version is not a new query result."} if name == "pir_energy" else {})}}


def _model_detail(model: str, broadcast: list[dict]) -> dict:
    pid, protocol_model = _MODELS[model]
    properties = PROPERTIES if model == "ES5BB" else PROFILES[model]
    specification = _spec_properties(protocol_model)
    groups = {key: [obj["object_id_hex"] for obj in broadcast if obj["evidence"] == value]
              for key, value in (("observed", "observed_capture"),
                                 ("protocol_defined_not_observed", "protocol_only"),
                                 ("compatibility_only", "upstream_compatibility_only"))}
    wake = {"status": "unknown", "action": None, "required": None,
            "scope": "No model-specific wake procedure established by this project.",
            "controlled_test_pending": False}
    if model == "ES5BB":
        wake.update(status="tested_workflow", action="short_press_once", required=None,
                    scope="Short press was used in successful tested-device connections; no universal requirement or guaranteed window is established.")
    elif model == "HS3BB":
        wake.update(status="observed_on_tested_device", action="short_press_once", required=None,
                    instruction="Short press the device button once; do not long press or reset.",
                    scope="On the same tested device, a separate pre-button scan saw nonconnectable advertisements; after confirmed short press a fresh connectable advertisement, Standard Auth and two valid reads succeeded. This does not establish a universal requirement or a single cause for earlier failures.",
                    controlled_test_pending=False,
                    controlled_observation={"seconds": 180, "advertisement_type": 3,
                                            "button_confirmation": "not_received", "gatt_attempted": False,
                                            "settings_written": False, "phase": "earlier_pre_button_window",
                                            "conclusion": "This earlier window ended before button confirmation; it was not a failed post-button result."},
                    observed_sequence={
                        "report": "docs/HS3BB_WAKE.md", "scope": "tested_device",
                        "comparison": "separate_scan_windows", "button_confirmation": "received_before_after_window",
                        "before": {"timestamps": ["2026-10-23T10:23:29+08:00", "2026-10-23T10:23:30+08:00"],
                                   "advertisement_type": 3, "connectable": False, "button_pressed": False},
                        "after": {"timestamp": "2026-10-23T10:45:41+08:00", "advertisement_type": 0,
                                  "connectable": True, "native_age_seconds": 0.016, "rssi": -91},
                        "authentication": {"timestamp": "2026-10-23T10:45:45+08:00", "status": "verified",
                                           "valid_read_names": ["indicator_light", "detection_interval"]},
                        "write_count": 0, "connection_closed": True,
                        "other_settings_changed": False, "exclusive_cause_established": False})
    return {
        "identity": {"model": model, "pid": pid, "pid_hex": f"0x{pid:04X}",
                     "protocol_model": protocol_model, "spec_type": SPEC_TYPES[model],
                     "spec_source": SPEC_BASE + SPEC_TYPES[model],
                     "evidence": "cloud_model_and_authenticated_gatt" if model in {"ES5BB", "KS2BB", "HS3BB"} else "cloud_model_and_authenticated_advertisement",
                     "scope": "tested_device", "model_confirmation_required": model == "HS3BB"},
        "runtime": {"xiaomi_gateway_required": False, "cloud_required_after_credentials": False,
                    "transports": ["local_ble", "esphome_bluetooth_proxy"],
                    "transport_validation": {"local_ble": "hardware_verified", "esphome_bluetooth_proxy": "simulation_only"},
                    "mesh": {"status": "not_supported_by_profiles", "relay": False,
                             "scope": "This library implements MiBeacon advertising and authenticated GATT, not BLE Mesh provisioning or relaying."}},
        "credentials": {"broadcast": {"kind": "bindkey", "bytes": 16},
                        "direct": {"kind": "standard_auth_token", "bytes": 12} if properties else None,
                        "proxy_api_key_is_separate": True},
        "wake": wake,
        "advertisement": {**groups, "evidence_scope": "Authenticated captures from tested devices; not live state or a guarantee of reporting frequency.",
                          "independent_pir": "not_observed" if model == "ES5BB" else "not_applicable"},
        "direct": {"settings_exposed": bool(properties),
                   "authentication": "verified" if model != "MS1BB" else "not_tested_no_settings",
                   "properties": [_property_detail(model, prop, specification.get((prop.siid, prop.piid), {})) for prop in properties],
                   "write_requirements": ["authenticated_current_session", "fresh_valid_read", "model_allowlist", "typed_value_and_range", "independent_readback"],
                   "hardware_scope": "Specific tested devices and firmware; the caller must validate its own session.",
                   "report": _REPORT},
    }


def capabilities(model: str | None = None) -> dict:
    """Return JSON-compatible definitions and evidence, never live device state.

    ``version/broadcast/direct`` retain the original v1 API. New consumers should
    use ``details.schema_version`` and distinguish declared access from tested
    outcomes. Unknown model names preserve the legacy empty-result behavior.
    """
    broadcast = object_catalog(model)
    direct = {}
    if model in (None, "ES5BB"):
        direct["ES5BB"] = {"protocol_model": _MODELS["ES5BB"][1], "authentication": "Standard Auth / 12-byte token",
                           "properties": [asdict(prop) for prop in PROPERTIES], "validation": _LEGACY_VALIDATION["ES5BB"]}
    for name, props in PROFILES.items():
        if model not in (None, name):
            continue
        direct[name] = {"protocol_model": PROTOCOL_MODELS[name], "properties": [prop.public() for prop in props],
                        "validation": _LEGACY_VALIDATION[name], "model_confirmation_required": name == "HS3BB"}
    return {"version": 1, "broadcast": broadcast, "direct": direct,
            "details": {"schema_version": 1, "models": {name: _model_detail(name, objects) for name, objects in broadcast.items()}}}


def capabilities_schema() -> dict:
    """Return JSON Schema 2020-12 for the additive capability details contract.

    Legacy inventories remain extensible; detailed evidence states and access
    booleans are typed so applications can validate without a web dependency.
    A fresh mutable dictionary is returned on each call.
    """
    def object_schema(properties: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": properties, "required": required or list(properties), "additionalProperties": True}

    boolean = {"type": "boolean"}
    text = {"type": "string"}
    strings = {"type": "array", "items": text}
    validation_schema = object_schema({
        "read": {"enum": ["verified", "out_of_spec_value", "not_verified"]},
        "notify": {"enum": ["samples_observed", "no_samples", "not_verified"]},
        "write": {"enum": ["verified_readback_restored", "not_verified", "not_allowed"]},
        "scope": {"enum": ["tested_device", "protocol_definition"]}, "report": text})
    validation_schema["properties"].update({
        "confidence": {"enum": ["unconfirmed"]}, "command_acknowledged": boolean,
        "observation_seconds": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "sample_count": {"type": "integer", "minimum": 0}, "diagnostic": text})
    count = {"type": "integer", "minimum": 0}
    validation_schema["properties"]["notify_evidence"] = object_schema({
        "date": {"type": "string", "format": "date"}, "radar_group_count": count,
        "pir_bitmap_flag_counts": object_schema({"0": count, "1": count}),
        "null_property_responses": count, "null_response_operation": {"type": "integer"},
        "null_response_type": text, "raw_header_count": count,
        "raw_message_kinds": {"type": "array", "items": {"type": "integer"}},
        "mic_errors": count, "session_errors": count, "final_original_settings_matched": count,
        "stop_acknowledged": boolean, "modes_tested": {"type": "array", "items": {"type": "integer"}},
        "subscription_variants_tested": boolean, "official_ui_pir_source": text,
        "official_es5_debug_chart_entry_visible": boolean, "likely_cause": text,
        "native_sdk_subscription_wire_verified": boolean, "permanent_unsupported_proven": boolean,
        "firmware": object_schema({"historical_version": text, "current_query": {"enum": ["not_obtained"]}})})
    validation_schema["allOf"] = [{"if": {"properties": {"notify": {"const": "no_samples"}}, "required": ["notify"]},
                                    "then": {"properties": {"sample_count": {"const": 0}}, "required": ["sample_count"]}}]
    property_schema = object_schema({
        "name": text, "siid": {"type": "integer", "minimum": 1}, "piid": {"type": "integer", "minimum": 1}, "format": text,
        "specification": object_schema({"miot_access": strings, "gatt_access": {"type": ["array", "null"], "items": {"enum": ["read", "write", "notify"]}}}),
        "sdk_access": object_schema({name: boolean for name in ("get", "set", "notify", "notification_only")}),
        "validation": validation_schema})
    wake_schema = object_schema({"status": {"enum": ["unknown", "tested_workflow", "user_reported", "controlled_observation", "observed_on_tested_device"]},
                                 "action": {"type": ["string", "null"]}, "required": {"type": ["boolean", "null"]},
                                 "scope": text, "controlled_test_pending": boolean})
    wake_schema["properties"]["instruction"] = text
    wake_schema["properties"]["controlled_observation"] = object_schema({
        "seconds": {"type": "integer", "minimum": 1}, "advertisement_type": {"type": "integer"},
        "button_confirmation": {"enum": ["not_received"]}, "gatt_attempted": boolean,
        "settings_written": boolean, "conclusion": text})
    timestamp = {"type": "string", "format": "date-time"}
    wake_schema["properties"]["observed_sequence"] = object_schema({
        "report": text, "scope": {"const": "tested_device"}, "comparison": {"const": "separate_scan_windows"},
        "button_confirmation": {"const": "received_before_after_window"},
        "before": object_schema({"timestamps": {"type": "array", "items": timestamp}, "advertisement_type": {"type": "integer"},
                                 "connectable": boolean, "button_pressed": boolean}),
        "after": object_schema({"timestamp": timestamp, "advertisement_type": {"type": "integer"}, "connectable": boolean,
                                "native_age_seconds": {"type": "number", "minimum": 0}, "rssi": {"type": "integer"}}),
        "authentication": object_schema({"timestamp": timestamp, "status": {"const": "verified"}, "valid_read_names": strings}),
        "write_count": count, "connection_closed": boolean, "other_settings_changed": boolean, "exclusive_cause_established": boolean})
    model_schema = object_schema({
        "identity": object_schema({"model": {"enum": list(_MODELS)}, "pid": {"type": "integer"}, "pid_hex": text,
                                   "protocol_model": text, "spec_type": text, "spec_source": text, "evidence": text,
                                   "scope": {"const": "tested_device"}, "model_confirmation_required": boolean}),
        "runtime": object_schema({"xiaomi_gateway_required": {"const": False}, "cloud_required_after_credentials": {"const": False},
                                  "transports": strings,
                                  "transport_validation": object_schema({"local_ble": {"const": "hardware_verified"}, "esphome_bluetooth_proxy": {"const": "simulation_only"}}),
                                  "mesh": object_schema({"status": {"const": "not_supported_by_profiles"}, "relay": {"const": False}, "scope": text})}),
        "credentials": object_schema({"broadcast": object_schema({"kind": {"const": "bindkey"}, "bytes": {"const": 16}}),
                                      "direct": {"anyOf": [{"type": "null"}, object_schema({"kind": {"const": "standard_auth_token"}, "bytes": {"const": 12}})]},
                                      "proxy_api_key_is_separate": {"const": True}}),
        "wake": wake_schema,
        "advertisement": object_schema({"observed": strings, "protocol_defined_not_observed": strings, "compatibility_only": strings,
                                        "evidence_scope": text, "independent_pir": {"enum": ["not_observed", "not_applicable"]}}),
        "direct": object_schema({"settings_exposed": boolean, "authentication": {"enum": ["verified", "not_tested_no_settings"]},
                                 "properties": {"type": "array", "items": property_schema}, "write_requirements": strings,
                                 "hardware_scope": text, "report": text})})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Linptech capability inventory",
            **object_schema({"version": {"const": 1}, "broadcast": {"type": "object"}, "direct": {"type": "object"},
                             "details": object_schema({"schema_version": {"const": 1}, "models": {"type": "object", "propertyNames": {"enum": list(_MODELS)}, "additionalProperties": model_schema}})})}
