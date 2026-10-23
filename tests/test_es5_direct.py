"""Dashboard orchestration consumes public SDK observations and operations.

Wire/security behavior is covered by the encrypted-peer integration tests;
these cases exercise UI guards, backup ordering, cache and stream freshness.
"""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from linptech_ble.client import LinptechClient, PropertyValue, ClientError
from linptech_ble.config import DeviceConfig, load_config, save_config, validate_auth_token
from linptech_ble.es5_direct import CATALOG, DirectController, numeric_array
from linptech_ble.standard_auth import DirectError
from linptech_ble.es5_radar import EmptyRoomScan, encode_series

TOKEN = "00112233445566778899aabb"
KEY = "00112233445566778899aabbccddeeff"
CONFIG = DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Test", KEY, TOKEN)
DEFINITIONS = LinptechClient(CONFIG).properties


def observed(name, value, status="read", error=None, operation=3):
    return PropertyValue(DEFINITIONS[name], value, status,
        datetime.now(timezone.utc).isoformat(), "gatt", error, operation)


class SDK:
    """Only the public client surface, with persisted values for UI workflows."""
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.connected, self.stream_mode = True, "off"
        self.calls, self.live = [], {}
    def diagnostics(self):
        return {"valid_op4_updates": dict(self.live), "connected": self.connected}
    def record_live(self, value):
        self.live[value.name] = {"count": 1, "last_at": value.updated_at}
    async def read(self, name):
        self.calls.append(("read", name))
        return observed(name, self.values[name])
    async def set(self, name, value):
        self.calls.append(("set", name, value))
        self.values[name] = value
        return observed(name, value)
    async def set_stream(self, mode):
        self.calls.append(("set_stream", mode))
        self.stream_mode = mode
    async def commit_radar_thresholds(self, *, empty_room):
        assert empty_room is True
        self.calls.append(("commit_radar_thresholds",))
        return True
    async def radar_calibrate(self, *, empty_room):
        assert empty_room is True
        self.calls.append(("radar_calibrate",))
        return True
    async def aclose(self):
        self.connected = False


def controller(tmp_path):
    control = DirectController(CONFIG, recording_dir=tmp_path)
    control.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args), time=lambda: 0)
    control.commands = asyncio.Queue()
    control.client = SDK()
    return control


def accepted(control, name, value, *, status="read", error=None, live=True):
    observation = observed(name, value, status, error, operation=4 if live else 3)
    if live and observation.valid and control.client:
        control.client.record_live(observation)
    control._accept(observation)
    return observation


def test_token_config_and_repr_are_distinct_from_bindkey(tmp_path):
    assert TOKEN not in repr(CONFIG) and KEY not in repr(CONFIG)
    save_config(tmp_path / "secrets.json", [CONFIG])
    assert load_config(tmp_path / "secrets.json")[0].auth_token == TOKEN
    with pytest.raises(ValueError) as error:
        validate_auth_token(KEY)
    assert KEY not in str(error.value)


def test_controls_require_authenticated_current_read_and_never_expose_secrets(tmp_path):
    c = controller(tmp_path)
    command = {"action": "set", "property": "sensitivity_level", "value": 0}
    with pytest.raises(DirectError):
        c.submit(command)
    c._update(authenticated=True, status="ready")
    with pytest.raises(DirectError, match="先成功讀取"):
        c.submit(command)
    accepted(c, "sensitivity_level", 1)
    identity = c.submit(command)
    assert c.commands.get_nowait() == {**command, "id": identity}
    with pytest.raises(DirectError, match="正在進行"):
        c.submit({"action": "refresh"})
    public = json.dumps(c.snapshot())
    assert TOKEN not in public and KEY not in public
    assert not c.snapshot()["calibration"]["available"]


def test_disconnect_invalidates_read_permission_without_deleting_values(tmp_path):
    c = controller(tmp_path)
    c._update(authenticated=True, status="ready")
    accepted(c, "sensitivity_level", 2)
    asyncio.run(c._disconnect())
    row = c.snapshot()["properties"]["sensitivity_level"]
    assert row["value"] == 2 and row["status"] == "cached"
    c._update(authenticated=True, status="ready")
    with pytest.raises(DirectError, match="先成功讀取"):
        c.submit({"action": "set", "property": "sensitivity_level", "value": 1})


