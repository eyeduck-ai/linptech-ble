"""Bridge service wiring: no sockets, household config or Bluetooth hardware."""
from types import SimpleNamespace
import json
import threading

import pytest

from linptech_ble import cli, dashboard
from linptech_ble.bridge_config import BridgeConfig
from linptech_ble.config import DeviceConfig
from linptech_ble.ha_mqtt import MqttConfig
from linptech_ble.transport import BluetoothConfig, ProxyConfig
from linptech_ble.standard_auth import DirectError


SYNTHETIC_API_KEY = "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="
SYNTHETIC_BINDKEY = "00112233445566778899aabbccddeeff"
SYNTHETIC_TOKEN = "00112233445566778899aabb"


def remote_config(*, services=True):
    return BridgeConfig(
        bluetooth=BluetoothConfig(backend="esphome", proxies=(
            ProxyConfig("bedroom", "192.0.2.10", noise_psk=SYNTHETIC_API_KEY),
        ), gatt_proxy="bedroom"),
        mqtt=MqttConfig(enabled=services, host="192.0.2.20", username="synthetic-user",
                        password="synthetic-password"),
        es5_auto_connect=services,
    )


@pytest.fixture
def services(monkeypatch):
    calls, fault, instances = [], {}, {}
    settings = remote_config()
    device = DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Synthetic ES5",
                          SYNTHETIC_BINDKEY, SYNTHETIC_TOKEN)
    monkeypatch.setattr(dashboard, "load_bridge_config", lambda path: settings)
    monkeypatch.setattr(dashboard, "load_config", lambda path: [device])

    def record(action):
        calls.append(action)
        if fault.get("at") == action:
            raise RuntimeError("synthetic startup failure")

    class Direct:
        def __init__(self, sensor, *, on_value, transport):
            self.device, self.transport = sensor, transport
            instances["direct"] = self

        def start(self):
            record("direct.start")

        def stop(self):
            record("direct.stop")

        def submit(self, value):
            instances.setdefault("commands", []).append(value)
            return "synthetic-operation"

        def snapshot(self):
            return {"status": "disconnected", "authenticated": False,
                    "token_configured": True}

    class Worker:
        def __init__(self, state, devices, *, replay_path, transport):
            self.state, self.transport = state, transport
            instances["worker"] = self

        def start(self):
            record("worker.start")
            self.state.set_scanner("listening")

        def stop(self):
            record("worker.stop")

    class Mqtt:
        def __init__(self, config, snapshot, submit, *, targeted_submit=None):
            self.config, self.get_snapshot, self.submit = config, snapshot, submit
            self.targeted_submit = targeted_submit
            instances["mqtt"] = self

        def start(self):
            record("mqtt.start")

        def stop(self):
            record("mqtt.stop")

        def status(self):
            return {"enabled": True, "status": "connected"}

    class Auto:
        def __init__(self, controller, *, retry_seconds, ready, submit=None):
            self.controller, self.retry_seconds, self.ready = controller, retry_seconds, ready
            self.submit = submit
            instances["auto"] = self

        def start(self):
            assert self.ready()
            record("auto.start")

        def stop(self):
            record("auto.stop")

    class Server:
        def __init__(self, state, port):
            self.state, self.server_address = state, ("127.0.0.1", port)
            self.closing = threading.Event()
            instances["server"] = self

        def serve_forever(self, *, poll_interval):
            record("server.serve")

        def server_close(self):
            assert self.closing.is_set()
            record("server.close")

    for name in ("DirectController", "ProcessDirectController"):
        monkeypatch.setattr(dashboard, name, Direct)
    monkeypatch.setattr(dashboard, "MonitorWorker", Worker)
    monkeypatch.setattr(dashboard, "HomeAssistantBridge", Mqtt)
    monkeypatch.setattr(dashboard, "AutoConnector", Auto)
    monkeypatch.setattr(dashboard, "DashboardServer", Server)
    monkeypatch.setattr(dashboard.webbrowser, "open", lambda url: record("browser.open"))
    return SimpleNamespace(calls=calls, fault=fault, instances=instances, settings=settings)


