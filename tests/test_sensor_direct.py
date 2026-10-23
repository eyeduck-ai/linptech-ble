"""Released-spec settings, session guards and controller transport boundaries."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from linptech_ble.config import DeviceConfig
from linptech_ble.sensor_direct import PROFILES, SensorDirectController
from linptech_ble.standard_auth import DirectError, StandardSession, encode_value, uuid16
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


def response(prop, value, **changes):
    result = {"siid": prop.siid, "piid": prop.piid, "status": 0, "available": True,
              "type_code": encode_value(value, prop.format)[0], "value": value, "tid": 1}
    result.update(changes)
    return result


def ready(controller, name, value):
    controller._update(authenticated=True, status="ready")
    controller._accept(response(controller.catalog[name], value))


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
    control._accept(response(prop, 0))
    row = control.snapshot()["properties"][prop.name]
    assert row["status"] == "invalid_value" and row["value"] == 0
    with pytest.raises(DirectError, match="先成功讀取"):
        control.submit({"action": "set", "property": prop.name, "value": 10})
    with pytest.raises(DirectError, match="型別"):
        control._accept(response(prop, 10, type_code=0x50))
    control._accept(response(prop, 10, available=False))
    assert control.snapshot()["properties"][prop.name]["status"] == "invalid_value"


def test_real_standard_session_readback_must_agree_before_setting_is_confirmed(tmp_path):
    control = controller(tmp_path)
    ready(control, "button_mode", 0)
    prop = control.catalog["button_mode"]

    class Session(StandardSession):
        def __init__(self):
            self.lock, self.sent, self.observed = asyncio.Lock(), [], 0

        async def _send(self, op, siid, piid, **kwargs):
            self.sent.append((op, siid, piid, kwargs))

        async def read(self, siid, piid, **kwargs):
            return response(prop, self.observed)

    async def run():
        control.session = Session()
        with pytest.raises(DirectError, match="回讀不一致"):
            await control._set("button_mode", 1)
        assert control.snapshot()["properties"]["button_mode"]["value"] == 0
        assert not any(d["kind"] == "setting_verified" for d in control.snapshot()["diagnostics"])
        control.session.observed = 1
        await control._set("button_mode", 1)
        assert control.snapshot()["properties"]["button_mode"]["value"] == 1
        assert control.snapshot()["diagnostics"][0]["kind"] == "setting_verified"
    asyncio.run(run())


def test_connect_refresh_disconnect_only_touches_ks2_profile(monkeypatch, tmp_path):
    import linptech_ble.sensor_direct as module
    control = controller(tmp_path)
    calls, sessions = [], []
    client = SimpleNamespace(services=SimpleNamespace(get_characteristic=lambda uuid: uuid == uuid16(4)))

    async def read_firmware(uuid):
        assert uuid == uuid16(4)
        return b"synthetic_0001"
    client.read_gatt_char = read_firmware

    @asynccontextmanager
    async def connected(*args):
        calls.append("connect")
        try:
            yield client
        finally:
            calls.append("disconnect")

    class Session:
        error = None

        def __init__(self, received_client, token, **kwargs):
            assert received_client is client and token == bytes.fromhex(TOKEN)
            sessions.append(self)

        async def authenticate(self):
            calls.append("authenticate")

        async def read(self, siid, piid, **kwargs):
            calls.append(("read", siid, piid, kwargs.get("subscribe")))
            prop = control.pairs[(siid, piid)]
            return response(prop, 0)

        async def close(self):
            calls.append("session.close")

        async def command(self, value):
            pytest.fail("KS2BB must never receive an ES5 vendor stream command")

    monkeypatch.setattr(module, "connected_client", connected)
    monkeypatch.setattr(module, "StandardSession", Session)

    async def run():
        try:
            await control._connect()
            assert control.snapshot()["authenticated"] and control.snapshot()["status"] == "ready"
            await control._idle()
        finally:
            await control._disconnect()
    asyncio.run(run())
    assert calls == ["connect", "authenticate", ("read", 6, 1, True), ("read", 6, 2, False),
                     "session.close", "disconnect"]
    assert control.client is None and control.session is None and control.log is None


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
        assert control.submit.__func__ is SensorDirectController.submit
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
