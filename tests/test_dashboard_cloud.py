"""Atomic selected credential imports and loopback cloud HTTP boundaries."""
import base64
from contextlib import contextmanager
from copy import deepcopy
from http.client import HTTPConnection
import json
import threading

import pytest

from linptech_ble import dashboard
from linptech_ble.config import DeviceConfig, load_config, save_config
from linptech_ble.dashboard import DashboardServer, DashboardState
from linptech_ble.dashboard_inventory import DashboardInventory
from linptech_ble.standard_auth import DirectError


FIRST, SECOND, NEW = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:03"
KEY, TOKEN = "00112233445566778899aabbccddeeff", "00112233445566778899aabb"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII=")


class Controller:
    def __init__(self, device):
        self.device, self.stopped = device, False
        self.state = {"status": "disconnected", "busy": False, "authenticated": False,
                      "calibration": {"restore_required": False}}
    def snapshot(self):
        return deepcopy(self.state)
    def stop(self):
        self.stopped = True


@pytest.fixture
def inventory(tmp_path):
    devices = [DeviceConfig(FIRST, "ES5BB", "Local radar name", KEY, TOKEN, "linp.sensor_occupy.es5b"),
               DeviceConfig(SECOND, "KS2BB", "Local climate name", "ab" * 16, "cd" * 12, "linp.sensor_ht.ks2bb")]
    path = tmp_path / "sensors.local.json"
    save_config(path, devices)
    state = DashboardState(devices)
    state.direct_devices = {device.address: Controller(device) for device in devices}
    state.direct = state.direct_devices[FIRST]
    state.sensor_direct = {"KS2BB": state.direct_devices[SECOND]}
    manager = DashboardInventory(state, devices, path, controller_factory=Controller,
        new_telemetry=lambda device: DashboardState([device]).devices[device.address])
    state.inventory = manager
    return manager


def test_selected_merge_saves_once_preserves_local_names_unselected_credentials_and_metadata(inventory, monkeypatch):
    import linptech_ble.dashboard_inventory as module
    calls = []
    original_save = module.save_config
    def save(path, devices):
        calls.append(path)
        original_save(path, devices)
    monkeypatch.setattr(module, "save_config", save)
    untouched = inventory.source()[1]
    controller = inventory.state.direct_devices[SECOND]
    expected = inventory.credential_snapshot([FIRST, NEW])
    result = inventory.merge_credentials([
        {"address": FIRST, "model": "ES5BB", "name": "Cloud name ignored", "bindkey": "ef" * 16, "auth_token": None},
        {"address": NEW, "model": "HS3BB", "name": "Cloud motion", "bindkey": KEY, "auth_token": TOKEN,
         "miot_model": "linp.motion.hs1bb2"}], expected=expected)
    assert calls == [inventory.path]
    devices = {device.address: device for device in load_config(inventory.path)}
    assert devices[FIRST].name == "Local radar name" and devices[FIRST].auth_token == TOKEN
    assert devices[FIRST].miot_model == "linp.sensor_occupy.es5b"
    assert devices[SECOND] == untouched and inventory.source()[1] is untouched
    assert inventory.state.direct_devices[SECOND] is controller and not controller.stopped
    assert devices[NEW].miot_model == "linp.motion.hs1bb2"
    assert set(device["address"] for device in result["devices"]) == {FIRST, NEW}
    assert KEY not in json.dumps(result) and TOKEN not in json.dumps(result)


def test_same_credentials_are_noop_even_when_selected_controller_is_connected(inventory, monkeypatch):
    import linptech_ble.dashboard_inventory as module
    controller = inventory.state.direct
    controller.state.update(authenticated=True, status="ready")
    original = inventory.source()
    before = inventory.path.read_bytes()
    def forbidden(*_):
        pytest.fail("Identical credentials must not cause a save or controller reset")
    monkeypatch.setattr(module, "save_config", forbidden)
    monkeypatch.setattr(inventory, "controller_factory", forbidden)
    inventory.merge_credentials([{"address": FIRST, "model": "ES5BB", "name": "Different cloud name",
                                  "bindkey": KEY, "auth_token": TOKEN}],
                                expected=inventory.credential_snapshot([FIRST]))
    assert inventory.source() is original and inventory.path.read_bytes() == before
    assert inventory.state.direct is controller and not controller.stopped


