"""Loopback generic settings API with synthetic, unstarted controllers only."""
import asyncio
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import threading
from types import SimpleNamespace

import pytest

from linptech_ble.config import DeviceConfig
from linptech_ble.dashboard import DashboardServer, DashboardState
from linptech_ble.sensor_direct import SensorDirectController
from linptech_ble.standard_auth import encode_value


KEY = "00112233445566778899aabbccddeeff"
TOKEN = "00112233445566778899aabb"


def sensor(model, suffix):
    return DeviceConfig(f"AA:BB:CC:DD:EE:{suffix:02X}", model, f"Synthetic {model}", KEY, TOKEN)


@pytest.fixture
def state(tmp_path):
    devices = [sensor("MS1BB", 1), sensor("KS2BB", 2), sensor("HS3BB", 3)]
    result = DashboardState(devices)
    for device in devices:
        control = SensorDirectController(device, recording_dir=tmp_path)
        control.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))
        control.commands = asyncio.Queue()
        result.sensor_direct[device.model] = control
    return result


def read(control, name, value):
    prop = control.catalog[name]
    control._update(authenticated=True, status="ready")
    control._accept({"siid": prop.siid, "piid": prop.piid, "status": 0, "available": True,
                     "type_code": encode_value(value, prop.format)[0], "value": value})


@contextmanager
def server(state):
    service = DashboardServer(state, port=0)
    thread = threading.Thread(target=service.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield service.server_address[1]
    finally:
        service.closing.set()
        service.shutdown()
        service.server_close()
        thread.join(timeout=3)


def post(port, payload, *, headers=None, path="/api/sensor/direct"):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", path, json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "X-Linptech-Control": "1", **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read()) if response.status in (202, 409) else response.read()
    finally:
        connection.close()


def test_valid_ks2_setting_routes_only_to_selected_controller_and_preserves_busy_guard(state):
    control = state.sensor_direct["KS2BB"]
    read(control, "button_mode", 0)
    command = {"model": "KS2BB", "action": "set", "property": "button_mode", "value": 1}
    with server(state) as port:
        status, answer = post(port, command)
        assert status == 202
        assert control.commands.get_nowait() == {
            "action": "set", "property": "button_mode", "value": 1, "id": answer["id"]}
        assert state.sensor_direct["HS3BB"].commands.empty()
        assert state.sensor_direct["MS1BB"].commands.empty()
        assert post(port, {"model": "KS2BB", "action": "refresh"})[0] == 409


def test_settings_require_auth_current_read_and_hs3_identity_even_through_http(state):
    ks2, hs3 = state.sensor_direct["KS2BB"], state.sensor_direct["HS3BB"]
    ks_command = {"model": "KS2BB", "action": "set", "property": "temperature_unit", "value": 1}
    with server(state) as port:
        assert post(port, ks_command)[0] == 409
        ks2._update(authenticated=True, status="ready")
        assert post(port, ks_command)[0] == 409
        read(hs3, "detection_interval", 10)
        status, answer = post(port, {"model": "HS3BB", "action": "set",
                                     "property": "detection_interval", "value": 20})
        assert status == 409 and "尚未確認 HS3BB" in answer["error"]
        assert post(port, {"model": "MS1BB", "action": "connect"})[0] == 409
    assert all(c.commands.empty() for c in state.sensor_direct.values())


@pytest.mark.parametrize("payload", [
    {"model": "ES5BB", "action": "connect"},
    {"model": "KS2BB", "action": "stream", "mode": "pir"},
    {"model": "HS3BB", "action": "calibrate", "empty_room": True},
    {"model": "KS2BB", "action": "connect", "auth_token": TOKEN},
    {"model": "KS2BB", "action": "connect", "miot_model": "linp.sensor_ht.ks2bb"},
    {"model": "KS2BB", "action": "set", "siid": 6, "piid": 1, "value": 1},
    {"model": "KS2BB", "action": "set", "property": ["button_mode"], "value": 1},
    {"model": {"name": "KS2BB"}, "action": "connect"},
])
def test_generic_route_rejects_model_spoofing_raw_addresses_and_vendor_operations(state, payload):
    with server(state) as port:
        assert post(port, payload)[0] == 400
    assert all(c.commands.empty() for c in state.sensor_direct.values())


@pytest.mark.parametrize("name,value", [("detection_interval", 20), ("cmd_to_device", 4),
                                        ("button_mode", 2), ("temperature_unit", True)])
def test_ks2_cannot_write_other_model_properties_or_invalid_enumerations(state, name, value):
    read(state.sensor_direct["KS2BB"], "button_mode", 0)
    with server(state) as port:
        assert post(port, {"model": "KS2BB", "action": "set", "property": name, "value": value})[0] == 409
    assert state.sensor_direct["KS2BB"].commands.empty()


@pytest.mark.parametrize("headers", [{"Origin": "https://untrusted.example"},
                                     {"Host": "untrusted.example"},
                                     {"Content-Type": "text/plain"},
                                     {"X-Linptech-Control": ""}])
def test_generic_control_requires_same_origin_and_explicit_json_header(state, headers):
    with server(state) as port:
        assert post(port, {"model": "KS2BB", "action": "connect"}, headers=headers)[0] == 403
    assert state.sensor_direct["KS2BB"].commands.empty()


def test_generic_snapshot_exports_settings_without_credentials_and_replay_has_no_controls(state):
    read(state.sensor_direct["KS2BB"], "temperature_unit", 0)
    with server(state) as port:
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/api/export")
            response = connection.getresponse()
            data = response.read()
            assert response.status == 200
            assert KEY.encode() not in data and TOKEN.encode() not in data
            exported = json.loads(data)
            assert exported["sensor_direct"]["KS2BB"]["properties"]["temperature_unit"]["value"] == 0
            assert exported["sensor_direct"]["MS1BB"]["supported"] is False
        finally:
            connection.close()
    replay = DashboardState([], mode="replay")
    with server(replay) as port:
        assert post(port, {"model": "KS2BB", "action": "connect"})[0] == 409
