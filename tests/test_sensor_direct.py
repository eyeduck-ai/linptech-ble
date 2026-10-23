"""Released-spec settings, session guards and controller transport boundaries."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from linptech_ble.config import DeviceConfig
from linptech_ble.client import ClientError, LinptechClient, PropertyValue
from linptech_ble.sensor_direct import PROFILES, SensorDirectController
from linptech_ble.standard_auth import DirectError
from linptech_ble.transport import BluetoothConfig, ProxyConfig
from linptech_ble.es5_worker import ProcessSensorController


KEY = "00112233445566778899aabbccddeeff"
TOKEN = "00112233445566778899aabb"
ADDRESS = "AA:BB:CC:DD:EE:01"


def controller(tmp_path, model="KS2BB", *, confirmed=False, transport=None):
    miot = "linp.motion.hs1bb2" if model == "HS3BB" and confirmed else None
    device = DeviceConfig(ADDRESS, model, "Synthetic sensor", KEY, TOKEN, miot_model=miot)
    result = SensorDirectController(device, recording_dir=tmp_path, transport=transport)
    result.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))
    result.commands = asyncio.Queue()
    return result


def observation(control, name, value, *, status="read", at="2026-10-23T00:00:01+00:00", error=None):
    definition = LinptechClient(control.device).properties[name]
    return PropertyValue(definition, value, status, at, "gatt", error)


def ready(controller, name, value):
    controller._update(authenticated=True, status="ready")
    controller._accept(observation(controller, name, value))


def test_profiles_only_expose_released_gatt_read_write_properties():
    root = Path(__file__).resolve().parents[1] / "linptech_ble" / "specs"
    for model, protocol in (("MS1BB", "linp.magnet.m1"), ("KS2BB", "linp.sensor_ht.ks2bb"),
                            ("HS3BB", "linp.motion.hs1bb2")):
        spec = json.loads((root / (protocol + ".json")).read_text(encoding="utf-8"))
        raw = {(s["iid"], p["iid"]): p for s in spec["services"] for p in s.get("properties", [])}
        for prop in PROFILES[model]:
            source = raw[(prop.siid, prop.piid)]
            assert {"read", "write"} <= set(source.get("gatt-access", []))
            assert prop.format == source["format"]
            if "value-range" in source:
                assert (prop.minimum, prop.maximum, prop.step) == tuple(source["value-range"])
            assert prop.notify == ("notify" in source.get("gatt-access", []))
    assert PROFILES["MS1BB"] == ()


@pytest.mark.parametrize("device", [None, SimpleNamespace(model="ES5BB"), SimpleNamespace(model="unknown")])
def test_other_models_cannot_enter_generic_controller(device):
    with pytest.raises(DirectError, match="僅支援"):
        SensorDirectController(device)


@pytest.mark.parametrize("model,name,value", [
    ("KS2BB", "button_mode", -1), ("KS2BB", "button_mode", 2),
    ("KS2BB", "button_mode", True), ("KS2BB", "temperature_unit", "1"),
    ("HS3BB", "detection_interval", 15), ("HS3BB", "detection_interval", 60),
    ("HS3BB", "no_motion_time", 0), ("HS3BB", "no_motion_time", 61),
    ("HS3BB", "indicator_light", 1), ("HS3BB", "no_motion_timeout_1", 59),
    ("HS3BB", "no_motion_timeout_2", 3601), ("HS3BB", "pir_single_report", "false"),
])
def test_model_setting_range_steps_and_wire_types_reject_invalid_values(tmp_path, model, name, value):
    control = controller(tmp_path, model, confirmed=True)
    with pytest.raises(DirectError):
        control.catalog[name].validate(value)


def test_current_session_read_is_required_and_cached_values_are_retained(tmp_path):
    control = controller(tmp_path)
    command = {"action": "set", "property": "button_mode", "value": 1}
    with pytest.raises(DirectError, match="先連線"):
        control.submit(command)
    control._update(authenticated=True, status="ready")
    with pytest.raises(DirectError, match="先成功讀取"):
        control.submit(command)
    ready(control, "button_mode", 0)
    asyncio.run(control._disconnect())
    assert control.snapshot()["properties"]["button_mode"]["status"] == "cached"
    assert control.snapshot()["properties"]["button_mode"]["value"] == 0
    control._update(authenticated=True, status="ready")
    with pytest.raises(DirectError, match="先成功讀取"):
        control.submit(command)
    ready(control, "button_mode", 0)
    operation = control.submit(command)
    assert control.commands.get_nowait() == {**command, "id": operation}
    public = json.dumps(control.snapshot())
    assert KEY not in public and TOKEN not in public


def test_hs3_model_identity_gate_prevents_writes_until_locally_confirmed(tmp_path):
    unknown = controller(tmp_path, "HS3BB")
    ready(unknown, "detection_interval", 10)
    assert unknown.snapshot()["model_confirmed"] is False
    with pytest.raises(DirectError, match="尚未確認 HS3BB"):
        unknown.submit({"action": "set", "property": "detection_interval", "value": 20})
    confirmed = controller(tmp_path, "HS3BB", confirmed=True)
    ready(confirmed, "detection_interval", 10)
    assert confirmed.submit({"action": "set", "property": "detection_interval", "value": 20})


def test_ms1_has_no_settings_and_never_uses_debug_v2_or_es5_commands(tmp_path):
    control = controller(tmp_path, "MS1BB")
    assert not control.snapshot()["supported"] and control.snapshot()["properties"] == {}
    with pytest.raises(DirectError, match="尚未提供可用設定"):
        control.submit({"action": "connect"})
    control._update(authenticated=True, status="ready")
    for command in ({"action": "set", "property": "not_closed_duration", "value": 30},
                    {"action": "stream", "mode": "pir"}, {"action": "calibrate"}):
        with pytest.raises(DirectError):
            control.submit(command)


def test_unknown_and_out_of_spec_readbacks_never_enable_writes(tmp_path):
    control = controller(tmp_path, "HS3BB", confirmed=True)
    prop = control.catalog["no_motion_time"]
    control._update(authenticated=True, status="ready")
    invalid = observation(control, prop.name, 0, status="invalid_value", error="Outside the released range.")
    control._accept(invalid)
    row = control.snapshot()["properties"][prop.name]
    assert row["status"] == "invalid_value" and row["value"] == 0
    with pytest.raises(DirectError, match="先成功讀取"):
        control.submit({"action": "set", "property": prop.name, "value": 10})
    # The SDK owns wire types/ranges. Its typed invalid/error states remain
    # visible without fabricating a replacement value or enabling writes.
    wrong_type = replace(invalid, value=True, error="Wire type differs from the definition.")
    control._accept(wrong_type)
    assert control.snapshot()["properties"][prop.name]["status"] == "invalid_value"
    unavailable = replace(wrong_type, status="unavailable", error="No current value.")
    control._accept(unavailable)
    assert control.snapshot()["properties"][prop.name]["status"] == "unavailable"
    with pytest.raises(TypeError, match="PropertyValue"):
        control._accept({"siid": prop.siid, "piid": prop.piid, "value": 10})


def test_sdk_readback_failure_is_not_confirmed_by_the_controller(tmp_path):
    control = controller(tmp_path)
    ready(control, "button_mode", 0)
    class Client:
        connected = True
        agrees = False
        calls = []

        async def set(self, name, value):
            self.calls.append((name, value))
            if not self.agrees:
                raise ClientError("Write did not produce an agreeing valid readback.", code="verification_failed")
            return observation(control, name, value, at="2026-10-23T00:00:02+00:00")

        def diagnostics(self):
            return {"model": "KS2BB", "connected": True}

    async def run():
        client = control.client = Client()
        with pytest.raises(ClientError) as error:
            await control._set("button_mode", 1)
        assert error.value.code == "verification_failed"
        assert control.snapshot()["properties"]["button_mode"]["value"] == 0
        assert not any(d["kind"] == "setting_verified" for d in control.snapshot()["diagnostics"])
        client.agrees = True
        await control._set("button_mode", 1)
        assert control.snapshot()["properties"]["button_mode"]["value"] == 1
        assert control.snapshot()["diagnostics"][0]["kind"] == "setting_verified"
        assert client.calls == [("button_mode", 1), ("button_mode", 1)]
    asyncio.run(run())


def test_connect_refresh_disconnect_only_touches_ks2_profile(monkeypatch, tmp_path):
    import linptech_ble.sensor_direct as module
    control = controller(tmp_path)
    calls, clients, forwarded = [], [], []
    control.on_value = forwarded.append

    class Client:
        connected = False

        def __init__(self, device, *, transport, on_update):
            assert device is control.device and transport is control.transport
            self.on_update = on_update
            self.values = {}
            clients.append(self)

        async def connect(self):
            calls.append("connect")
            self.connected = True

        async def refresh(self, names):
            calls.append(("refresh", names))
            for name in names:
                self.values[name] = observation(control, name, 0)
                self.on_update(self.values[name])
            return dict(self.values)

        def get_property(self, name):
            return self.values[name]

        def diagnostics(self):
            return {"model": "KS2BB", "connected": self.connected}

        async def aclose(self):
            calls.append("close")
            self.connected = False
            # This late callback must be ignored before the SDK finishes close.
            late = replace(self.values["button_mode"], value=1, updated_at="2026-10-23T00:00:03+00:00")
            self.values["button_mode"] = late
            self.on_update(late)

    monkeypatch.setattr(module, "LinptechClient", Client)

    async def run():
        try:
            await control._connect()
            assert control.snapshot()["authenticated"] and control.snapshot()["status"] == "ready"
            assert control.snapshot()["firmware"] is None
            assert len(forwarded) == 2  # Return values + callbacks are not logged twice.
            # A queued old update cannot roll back a newer cache observation.
            client = clients[0]
            old = client.values["button_mode"]
            newest = replace(old, value=1, updated_at="2026-10-23T00:00:02+00:00")
            client.values["button_mode"] = newest
            client.on_update(newest)
            client.on_update(old)
            assert control.snapshot()["properties"]["button_mode"]["value"] == 1
            assert len(forwarded) == 3
            # Value equality is insufficient: equal-clock messages are still
            # distinct observations, while an object not in cache is stale.
            client.on_update(replace(newest))
            assert len(forwarded) == 3
            repeated = replace(newest)
            client.values["button_mode"] = repeated
            client.on_update(repeated)
            assert len(forwarded) == 4
            await control._idle()
        finally:
            await control._disconnect()
    asyncio.run(run())
    assert calls == ["connect", ("refresh", ("button_mode", "temperature_unit")), "close"]
    assert control.client is None and control.log is None
    assert control.snapshot()["properties"]["button_mode"]["status"] == "cached"
    assert control.snapshot()["properties"]["button_mode"]["value"] == 1
    assert len(forwarded) == 4
    assert not hasattr(control, "session") and not hasattr(control, "client_context")
    log = Path(control.snapshot()["recording"]).read_text(encoding="utf-8")
    assert KEY not in log and TOKEN not in log


@pytest.mark.parametrize("failure", ["constructor", "connect", "cancel"])
def test_public_client_open_failures_close_logs_and_keep_old_values_cached(monkeypatch, tmp_path, failure):
    import linptech_ble.sensor_direct as module
    control = controller(tmp_path)
    ready(control, "temperature_unit", 0)
    closed = []

    class Client:
        connected = False

        def __init__(self, *args, **kwargs):
            if failure == "constructor":
                raise ClientError("Synthetic construction failure.")

        async def connect(self):
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise ClientError("Synthetic authentication failure.")

        async def aclose(self):
            closed.append(True)

        def diagnostics(self):
            return {"connected": False}

    monkeypatch.setattr(module, "LinptechClient", Client)

    async def run():
        error_type = asyncio.CancelledError if failure == "cancel" else ClientError
        with pytest.raises(error_type):
            await control._connect()
        assert control.client is None and control.log is None
        state = control.snapshot()
        assert not state["authenticated"] and state["status"] == "disconnected"
        assert state["properties"]["temperature_unit"]["status"] == "cached"
        assert state["properties"]["temperature_unit"]["value"] == 0

    asyncio.run(run())
    assert len(closed) == (0 if failure == "constructor" else 1)
    for path in tmp_path.glob("linptech-recording-v1-*.events.*.jsonl"):
        with path.open("a", encoding="utf-8") as reopened:
            reopened.write("")


@pytest.mark.parametrize("backend", ["local", "esphome"])
def test_process_mro_keeps_sensor_catalog_and_selects_correct_execution_boundary(tmp_path, backend):
    transport = BluetoothConfig() if backend == "local" else BluetoothConfig(
        "esphome", (ProxyConfig("room", "192.0.2.1"),))
    control = ProcessSensorController(DeviceConfig(ADDRESS, "KS2BB", "Synthetic", KEY, TOKEN),
                                      recording_dir=tmp_path, transport=transport)
    control.start()
    try:
        deadline = time.monotonic() + 8
        while control.loop is None and time.monotonic() < deadline:
            time.sleep(.05)
        state = control.snapshot()
        assert set(state["properties"]) == {"button_mode", "temperature_unit"}
        # The lazy process boundary delegates validation through the model
        # controller; ES5 calibration must never enter a KS2BB worker queue.
        with pytest.raises(DirectError, match="此型號不支援"):
            control.submit({"action": "calibrate", "empty_room": True})
        assert control.pending_command is None
        assert control._connect.__func__ is SensorDirectController._connect
        if backend == "local":
            assert control.worker is not None and control.worker.is_alive()
        else:
            assert control.worker is None and control.thread.is_alive()
        # No connect command is submitted: neither path may touch BLE/network.
    finally:
        control.stop()
    if control.worker:
        assert not control.worker.is_alive()
    assert not control.thread.is_alive()
