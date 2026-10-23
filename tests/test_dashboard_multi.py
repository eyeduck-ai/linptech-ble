"""Multiple same-model controllers route by configured address, without a radio."""
import asyncio
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import threading
from types import SimpleNamespace

import pytest

from linptech_ble import dashboard
from linptech_ble.bridge_config import BridgeConfig
from linptech_ble.config import DeviceConfig
from linptech_ble.dashboard import DashboardServer, DashboardState
from linptech_ble.es5_worker import ProcessDirectController, ProcessSensorController
from linptech_ble.standard_auth import DirectError


TOKEN = "00112233445566778899aabb"


def config(number, model="ES5BB", *, token=TOKEN):
    return DeviceConfig(f"AA:BB:CC:DD:EE:{number:02X}", model, f"Synthetic {number}", auth_token=token)


class Controller:
    def __init__(self, device, *, on_value=None, transport=None):
        self.device, self.on_value, self.transport = device, on_value, transport
        self.commands = []
        self.catalog = {} if device.model == "MS1BB" else {"synthetic": True}

    def submit(self, command):
        self.commands.append(command)
        return "synthetic-operation"

    def snapshot(self):
        return {"status": "disconnected", "token_configured": True}


@contextmanager
def server(state):
    instance = DashboardServer(state, port=0)
    thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield instance.server_address[1]
    finally:
        instance.closing.set()
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=3)


def post(port, payload, path="/api/es5/direct"):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", path, json.dumps(payload),
            headers={"Content-Type": "application/json", "X-Linptech-Control": "1"})
        response = connection.getresponse()
        return response.status, json.loads(response.read()) if response.status != 400 else response.read()
    finally:
        connection.close()


def test_targeted_same_model_requests_and_legacy_first_instance_routes():
    devices = [config(1), config(2), config(3, "KS2BB"), config(4, "KS2BB")]
    state = DashboardState(devices)
    state.direct_devices = {item.address: Controller(item) for item in devices}
    state.direct = state.direct_devices[devices[0].address]
    state.sensor_direct = {"KS2BB": state.direct_devices[devices[2].address]}
    with server(state) as port:
        status, result = post(port, {"action": "connect", "address": devices[1].address.lower().replace(":", "-")})
        assert status == 202 and result["id"] == "synthetic-operation"
        assert state.direct_devices[devices[1].address].commands == [{"action": "connect"}]
        assert state.direct.commands == []
        assert post(port, {"action": "connect"})[0] == 202
        assert state.direct.commands == [{"action": "connect"}]
        assert post(port, {"action": "connect", "model": "KS2BB", "address": devices[3].address}, "/api/sensor/direct")[0] == 202
        assert state.direct_devices[devices[3].address].commands == [{"action": "connect"}]
        assert state.sensor_direct["KS2BB"].commands == []
    public = state.snapshot()
    assert set(public["direct_devices"]) == {item.address for item in devices}
    assert TOKEN not in json.dumps(public)


@pytest.mark.parametrize("address,status", [("AA:BB:CC:DD:EE:FE", 409), ("invalid", 400), (None, 400)])
def test_unconfigured_or_invalid_address_never_falls_back_to_first_sensor(address, status):
    device = config(1)
    state = DashboardState([device])
    state.direct = Controller(device)
    state.direct_devices = {device.address: state.direct}
    with server(state) as port:
        assert post(port, {"action": "connect", "address": address})[0] == status
    assert state.direct.commands == []


def test_cross_model_target_is_rejected_without_touching_a_controller():
    devices = [config(1), config(2, "HS3BB")]
    state = DashboardState(devices)
    state.direct_devices = {item.address: Controller(item) for item in devices}
    with server(state) as port:
        assert post(port, {"action": "connect", "address": devices[1].address})[0] == 409
        assert post(port, {"action": "connect", "model": "HS3BB", "address": devices[0].address}, "/api/sensor/direct")[0] == 409
    assert all(not controller.commands for controller in state.direct_devices.values())


