"""BLE transport wiring: no sockets, household config or Bluetooth hardware."""
from types import SimpleNamespace
import json
import threading

import pytest

from linptech_ble import cli, dashboard
from linptech_ble.transport_config import TransportConfig
from linptech_ble.config import DeviceConfig
from linptech_ble.transport import BluetoothConfig, ProxyConfig
from linptech_ble.standard_auth import DirectError


SYNTHETIC_API_KEY = "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="
SYNTHETIC_BINDKEY = "00112233445566778899aabbccddeeff"
SYNTHETIC_TOKEN = "00112233445566778899aabb"


def remote_config(*, services=True):
    return TransportConfig(
        bluetooth=BluetoothConfig(backend="esphome", proxies=(
            ProxyConfig("bedroom", "192.0.2.10", noise_psk=SYNTHETIC_API_KEY),
        ), gatt_proxy="bedroom"),
        es5_auto_connect=services,
    )


@pytest.fixture
def services(monkeypatch):
    calls, fault, instances = [], {}, {}
    settings = remote_config()
    device = DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Synthetic ES5",
                          SYNTHETIC_BINDKEY, SYNTHETIC_TOKEN)
    monkeypatch.setattr(dashboard, "load_transport_config", lambda path: settings)
    monkeypatch.setattr(dashboard, "load_config", lambda path: [device])

    def record(action):
        calls.append(action)
        if fault.get("at") == action:
            raise RuntimeError("synthetic startup failure")

    class Direct:
        def __init__(self, sensor, *, on_value, transport, recording):
            self.device, self.transport = sensor, transport
            self.recording = recording
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
        def __init__(self, state, devices, *, replay_path, transport, recording):
            self.state, self.transport = state, transport
            self.recording = recording
            instances["worker"] = self

        def start(self):
            record("worker.start")
            self.state.set_scanner("listening")

        def stop(self):
            record("worker.stop")

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
    monkeypatch.setattr(dashboard, "AutoConnector", Auto)
    monkeypatch.setattr(dashboard, "DashboardServer", Server)
    monkeypatch.setattr(dashboard.webbrowser, "open", lambda url: record("browser.open"))
    return SimpleNamespace(calls=calls, fault=fault, instances=instances, settings=settings)


def test_dashboard_routes_transport_and_closes_every_started_service(services):
    dashboard.serve_dashboard(config="not-read.json", transport_path="not-read-transport.json", open_browser=True)
    instances = services.instances
    assert instances["worker"].transport is services.settings.bluetooth
    assert instances["direct"].transport is services.settings.bluetooth
    assert instances["auto"].controller is instances["direct"]
    address = instances["direct"].device.address
    state = instances["server"].state
    assert state.direct_controller("ES5BB", address) is instances["direct"]
    assert instances["auto"].submit({"action": "connect"}) == "synthetic-operation"
    assert instances["commands"] == [{"action": "connect"}]
    with pytest.raises(DirectError):
        state.direct_controller("ES5BB", "AA:BB:CC:DD:EE:99")
    assert services.calls == [
        "worker.start", "direct.start", "auto.start", "browser.open", "server.serve",
        # GATT must close while its shared proxy advertisement owner is alive.
        "auto.stop", "direct.stop", "worker.stop", "server.close",
    ]
    snapshot = instances["server"].state.snapshot()
    assert snapshot["transport"] == {"backend": "esphome", "proxy_names": ["bedroom"], "gatt_proxy": "bedroom"}
    public = json.dumps(snapshot)
    for private in (SYNTHETIC_API_KEY, SYNTHETIC_BINDKEY, SYNTHETIC_TOKEN, "192.0.2.10"):
        assert private not in public


@pytest.mark.parametrize("failure", ["worker.start", "direct.start", "auto.start"])
def test_startup_failure_preserves_original_error_and_releases_http(services, failure):
    services.fault["at"] = failure
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        dashboard.serve_dashboard(config="not-read.json", transport_path="not-read-transport.json")
    assert "server.serve" not in services.calls
    assert services.calls[-1] == "server.close"
    assert "direct.stop" in services.calls and "auto.stop" in services.calls
    if failure != "worker.start":
        assert "worker.stop" in services.calls


def test_occupied_http_port_starts_no_radio_or_automatic_connections(services, monkeypatch):
    def occupied(*args, **kwargs):
        raise OSError("synthetic port already occupied")
    monkeypatch.setattr(dashboard, "DashboardServer", occupied)
    with pytest.raises(OSError, match="occupied"):
        dashboard.serve_dashboard(config="not-read.json", transport_path="not-read-transport.json")
    assert services.calls == []


def test_dashboard_cli_propagates_transport_argument_without_loading_files(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "serve_dashboard", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["dashboard", "--config", "synthetic-sensors.json", "--transport-config",
                     "synthetic-transport.json", "--port", "8767", "--open"]) == 0
    recording = calls[0].pop("recording")
    assert recording.enabled and recording.max_file_bytes == 10 * 1024 * 1024
    assert calls == [{"config": "synthetic-sensors.json", "port": 8767, "open_browser": True,
                      "replay_path": None, "stale_after": 180, "transport_path": "synthetic-transport.json"}]


@pytest.mark.parametrize("command", ["scan", "monitor"])
def test_capture_cli_propagates_remote_transport_without_starting_services(monkeypatch, command):
    from linptech_ble import transport_config
    settings = remote_config(services=False)
    calls = []
    monkeypatch.setattr(transport_config, "load_transport_config", lambda path: settings)
    monkeypatch.setattr(cli, "load_config", lambda path: [])

    async def capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(cli, "capture", capture)
    assert cli.main([command, "--transport-config", "synthetic-transport.json", "--seconds", "1"]) == 0
    assert len(calls) == 1 and calls[0]["transport"] is settings.bluetooth
