"""Control allowlist, cache validity and credential boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from linptech_ble.config import DeviceConfig, load_config, save_config, validate_auth_token
from linptech_ble.es5_direct import CATALOG, DirectController, numeric_array
from linptech_ble.standard_auth import DirectError
from linptech_ble.es5_radar import EmptyRoomScan, encode_series

TOKEN = "00112233445566778899aabb"
KEY = "00112233445566778899aabbccddeeff"


def controller(tmp_path):
    c = DirectController(DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Test", KEY, TOKEN), recording_dir=tmp_path)
    c.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args), time=lambda: 0)
    c.commands = asyncio.Queue()
    return c


def result(value=1, code=0x10):
    return {"siid": 6, "piid": 11, "status": 0, "type_code": code, "value": value, "tid": 1}


def test_token_config_and_repr_are_distinct_from_bindkey(tmp_path):
    device = DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Test", KEY, TOKEN.upper())
    assert TOKEN not in repr(device) and KEY not in repr(device)
    save_config(tmp_path / "secrets.json", [device])
    assert load_config(tmp_path / "secrets.json")[0].auth_token == TOKEN
    with pytest.raises(ValueError) as error:
        validate_auth_token(KEY)
    assert KEY not in str(error.value)


def test_controls_require_current_authenticated_read_and_never_expose_secrets(tmp_path):
    c = controller(tmp_path)
    with pytest.raises(DirectError):
        c.submit({"action": "set", "property": "sensitivity_level", "value": 2})
    c._update(authenticated=True, status="ready")
    with pytest.raises(DirectError, match="先成功讀取"):
        c.submit({"action": "set", "property": "sensitivity_level", "value": 2})
    c._accept(result())
    operation_id = c.submit({"action": "set", "property": "sensitivity_level", "value": 0})
    assert c.commands.get_nowait() == {"action": "set", "property": "sensitivity_level", "value": 0, "id": operation_id}
    with pytest.raises(DirectError, match="正在進行"):
        c.submit({"action": "refresh"})
    public = json.dumps(c.snapshot())
    assert TOKEN not in public and KEY not in public
    assert c.snapshot()["calibration"]["available"] is False


def test_disconnect_invalidates_prior_read_permission_without_deleting_values(tmp_path):
    c = controller(tmp_path)
    c._update(authenticated=True, status="ready")
    c._accept(result(2))
    asyncio.run(c._disconnect())
    state = c.snapshot()
    assert state["properties"]["sensitivity_level"]["value"] == 2
    assert state["properties"]["sensitivity_level"]["status"] == "cached"
    c._update(authenticated=True, status="ready")
    with pytest.raises(DirectError, match="先成功讀取"):
        c.submit({"action": "set", "property": "sensitivity_level", "value": 1})


@pytest.mark.parametrize("name,value", [("sensitivity_level", 4), ("sensitivity_level", True), ("no_one_determine_time", 10001), ("no_one_determine_time", -1), ("detect_region", 2**32), ("motion_energy", "1,2,3")])
def test_read_only_properties_and_invalid_settings_cannot_be_written(name, value):
    with pytest.raises(DirectError):
        CATALOG[name].validate(value)


def test_different_wire_type_cannot_enable_setting_controls(tmp_path):
    c = controller(tmp_path)
    with pytest.raises(DirectError, match="型別"):
        c._accept(result(2, 0x30))
    assert c.snapshot()["properties"]["sensitivity_level"]["status"] == "unread"


@pytest.mark.parametrize("value", ["AQID", "1,2,3", "[true,2]", "[]", "[NaN,2]", "{\"bins\":[1,2]}", [float("inf")]])
def test_energy_decoder_does_not_guess_unknown_encodings(value):
    assert numeric_array(value) is None


def test_energy_arrays_and_false_property_are_preserved(tmp_path):
    assert numeric_array("[0, 2.5, 9]") == [0.0, 2.5, 9.0]
    c = controller(tmp_path)
    c._accept({"siid": 6, "piid": 72, "status": 0, "type_code": 0, "value": False})
    assert c.snapshot()["properties"]["interest_enable"]["value"] is False


def test_battery_read_accepts_zero_and_rejects_out_of_range_and_writes(tmp_path):
    c = controller(tmp_path)
    record = {"siid": 7, "piid": 1003, "status": 0, "type_code": 0x10, "value": 0}
    c._accept(record)
    assert c.snapshot()["properties"]["battery_level"]["value"] == 0
    with pytest.raises(DirectError, match="範圍"):
        c._accept({**record, "value": 101})
    assert c.snapshot()["properties"]["battery_level"]["value"] == 0
    with pytest.raises(DirectError, match="僅供讀取"):
        CATALOG["battery_level"].validate(100)


def test_calibration_requires_explicit_empty_room_preparation_and_current_reads(tmp_path):
    c = controller(tmp_path)
    c._update(authenticated=True, status="ready")
    with pytest.raises(DirectError, match="確認裝置已固定"):
        c.submit({"action": "radar_calibrate"})
    with pytest.raises(DirectError, match="即時資料"):
        c.submit({"action": "calibrate", "empty_room": True})
    c._cal_state(phase="scanning")
    with pytest.raises(DirectError, match="校正正在進行"):
        c.submit({"action": "refresh"})
    assert c.submit({"action": "calibration_cancel"})


@pytest.mark.parametrize("fail", [False, True])
def test_calibration_apply_verifies_final_values_and_restores_after_partial_failure(tmp_path, fail):
    c = controller(tmp_path)
    originals = {"motion_threshold": encode_series([30] * 32), "presence_threshold": encode_series([40] * 32),
                 "radar_motion_sens": 1, "radar_presence_sens": 2}
    proposed = {"motion_threshold": encode_series([50] * 32), "presence_threshold": encode_series([60] * 32)}
    class Session:
        def __init__(self):
            self.values = {(CATALOG[n].siid, CATALOG[n].piid): v for n, v in originals.items()}
            self.failed, self.commands = False, []
        async def read(self, siid, piid, **kw):
            prop = next(p for p in CATALOG.values() if (p.siid, p.piid) == (siid, piid))
            from linptech_ble.standard_auth import encode_value
            return {"siid": siid, "piid": piid, "status": 0, "value": self.values[(siid, piid)], "type_code": encode_value(self.values[(siid, piid)], prop.format)[0]}
        async def set(self, siid, piid, value, format):
            if fail and piid == 3 and not self.failed:
                self.failed = True
                raise DirectError("synthetic failed write")
            self.values[(siid, piid)] = value
            return await self.read(siid, piid)
        async def command(self, value):
            self.commands.append(value)
            return True
    c.session = Session()
    c.scan = EmptyRoomScan(originals, 0)
    c._cal_state(phase="preview", proposed=proposed)
    async def run():
        if fail:
            with pytest.raises(DirectError, match="synthetic"):
                await c._calibration_action("calibration_apply")
            for name, value in originals.items():
                assert c.session.values[(CATALOG[name].siid, CATALOG[name].piid)] == value
        else:
            await c._calibration_action("calibration_apply")
            assert c.snapshot()["calibration"]["phase"] == "applied"
            assert c.session.commands == [512]
            await c._calibration_action("calibration_restore")
            for name, value in originals.items():
                assert c.session.values[(CATALOG[name].siid, CATALOG[name].piid)] == value
    asyncio.run(run())


def accepted(c, name, value):
    from linptech_ble.standard_auth import encode_value
    p = CATALOG[name]
    c._accept({"siid": p.siid, "piid": p.piid, "status": 0,
        "type_code": encode_value(value, p.format)[0], "value": value})


def test_pir_signed_energy_preserves_negative_zero_and_full_range_without_mobile_chart_clipping(tmp_path):
    c = controller(tmp_path)
    c.state["streaming"].update(requested_mode="pir", mode="pir")
    for value in (-1000, -72, 0, 80, 1000):
        accepted(c, "pir_energy", value)
    accepted(c, "pir_threshold", 0)
    pir = c.snapshot()["pir"]
    assert [p["value"] for p in pir["samples"]] == [-1000, -72, 0, 80, 1000]
    assert (pir["value"], pir["threshold"], pir["unit"]) == (1000, 0, "raw")
    assert pir["active"] and not c.snapshot()["streaming"]["active"]
    for value in (-1001, 1001):
        with pytest.raises(DirectError, match="範圍"):
            accepted(c, "pir_energy", value)
    with pytest.raises(DirectError, match="型別"):
        c._accept({"siid": 6, "piid": 18, "status": 0, "type_code": 0x50, "value": 1})
    for _ in range(260):
        accepted(c, "pir_energy", -5)
    assert len(c.snapshot()["pir"]["samples"]) == 256
    with pytest.raises(DirectError, match="僅供讀取"):
        CATALOG["pir_energy"].validate(-5)


@pytest.mark.parametrize("name,good,bad", [
    ("pir_threshold", 0, 256), ("indicator_light", False, 0),
    ("radar_frame_rate", 1, 0), ("convergence_level", 50, 51),
    ("presence_response_time", 8, 9), ("connection_wake_time", 3, 4),
    ("function_mode", 2, 3), ("manual_off_activation_lux", 0, 1001),
    ("no_one_report_mask", 0xFFF, 0x1000),
])
def test_official_advanced_settings_enforce_wire_types_and_known_ranges(name, good, bad):
    CATALOG[name].validate(good)
    with pytest.raises(DirectError):
        CATALOG[name].validate(bad)


def test_published_plugin_type_conflicts_and_linked_power_settings_remain_readonly(tmp_path):
    c = controller(tmp_path)
    for name, value in (("manual_off_detection_seconds", 3), ("power_upload_type", 1), ("power_upload_fast_enable", 1)):
        accepted(c, name, value)
        with pytest.raises(DirectError, match="僅供讀取"):
            CATALOG[name].validate(value)
    accepted(c, "no_one_report_mask", 0x8001)
    assert c.snapshot()["reporting"]["no_one_report_mask"]["unknown_bits"] == 0x8000
    assert c.snapshot()["reporting"]["no_one_report_mask"]["enabled_minutes"] == [1]
    with pytest.raises(DirectError, match="保留位元"):
        CATALOG["no_one_report_mask"].validate(0x8001)


class StreamSession:
    def __init__(self, fail_pir=False, reject_all=False):
        self.calls = []
        self.fail_pir, self.reject_all = fail_pir, reject_all
        self.read_ops = {}
        self.error = None
    async def command(self, value):
        self.calls.append(("command", value))
        return not (self.reject_all or (value == 4 and self.fail_pir))
    async def subscribe(self, siid, piid):
        self.calls.append(("subscribe", siid, piid))
        return None  # Valid subscription with no initial PIR snapshot.


def test_streams_are_mutually_exclusive_and_switch_back_to_radar_without_false_freshness(tmp_path):
    c = controller(tmp_path)
    c.session = StreamSession()
    c._update(status="ready", authenticated=True)
    c.state["streaming"].update(mode="radar", requested_mode="radar", active=True)
    async def run():
        await c._select_stream("pir")
        assert c.session.calls == [("command", 0), ("subscribe", 6, 18), ("command", 4)]
        state = c.snapshot()["streaming"]
        assert state["mode"] == "pir" and not state["active"] and not state["pir_active"]
        accepted(c, "motion_energy", encode_series([30] * 32))
        assert not c.snapshot()["streaming"]["active"]  # A queued radar frame is not the selected stream.
        accepted(c, "pir_energy", -12)
        assert c.snapshot()["streaming"]["pir_active"]
        await c._select_stream("radar")
        assert c.session.calls[-2:] == [("command", 0), ("command", 1)]
        assert not c.snapshot()["streaming"]["pir_active"]
        accepted(c, "pir_energy", -8)
        assert not c.snapshot()["streaming"]["pir_active"]
        accepted(c, "motion_energy", encode_series([32] * 32))
        assert c.snapshot()["streaming"]["active"]
        await c._select_stream("off")
        assert c.session.calls[-1] == ("command", 0)
        assert c.snapshot()["streaming"]["mode"] == "off"
        assert not c.snapshot()["streaming"]["requested"]
    asyncio.run(run())


@pytest.mark.parametrize("reject_all", [False, True])
def test_failed_pir_switch_restores_radar_or_marks_unknown_without_claiming_active(tmp_path, reject_all):
    c = controller(tmp_path)
    c.session = StreamSession(fail_pir=True, reject_all=reject_all)
    c._update(status="ready", authenticated=True)
    c.state["streaming"].update(mode="radar", requested_mode="radar", active=True)
    async def run():
        with pytest.raises(DirectError):
            await c._select_stream("pir")
        state = c.snapshot()["streaming"]
        assert state["mode"] == ("unknown" if reject_all else "radar")
        assert not state["active"] and not state["pir_active"]
        if not reject_all:
            assert c.session.calls[-2:] == [("command", 0), ("command", 1)]
        else:
            assert not any(call == ("command", 4) for call in c.session.calls)
    asyncio.run(run())


def test_stream_control_requires_authentication_and_does_not_interrupt_calibration(tmp_path):
    c = controller(tmp_path)
    with pytest.raises(DirectError, match="認證"):
        c.submit({"action": "stream", "mode": "pir"})
    c._update(status="ready", authenticated=True)
    for invalid in (None, 4, "both", "reset"):
        with pytest.raises(DirectError, match="串流模式"):
            c.submit({"action": "stream", "mode": invalid})
    c._cal_state(phase="scanning")
    with pytest.raises(DirectError, match="校正正在進行"):
        c.submit({"action": "stream", "mode": "pir"})
    c._cal_state(phase="preview")
    with pytest.raises(DirectError, match="取消空房掃描預覽"):
        c.submit({"action": "stream", "mode": "pir"})
    c._cal_state(phase="idle")
    c.state["streaming"].update(mode="pir", requested_mode="pir")
    with pytest.raises(DirectError, match="恢復雷達串流"):
        c.submit({"action": "radar_calibrate", "empty_room": True})


def test_optional_refresh_is_bounded_rotates_and_never_reads_notify_only_pir(tmp_path):
    c = controller(tmp_path)
    c.OPTIONAL_REFRESH_SECONDS, c.OPTIONAL_PROPERTY_TIMEOUT = .1, 1
    c.session = SimpleNamespace(error=None)
    observed = []
    async def read(prop):
        observed.append(prop)
        if prop.optional:
            await asyncio.sleep(.05)
        return 0
    c._read = read
    async def run():
        await asyncio.wait_for(c._refresh(), .5)
        first_optional = [p.name for p in observed if p.optional]
        assert first_optional and c.snapshot()["status"] == "ready"
        observed.clear()
        await asyncio.wait_for(c._refresh(), .5)
        second_optional = [p.name for p in observed if p.optional]
        assert second_optional and second_optional[0] != first_optional[0]
        assert not any(p.notification_only for p in observed)
    asyncio.run(run())


def test_three_optional_failures_do_not_abort_a_working_authenticated_connection(tmp_path):
    c = controller(tmp_path)
    c.session = SimpleNamespace(error=None)
    failed = []
    async def read(prop):
        if prop.optional:
            failed.append(prop.name)
            raise DirectError("synthetic optional property unsupported")
        return 0
    c._read = read
    asyncio.run(c._refresh())
    assert len(failed) > 3 and c.snapshot()["status"] == "ready"
    assert all(c.snapshot()["properties"][name]["status"] == "error" for name in failed)


def test_pir_threshold_requires_explicit_custom_mode_and_fresh_check_before_write(tmp_path):
    c = controller(tmp_path)
    c._update(status="ready", authenticated=True)
    accepted(c, "pir_threshold", 12)
    accepted(c, "pir_sensitivity", 3)
    with pytest.raises(DirectError, match="自訂"):
        c.submit({"action": "set", "property": "pir_threshold", "value": 14})
    accepted(c, "pir_sensitivity", 0)
    c.submit({"action": "set", "property": "pir_threshold", "value": 14})
    writes = []
    class Session:
        async def read(self, siid, piid, **kwargs):
            return {"siid": siid, "piid": piid, "status": 0, "type_code": 0x10, "value": 3}
        async def set(self, *args):
            writes.append(args)
            pytest.fail("PIR sensitivity changed since the UI snapshot; threshold must not be written")
    c.session = Session()
    async def run():
        with pytest.raises(DirectError, match="尚未寫入"):
            await c._set("pir_threshold", 14)
    asyncio.run(run())
    assert writes == []


def test_unknown_report_bits_cannot_be_silently_cleared_by_setting_known_mask(tmp_path):
    c = controller(tmp_path)
    c._update(status="ready", authenticated=True)
    accepted(c, "no_one_report_mask", 0x8001)
    with pytest.raises(DirectError, match="保留原設定"):
        c.submit({"action": "set", "property": "no_one_report_mask", "value": 1})


@pytest.mark.parametrize("name", ["no_one_duration", "has_someone_duration"])
def test_duration_enumerations_preserve_unknown_raw_values_without_publishing_as_minutes(tmp_path, name):
    c = controller(tmp_path)
    records = []
    c.on_value = records.append
    accepted(c, name, 3)
    assert c.snapshot()["properties"][name]["status"] == "read" and records[-1]["value"] == 3
    count = len(records)
    accepted(c, name, 4)
    prop = c.snapshot()["properties"][name]
    assert prop["status"] == "invalid_value" and prop["value"] == 4
    assert "列舉" in prop["error"] and len(records) == count
    assert prop["allowed_values"] == [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120]
    accepted(c, name, 5)
    assert c.snapshot()["properties"][name]["status"] == "read" and records[-1]["value"] == 5