def test_bridge_service_routes_transport_and_closes_every_started_service(services):
    dashboard.serve_dashboard(config="not-read.json", bridge_path="not-read-bridge.json", open_browser=True)
    instances = services.instances
    assert instances["worker"].transport is services.settings.bluetooth
    assert instances["direct"].transport is services.settings.bluetooth
    assert instances["mqtt"].config is services.settings.mqtt
    assert instances["auto"].controller is instances["direct"]
    assert instances["mqtt"].submit({"action": "connect"}) == "synthetic-operation"
    address = instances["direct"].device.address
    assert instances["mqtt"].targeted_submit(address, {"action": "refresh"}) == "synthetic-operation"
    assert instances["auto"].submit({"action": "connect"}) == "synthetic-operation"
    assert instances["commands"] == [{"action": "connect"}, {"action": "refresh"}, {"action": "connect"}]
    with pytest.raises(DirectError):
        instances["mqtt"].targeted_submit("AA:BB:CC:DD:EE:99", {"action": "connect"})
    assert services.calls == [
        "worker.start", "direct.start", "mqtt.start", "auto.start", "browser.open", "server.serve",
        # GATT must close while its shared proxy advertisement owner is alive.
        "auto.stop", "mqtt.stop", "direct.stop", "worker.stop", "server.close",
    ]
    snapshot = instances["server"].state.snapshot()
    assert snapshot["transport"] == {"backend": "esphome", "proxy_names": ["bedroom"], "gatt_proxy": "bedroom"}
    assert snapshot["mqtt"] == {"enabled": True, "status": "connected"}
    public = json.dumps(snapshot)
    for private in (SYNTHETIC_API_KEY, SYNTHETIC_BINDKEY, SYNTHETIC_TOKEN,
                    "synthetic-password", "synthetic-user", "192.0.2.10", "192.0.2.20"):
        assert private not in public


@pytest.mark.parametrize("failure", ["worker.start", "direct.start", "mqtt.start"])
def test_startup_failure_preserves_original_error_and_releases_http(services, failure):
    services.fault["at"] = failure
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        dashboard.serve_dashboard(config="not-read.json", bridge_path="not-read-bridge.json")
    assert "server.serve" not in services.calls
    assert services.calls[-1] == "server.close"
    assert "direct.stop" in services.calls and "mqtt.stop" in services.calls
    if failure != "worker.start":
        assert "worker.stop" in services.calls


def test_occupied_http_port_starts_no_ble_or_mqtt_services(services, monkeypatch):
    def occupied(*args, **kwargs):
        raise OSError("synthetic port already occupied")
    monkeypatch.setattr(dashboard, "DashboardServer", occupied)
    with pytest.raises(OSError, match="occupied"):
        dashboard.serve_dashboard(config="not-read.json", bridge_path="not-read-bridge.json")
    assert services.calls == []


def test_dashboard_cli_propagates_bridge_argument_without_loading_files(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "serve_dashboard", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["dashboard", "--config", "synthetic-sensors.json", "--bridge-config",
                     "synthetic-bridge.json", "--port", "8767", "--open"]) == 0
    assert calls == [{"config": "synthetic-sensors.json", "port": 8767, "open_browser": True,
                      "replay_path": None, "stale_after": 180, "bridge_path": "synthetic-bridge.json"}]


@pytest.mark.parametrize("command", ["scan", "monitor"])
def test_capture_cli_propagates_remote_transport_without_starting_services(monkeypatch, command):
    from linptech_ble import bridge_config
    settings = remote_config(services=False)
    calls = []
    monkeypatch.setattr(bridge_config, "load_bridge_config", lambda path: settings)
    monkeypatch.setattr(cli, "load_config", lambda path: [])

    async def capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(cli, "capture", capture)
    assert cli.main([command, "--bridge-config", "synthetic-bridge.json", "--seconds", "1"]) == 0
    assert len(calls) == 1 and calls[0]["transport"] is settings.bluetooth