def test_missing_selected_credentials_never_clear_existing_values(inventory):
    before = inventory.source()[0]
    inventory.merge_credentials([{"address": FIRST, "model": "ES5BB", "bindkey": None, "auth_token": " ", "miot_model": None}],
                                expected=inventory.credential_snapshot([FIRST]))
    assert inventory.source()[0] is before


def test_verified_cloud_protocol_change_also_requires_selected_hs3_disconnected(inventory):
    inventory.mutate({"action": "add", "address": NEW, "model": "HS3BB", "name": "Local motion", "auth_token": TOKEN})
    controller = inventory.state.direct_devices[NEW]
    controller.state.update(status="ready", authenticated=True)
    before = inventory.path.read_bytes()
    with pytest.raises(DirectError):
        inventory.merge_credentials([{"address": NEW, "model": "HS3BB", "auth_token": TOKEN,
                                      "miot_model": "linp.motion.hs1bb2"}],
                                    expected=inventory.credential_snapshot([NEW]))
    assert inventory.path.read_bytes() == before and controller.device.miot_model is None


@pytest.mark.parametrize("change", [{"busy": True}, {"authenticated": True}, {"calibration": {"restore_required": True}}])
def test_any_selected_active_target_blocks_entire_changed_batch(inventory, change):
    inventory.state.direct.state.update(change)
    before, source = inventory.path.read_bytes(), inventory.source()
    expected = inventory.credential_snapshot([FIRST, NEW])
    with pytest.raises(DirectError):
        inventory.merge_credentials([{"address": NEW, "model": "HS3BB", "name": "New", "bindkey": KEY},
                                     {"address": FIRST, "model": "ES5BB", "bindkey": "ab" * 16}], expected=expected)
    assert inventory.path.read_bytes() == before and inventory.source() is source
    assert NEW not in inventory.state.devices and not inventory.state.direct.stopped


def test_unselected_active_device_does_not_block_selected_credential_import(inventory):
    inventory.state.direct_devices[SECOND].state.update(status="ready", authenticated=True)
    inventory.merge_credentials([{"address": FIRST, "model": "ES5BB", "bindkey": "ef" * 16}],
                                expected=inventory.credential_snapshot([FIRST]))
    assert not inventory.state.direct_devices[SECOND].stopped


@pytest.mark.parametrize("change", ["rename", "credential", "remove_readd", "add_absent"])
def test_selected_configuration_change_during_network_fetch_rejects_stale_commit(inventory, change):
    address = NEW if change == "add_absent" else FIRST
    expected = inventory.credential_snapshot([address])
    if change == "rename":
        inventory.mutate({"action": "update", "address": FIRST, "name": "User renamed during lookup"})
    elif change == "credential":
        inventory.mutate({"action": "update", "address": FIRST, "bindkey": "ef" * 16})
    elif change == "remove_readd":
        previous = inventory.source()[0]
        inventory.mutate({"action": "remove", "address": FIRST})
        inventory.mutate({"action": "add", "address": FIRST, "model": previous.model, "name": previous.name,
                          "bindkey": previous.bindkey, "auth_token": previous.auth_token, "miot_model": previous.miot_model})
    else:
        inventory.mutate({"action": "add", "address": NEW, "model": "ES5BB", "name": "Added during lookup"})
    before = inventory.path.read_bytes()
    with pytest.raises(DirectError):
        inventory.merge_credentials([{"address": address, "model": "ES5BB", "bindkey": "ab" * 16}], expected=expected)
    assert inventory.path.read_bytes() == before


def test_unselected_change_during_network_lookup_is_preserved(inventory):
    expected = inventory.credential_snapshot([FIRST])
    inventory.mutate({"action": "update", "address": SECOND, "name": "Renamed during lookup"})
    inventory.merge_credentials([{"address": FIRST, "model": "ES5BB", "bindkey": "ef" * 16}], expected=expected)
    assert load_config(inventory.path)[1].name == "Renamed during lookup"


@pytest.mark.parametrize("rows", [
    [{"address": FIRST, "model": "ES5BB", "bindkey": "ef" * 16}, {"address": SECOND, "model": "KS2BB", "auth_token": "bad"}],
    [{"address": FIRST, "model": "KS2BB", "bindkey": KEY}],
    [{"address": FIRST, "model": "ES5BB"}, {"address": FIRST.lower(), "model": "ES5BB"}],
    [{"address": FIRST, "model": "ES5BB", "private_payload": "never-echo"}],
])
def test_invalid_batch_never_partially_saves_valid_earlier_rows(inventory, rows):
    before, source = inventory.path.read_bytes(), inventory.source()
    with pytest.raises((ValueError, DirectError)):
        inventory.merge_credentials(rows, expected=inventory.credential_snapshot([FIRST, SECOND]))
    assert inventory.path.read_bytes() == before and inventory.source() is source
    assert not inventory.state.direct.stopped