@pytest.mark.parametrize("name,value", [("sensitivity_level", 4), ("sensitivity_level", True),
    ("no_one_determine_time", 10001), ("no_one_determine_time", -1),
    ("detect_region", 2**32), ("motion_energy", "1,2,3")])
def test_read_only_properties_and_invalid_settings_cannot_be_submitted(name, value):
    with pytest.raises(DirectError):
        CATALOG[name].validate(value)


def test_invalid_sdk_observation_is_visible_but_never_enables_writes_or_publication(tmp_path):
    c, records = controller(tmp_path), []
    c.on_value = records.append
    c._update(authenticated=True, status="ready")
    accepted(c, "sensitivity_level", 9, status="invalid_value", error="Invalid firmware enum")
    assert c.snapshot()["properties"]["sensitivity_level"]["value"] == 9 and not records
    with pytest.raises(DirectError, match="先成功讀取"):
        c.submit({"action": "set", "property": "sensitivity_level", "value": 1})
    with pytest.raises(TypeError, match="PropertyValue"):
        c._accept({"value": 1})


@pytest.mark.parametrize("value", ["AQID", "1,2,3", "[true,2]", "[]", "[NaN,2]", '{"bins":[1,2]}', [float("inf")]])
def test_energy_decoder_does_not_guess_unknown_encodings(value):
    assert numeric_array(value) is None


def test_false_zero_values_and_callback_deduplication(tmp_path):
    c, records = controller(tmp_path), []
    c.on_value = records.append
    assert numeric_array("[0, 2.5, 9]") == [0.0, 2.5, 9.0]
    value = accepted(c, "interest_enable", False)
    c._accept(value)
    accepted(c, "battery_level", 0)
    assert c.snapshot()["properties"]["interest_enable"]["value"] is False
    assert c.snapshot()["properties"]["battery_level"]["value"] == 0
    assert len(records) == 2


def test_calibration_requires_preparation_fresh_data_and_radar_stream(tmp_path):
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
def test_calibration_uses_public_operations_and_backup_precedes_first_write(tmp_path, fail):
    c = controller(tmp_path)
    originals = {"motion_threshold": encode_series([30] * 32), "presence_threshold": encode_series([40] * 32),
                 "radar_motion_sens": 1, "radar_presence_sens": 2}
    proposed = {"motion_threshold": encode_series([50] * 32), "presence_threshold": encode_series([60] * 32)}
    backups = []
    class CalibrationSDK(SDK):
        failed = False
        async def set(self, name, value):
            assert backups == [originals]
            assert c.snapshot()["calibration"]["restore_required"]
            if fail and name == "presence_threshold" and not self.failed:
                self.failed = True
                raise DirectError("synthetic failed write")
            return await super().set(name, value)
    c.client = CalibrationSDK(originals)
    c.on_backup = backups.append
    c.scan = EmptyRoomScan(originals, 0)
    c._cal_state(phase="preview", proposed=proposed)
    async def run():
        if fail:
            with pytest.raises(DirectError, match="synthetic"):
                await c._calibration_action("calibration_apply")
        else:
            await c._calibration_action("calibration_apply")
            assert c.snapshot()["calibration"]["phase"] == "applied"
            assert ("commit_radar_thresholds",) in c.client.calls
            await c._calibration_action("calibration_restore")
        assert c.client.values == originals
        assert not c.snapshot()["calibration"]["restore_required"]
        assert c.client.calls[-4:] == [("read", name) for name in originals]
    asyncio.run(run())


def test_stream_ack_and_snapshots_do_not_claim_live_samples(tmp_path):
    c = controller(tmp_path)
    async def run():
        await c._select_stream("pir")
        assert c.client.calls == [("set_stream", "pir")]
        accepted(c, "pir_energy", 0, live=False)
        assert not c.snapshot()["pir"]["active"] and not c.snapshot()["pir"]["samples"]
        for value in (-1000, -1, 0, 1000):
            accepted(c, "pir_energy", value)
        assert c.snapshot()["pir"]["active"] and c.snapshot()["pir"]["value"] == 1000
        for _ in range(260):
            accepted(c, "pir_energy", -5)
        assert len(c.snapshot()["pir"]["samples"]) == 256
        await c._select_stream("radar")
        accepted(c, "pir_energy", -8)
        assert not c.snapshot()["pir"]["active"]
        accepted(c, "motion_energy", encode_series([32] * 32))
        assert c.snapshot()["streaming"]["active"]
        await c._select_stream("off")
        assert not c.snapshot()["streaming"]["requested"]
    asyncio.run(run())