def test_gatt_callback_uses_exact_device_address_and_keeps_legacy_callback():
    first, second = config(1), config(2)
    state = DashboardState([first, second])
    state.ingest_direct({"name": "occupancy_status", "value": 1, "updated_at": "2026-10-23T00:00:00Z", "address": second.address.lower()})
    snapshot = state.snapshot()["devices"]
    assert snapshot[0]["binary_sensors"] == {}
    assert snapshot[1]["binary_sensors"]["occupancy"]["value"] is True
    state.ingest_direct({"name": "occupancy_status", "value": 0, "updated_at": "2026-10-23T00:00:01Z"})
    assert state.snapshot()["devices"][0]["binary_sensors"]["occupancy"]["value"] is False


def test_service_constructs_each_instance_routes_callbacks_and_stops_every_controller(monkeypatch):
    devices = [config(1), config(2), config(3, "KS2BB"), config(4, "KS2BB"), config(5, "HS3BB"), config(6, "MS1BB")]
    controllers, started, stopped, autos, snapshot = [], [], [], [], {}
    settings = BridgeConfig(es5_auto_connect=True)
    monkeypatch.setattr(dashboard, "load_config", lambda _: devices)
    monkeypatch.setattr(dashboard, "load_bridge_config", lambda _: settings)

    class Direct(Controller):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            controllers.append(self)
        def start(self):
            started.append(self.device.address)
        def stop(self):
            stopped.append(self.device.address)

    class Worker:
        def __init__(self, state, *args, **kwargs):
            self.state = state
        def start(self):
            self.state.set_scanner("listening")
        def stop(self):
            pass

    class Auto:
        def __init__(self, controller, **kwargs):
            autos.append(controller.device.address)
        def start(self):
            pass
        def stop(self):
            pass

    class Server:
        def __init__(self, state, port):
            self.state, self.server_address, self.closing = state, ("127.0.0.1", port), threading.Event()
        def serve_forever(self, **kwargs):
            controllers[1].on_value({"name": "occupancy_status", "value": 1, "updated_at": "2026-10-23T00:00:00Z"})
            snapshot.update(self.state.snapshot())
        def server_close(self):
            pass

    for name in ("ProcessDirectController", "ProcessSensorController"):
        monkeypatch.setattr(dashboard, name, Direct)
    monkeypatch.setattr(dashboard, "MonitorWorker", Worker)
    monkeypatch.setattr(dashboard, "AutoConnector", Auto)
    monkeypatch.setattr(dashboard, "DashboardServer", Server)
    dashboard.serve_dashboard(config="synthetic-unused.json")
    assert len(controllers) == 6 and all(controller.defer_start for controller in controllers)
    assert started == [item.address for item in devices[:-1]]
    assert stopped == [item.address for item in devices]
    assert autos == [devices[0].address, devices[1].address]
    assert snapshot["devices"][0]["binary_sensors"] == {}
    assert snapshot["devices"][1]["binary_sensors"]["occupancy"]["value"] is True


def test_dashboard_lazy_worker_allocates_nothing_until_explicit_connect(tmp_path, monkeypatch):
    controller = ProcessDirectController(config(1), recording_dir=tmp_path)
    controller.defer_start = True
    controller.start()
    assert controller.worker is None and controller.thread is None and controller.loop is None
    started = []
    def fake_start():
        if controller.loop is None:
            started.append(True)
            controller.commands = asyncio.Queue()
            controller.loop = SimpleNamespace(call_soon_threadsafe=lambda callback, *args: callback(*args))
    monkeypatch.setattr(controller, "_start_now", fake_start)
    operation = controller.submit({"action": "connect"})
    assert started == [True]
    assert controller.commands.get_nowait()["id"] == operation
    assert controller.worker is None
    controller.loop, controller.commands = None, None
    controller.stop()


def test_missing_token_and_unsupported_model_do_not_start_lazy_workers(tmp_path, monkeypatch):
    for controller in (ProcessDirectController(config(1, token=None), recording_dir=tmp_path),
                       ProcessSensorController(config(2, "MS1BB"), recording_dir=tmp_path)):
        controller.defer_start = True
        monkeypatch.setattr(controller, "_start_now", lambda: pytest.fail("Unsupported connect must not spawn"))
        controller.start()
        with pytest.raises(DirectError):
            controller.submit({"action": "connect"})
        controller.stop()