def test_selected_merge_disk_failure_leaves_all_original_controllers_and_config(inventory, monkeypatch):
    import linptech_ble.dashboard_inventory as module
    before, source = inventory.path.read_bytes(), inventory.source()
    def fail(*_):
        raise OSError("private-sensitive-path")
    monkeypatch.setattr(module, "save_config", fail)
    with pytest.raises(DirectError) as error:
        inventory.merge_credentials([{"address": FIRST, "model": "ES5BB", "bindkey": "ab" * 16}],
                                    expected=inventory.credential_snapshot([FIRST]))
    assert "private-sensitive-path" not in str(error.value)
    assert inventory.path.read_bytes() == before and inventory.source() is source
    assert not inventory.state.direct.stopped


def test_new_device_without_any_retrieved_credentials_rejects_entire_batch(inventory):
    before, source = inventory.path.read_bytes(), inventory.source()
    with pytest.raises(ValueError):
        inventory.merge_credentials([{"address": FIRST, "model": "ES5BB", "bindkey": "ef" * 16},
                                     {"address": NEW, "model": "HS3BB", "name": "No credentials",
                                      "bindkey": None, "auth_token": None, "miot_model": "linp.motion.hs1bb2"}],
                                    expected=inventory.credential_snapshot([FIRST, NEW]))
    assert inventory.path.read_bytes() == before and inventory.source() is source
    assert NEW not in inventory.state.devices and not inventory.state.direct.stopped


class CloudManager:
    def __init__(self):
        self.cookie = "private-session-cookie"
        self.calls = []
    def snapshot(self):
        return {"enabled": True, "dependencies_available": True, "phase": "waiting", "run_id": "synthetic-run",
                "revision": 2, "qr_available": True, "devices": [{"id": "opaque-id", "address": FIRST,
                    "model": "ES5BB", "name": "Synthetic"}], "error": None}
    def qr_image(self, run_id, revision):
        return PNG if run_id == "synthetic-run" and revision == 2 else None
    def start(self, region):
        self.calls.append(("start", region))
        return self.snapshot()
    def cancel(self, run_id):
        self.calls.append(("cancel", run_id))
        return self.snapshot()
    def save(self, run_id, devices):
        self.calls.append(("save", run_id, devices))
        return self.snapshot()