@pytest.mark.parametrize("restored", [False, True])
def test_failed_stream_change_reflects_sdk_rollback_without_reimplementing_it(tmp_path, restored):
    c = controller(tmp_path)
    class FailedSDK(SDK):
        async def set_stream(self, mode):
            self.calls.append(("set_stream", mode))
            self.stream_mode = "radar" if restored else "unknown"
            raise DirectError("synthetic switch failure")
    c.client = FailedSDK()
    async def run():
        with pytest.raises(DirectError):
            await c._select_stream("pir")
        assert c.client.calls == [("set_stream", "pir")]
        state = c.snapshot()["streaming"]
        assert state["mode"] == ("radar" if restored else "unknown")
        assert not state["active"] and not state["pir_active"]
    asyncio.run(run())


def test_stream_control_guards(tmp_path):
    c = controller(tmp_path)
    with pytest.raises(DirectError, match="認證"):
        c.submit({"action": "stream", "mode": "pir"})
    c._update(status="ready", authenticated=True)
    for invalid in (None, 4, "both", "reset"):
        with pytest.raises(DirectError, match="串流模式"):
            c.submit({"action": "stream", "mode": invalid})
    c._cal_state(phase="preview")
    with pytest.raises(DirectError, match="取消空房掃描預覽"):
        c.submit({"action": "stream", "mode": "pir"})
    c._cal_state(phase="idle")
    c.state["streaming"].update(mode="pir", requested_mode="pir")
    with pytest.raises(DirectError, match="恢復雷達串流"):
        c.submit({"action": "radar_calibrate", "empty_room": True})


def test_optional_refresh_budget_rotates_without_cancelling_wire_transactions(tmp_path):
    c, calls = controller(tmp_path), []
    c.OPTIONAL_REFRESH_SECONDS = .06
    async def read(prop):
        calls.append(prop)
        if prop.optional:
            await asyncio.sleep(.04)
        return 0
    c._read = read
    async def run():
        await asyncio.wait_for(c._refresh(), .5)
        first = [p.name for p in calls if p.optional]
        calls.clear()
        await asyncio.wait_for(c._refresh(), .5)
        second = [p.name for p in calls if p.optional]
        assert first and second and first[0] != second[0]
        assert not any(p.notification_only for p in calls)
    asyncio.run(run())


def test_optional_failures_do_not_abort_working_client(tmp_path):
    c, failed = controller(tmp_path), []
    async def read(prop):
        if prop.optional:
            failed.append(prop.name)
            raise DirectError("optional unsupported")
        return 0
    c._read = read
    asyncio.run(c._refresh())
    assert len(failed) > 3 and c.snapshot()["status"] == "ready"


def test_set_delegates_fresh_guard_to_public_client(tmp_path):
    c = controller(tmp_path)
    class GuardSDK(SDK):
        async def set(self, name, value):
            self.calls.append(("set", name, value))
            raise ClientError("Fresh PIR sensitivity is no longer custom", code="pir_not_custom")
    c.client = GuardSDK()
    async def run():
        with pytest.raises(ClientError) as error:
            await c._set("pir_threshold", 14)
        assert error.value.code == "pir_not_custom"
        assert c.client.calls == [("set", "pir_threshold", 14)]
        assert not any(row["kind"] == "setting_verified" for row in c.state["diagnostics"])
    asyncio.run(run())


def test_unknown_report_bits_and_invalid_duration_remain_diagnostic(tmp_path):
    c, records = controller(tmp_path), []
    c.on_value = records.append
    c._update(authenticated=True, status="ready")
    accepted(c, "no_one_report_mask", 0x8001)
    assert c.snapshot()["reporting"]["no_one_report_mask"]["unknown_bits"] == 0x8000
    with pytest.raises(DirectError, match="保留原設定"):
        c.submit({"action": "set", "property": "no_one_report_mask", "value": 1})
    accepted(c, "no_one_duration", 4, status="invalid_value", error="Unknown duration enum")
    assert c.snapshot()["properties"]["no_one_duration"]["value"] == 4
    assert len(records) == 1