@contextmanager
def serving(state):
    server = DashboardServer(state, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.closing.set()
        server.shutdown()
        server.server_close()
        thread.join(3)


def request(port, path, value=None, *, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        options = {"Content-Type": "application/json", "X-Linptech-Control": "1", **(headers or {})}
        connection.request("POST" if value is not None else "GET", path, json.dumps(value) if value is not None else None, options)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_cloud_endpoint_returns_only_redacted_snapshot_and_does_not_leak_into_exports(inventory):
    manager = inventory.state.cloud_credentials = CloudManager()
    with serving(inventory.state) as port:
        status, headers, body = request(port, "/api/cloud")
        assert status == 200 and json.loads(body)["devices"][0]["id"] == "opaque-id"
        assert headers["Cache-Control"] == "no-store"
        for path in ("/api/state", "/api/export"):
            _, _, exported = request(port, path)
            assert "opaque-id" not in exported.decode() and "synthetic-run" not in exported.decode()
            assert manager.cookie not in exported.decode() and KEY not in exported.decode() and TOKEN not in exported.decode()
        assert request(port, "/api/cloud", {"action": "start", "region": "cn"})[0] == 202
        assert request(port, "/api/cloud", {"action": "save", "run_id": "synthetic-run", "devices": ["opaque-id"]})[0] == 202
        assert request(port, "/api/cloud", {"action": "cancel", "run_id": "synthetic-run"})[0] == 202
    assert manager.calls == [("start", "cn"), ("save", "synthetic-run", ["opaque-id"]), ("cancel", "synthetic-run")]


def test_qr_endpoint_requires_exact_run_and_revision_and_disables_caching(inventory):
    inventory.state.cloud_credentials = CloudManager()
    with serving(inventory.state) as port:
        status, headers, body = request(port, "/api/cloud/qr.png?run=synthetic-run&revision=2")
        assert status == 200 and body == PNG and headers["Content-Type"] == "image/png"
        assert headers["Cache-Control"] == "no-store"
        assert request(port, "/api/cloud/qr.png?run=old-run&revision=2")[0] == 404
        assert request(port, "/api/cloud/qr.png?run=synthetic-run&revision=1")[0] == 404
        assert request(port, "/api/cloud/qr.png?run=synthetic-run&revision=2&revision=2")[0] == 400
        assert request(port, "/api/cloud/qr.png?run=synthetic-run&revision=-1")[0] == 400
        assert request(port, "/api/cloud/qr.png?run=synthetic-run&revision=2", headers={"Origin": "http://evil.invalid"})[0] == 403


@pytest.mark.parametrize("headers", [{"X-Linptech-Control": ""}, {"Content-Type": "text/plain"},
                                      {"Origin": "http://evil.invalid"}, {"Host": "evil.invalid"}])
def test_cloud_post_retains_host_origin_and_custom_header_checks(inventory, headers):
    manager = inventory.state.cloud_credentials = CloudManager()
    with serving(inventory.state) as port:
        assert request(port, "/api/cloud", {"action": "start", "region": "cn"}, headers=headers)[0] == 403
    assert manager.calls == []


@pytest.mark.parametrize("command", [
    {"action": "start"}, {"action": "start", "region": 1}, {"action": "start", "region": "cn", "password": "never-echo"},
    {"action": "save", "run_id": "", "devices": ["id"]}, {"action": "save", "run_id": "run", "devices": []},
    {"action": "save", "run_id": "run", "devices": [None]}, {"action": "cancel", "run_id": "run", "extra": "never-echo"},
])
def test_malformed_cloud_requests_never_reach_manager_or_echo_inputs(inventory, command):
    manager = inventory.state.cloud_credentials = CloudManager()
    with serving(inventory.state) as port:
        status, _, body = request(port, "/api/cloud", command)
        assert status == 400 and b"never-echo" not in body
    assert manager.calls == []


def test_unexpected_cloud_errors_are_redacted_and_replay_is_disabled(inventory):
    manager = inventory.state.cloud_credentials = CloudManager()
    def fail(*_):
        raise RuntimeError("private-session-cookie-and-account-url")
    manager.start = fail
    with serving(inventory.state) as port:
        status, _, body = request(port, "/api/cloud", {"action": "start", "region": "cn"})
        assert status == 503 and b"private-session" not in body
        inventory.state.mode = "replay"
        status, _, body = request(port, "/api/cloud")
        assert status == 200 and json.loads(body)["enabled"] is False
        assert request(port, "/api/cloud", {"action": "start", "region": "cn"})[0] == 409
        assert request(port, "/api/cloud/qr.png?run=synthetic-run&revision=2")[0] == 404


def test_dashboard_closes_cloud_manager_before_radio_cleanup_even_if_cloud_close_fails(tmp_path, monkeypatch):
    from linptech_ble import cloud_credentials
    calls = []
    class ManagedCloud:
        def __init__(self, inventory):
            assert inventory.state.mode == "live"
            calls.append("cloud.created")
        def close(self):
            calls.append("cloud.close")
            raise RuntimeError("private-session-close-error")
    class Worker:
        def __init__(self, state, *args, **kwargs):
            self.state = state
        def start(self):
            self.state.set_scanner("listening")
            calls.append("worker.start")
        def stop(self):
            calls.append("worker.stop")
    class Server:
        def __init__(self, state, port):
            self.state, self.server_address, self.closing = state, ("127.0.0.1", port), threading.Event()
        def serve_forever(self, **kwargs):
            calls.append("server.serve")
            assert self.state.cloud_credentials is not None
            assert "cloud_credentials" not in self.state.snapshot()
        def server_close(self):
            calls.append("server.close")
    monkeypatch.setattr(cloud_credentials, "CloudCredentialManager", ManagedCloud)
    monkeypatch.setattr(dashboard, "MonitorWorker", Worker)
    monkeypatch.setattr(dashboard, "DashboardServer", Server)
    dashboard.serve_dashboard(config=tmp_path / "missing.local.json")
    assert calls == ["cloud.created", "worker.start", "server.serve", "cloud.close", "worker.stop", "server.close"]
