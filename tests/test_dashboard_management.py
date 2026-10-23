"""Inventory mutations and discovery use synthetic devices and loopback HTTP only."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from http.client import HTTPConnection
import json
import threading
from types import SimpleNamespace

import pytest

from linptech_ble import dashboard, runtime
from linptech_ble.config import DeviceConfig, load_config, save_config
from linptech_ble.dashboard import DashboardServer, DashboardState
from linptech_ble.dashboard_inventory import DashboardInventory, NearbyDiscovery
from linptech_ble.models import AdvertisementRecord, MIBEACON_UUID
from linptech_ble.standard_auth import DirectError
from conftest import ADDRESS, TEST_KEY, object_bytes


TOKEN = "00112233445566778899aabb"


class Controller:
    def __init__(self, device):
        self.device, self.stopped = device, False
        self.state = {"status": "disconnected", "authenticated": False, "busy": False,
                      "calibration": {"restore_required": False}}

    def snapshot(self):
        return deepcopy(self.state)

    def stop(self):
        self.stopped = True


@pytest.fixture
def inventory(tmp_path):
    devices = [DeviceConfig(ADDRESS, "ES5BB", "Original", TEST_KEY, TOKEN)]
    state = DashboardState(devices)
    state.direct_devices[ADDRESS] = Controller(devices[0])
    state.direct = state.direct_devices[ADDRESS]
    path = tmp_path / "sensors.local.json"
    save_config(path, devices)
    manager = DashboardInventory(state, devices, path, controller_factory=Controller,
        new_telemetry=lambda device: DashboardState([device]).devices[device.address])
    state.inventory = manager
    return manager


@contextmanager
def serving(state):
    instance = DashboardServer(state, port=0)
    thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield instance.server_address[1]
    finally:
        instance.closing.set()
        instance.shutdown()
        instance.server_close()
        thread.join(3)


def request(port, route, value=None, *, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        options = {"Content-Type": "application/json", "X-Linptech-Control": "1"}
        options.update(headers or {})
        connection.request("POST" if value is not None else "GET", route,
                           json.dumps(value) if value is not None else None, options)
        response = connection.getresponse()
        raw = response.read().decode()
        return response.status, raw
    finally:
        connection.close()


def test_add_update_remove_immediately_changes_inventory_and_preserves_other_telemetry(inventory):
    original_source = inventory.source()
    telemetry = inventory.state.devices[ADDRESS]
    telemetry["packets"] = 11
    result = inventory.mutate({"action": "add", "address": "aabbccddee02", "model": "KS2BB", "name": "Bedroom",
                               "bindkey": TEST_KEY, "auth_token": TOKEN})
    address = "AA:BB:CC:DD:EE:02"
    assert len(result["devices"]) == 2
    assert inventory.source() is not original_source
    assert inventory.state.devices[ADDRESS] is telemetry and telemetry["packets"] == 11
    assert inventory.state.direct_devices[address].device.auth_token == TOKEN
    assert load_config(inventory.path)[1].bindkey == TEST_KEY
    controller = inventory.state.direct_devices[address]
    inventory.mutate({"action": "update", "address": address, "name": "Renamed", "bindkey": "", "auth_token": " "})
    assert inventory.state.direct_devices[address] is controller
    assert controller.device.name == "Renamed" and controller.device.bindkey == TEST_KEY
    inventory.mutate({"action": "remove", "address": address})
    assert controller.stopped
    assert address not in inventory.state.devices and address not in inventory.state.direct_devices
    assert len(load_config(inventory.path)) == 1


def test_secret_replacement_rebuilds_controller_and_clears_key_verification(inventory):
    old = inventory.state.direct
    inventory.state.devices[ADDRESS]["verified"] = True
    inventory.mutate({"action": "update", "address": ADDRESS, "bindkey": "ab" * 16})
    assert old.stopped
    assert inventory.state.direct is not old
    assert inventory.state.direct.device.auth_token == TOKEN
    assert inventory.state.devices[ADDRESS]["verified"] is False
    assert inventory.state.devices[ADDRESS]["key_configured"] is True


@pytest.mark.parametrize("change", [{"busy": True}, {"authenticated": True}, {"connected": True},
    {"status": "connecting"}, {"status": "error"}, {"calibration": {"restore_required": True}}])
def test_active_or_pending_restore_rejects_destructive_changes_but_allows_rename(inventory, change):
    inventory.state.direct.state.update(change)
    before = inventory.path.read_bytes()
    with pytest.raises(DirectError):
        inventory.mutate({"action": "remove", "address": ADDRESS})
    with pytest.raises(DirectError):
        inventory.mutate({"action": "update", "address": ADDRESS, "auth_token": "ab" * 12})
    assert inventory.path.read_bytes() == before
    inventory.mutate({"action": "update", "address": ADDRESS, "name": "Renamed while connected"})
    assert inventory.state.direct.device.name == "Renamed while connected"
    assert not inventory.state.direct.stopped


@pytest.mark.parametrize("command", [
    {"action": "add", "address": ADDRESS, "model": "ES5BB", "name": "Duplicate"},
    {"action": "update", "address": "AA:BB:CC:DD:EE:99", "name": "Unknown"},
    {"action": "remove", "address": ADDRESS, "bindkey": TEST_KEY},
    {"action": "update", "address": ADDRESS, "model": "KS2BB"},
    {"action": "update", "address": ADDRESS, "name": "bad\nname"},
    {"action": "update", "address": ADDRESS, "bindkey": None},
    {"action": "update", "address": ADDRESS, "auth_token": "bad-token"},
    {"action": "update", "address": ADDRESS, "unknown": "ignored?"},
])
def test_invalid_mutations_never_change_file_or_runtime(inventory, command):
    before = inventory.path.read_bytes()
    original = inventory.source()
    with pytest.raises((ValueError, DirectError)):
        inventory.mutate(command)
    assert inventory.path.read_bytes() == before and inventory.source() is original


def test_atomic_write_failure_leaves_original_runtime_and_secrets_unexposed(inventory, monkeypatch):
    import linptech_ble.dashboard_inventory as module
    def fail(*_):
        raise OSError("secret-private-directory")
    monkeypatch.setattr(module, "save_config", fail)
    original = inventory.source()
    with pytest.raises(DirectError) as error:
        inventory.mutate({"action": "remove", "address": ADDRESS})
    assert "secret" not in str(error.value)
    assert inventory.source() is original and not inventory.state.direct.stopped


def test_http_mutations_are_redacted_and_apply_without_service_restart(inventory):
    with serving(inventory.state) as port:
        status, text = request(port, "/api/devices")
        assert status == 200 and json.loads(text)["devices"][0]["token_configured"]
        assert TEST_KEY not in text and TOKEN not in text
        status, text = request(port, "/api/devices", {"action": "update", "address": ADDRESS, "name": "Office"})
        assert status == 200 and json.loads(text)["devices"][0]["name"] == "Office"
        status, text = request(port, "/api/devices", {"action": "update", "address": ADDRESS, "bindkey": "do-not-echo"})
        assert status == 400 and "do-not-echo" not in text
        status, text = request(port, "/api/state")
        assert json.loads(text)["devices"][0]["name"] == "Office"
        assert TEST_KEY not in text and TOKEN not in text


@pytest.mark.parametrize("route", ["/api/devices", "/api/discovery"])
@pytest.mark.parametrize("headers", [{"X-Linptech-Control": ""}, {"Origin": "http://evil.invalid"},
                                      {"Host": "evil.invalid"}, {"Content-Type": "text/plain"}])
def test_inventory_and_discovery_retain_loopback_csrf_checks(inventory, route, headers):
    with serving(inventory.state) as port:
        status, _ = request(port, route, {"action": "start"} if route.endswith("discovery") else
                            {"action": "remove", "address": ADDRESS}, headers=headers)
        assert status == 403
    assert len(inventory.source()) == 1 and inventory.discovery.snapshot()["status"] == "stopped"


def test_discovery_uses_public_metadata_only_and_is_time_and_size_bounded(inventory, make_record):
    clock = [100.0]
    discovery = NearbyDiscovery(lambda: {ADDRESS}, clock=lambda: clock[0])
    assert discovery.command("start")["status"] == "scanning"
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    discovery.observe(record)
    row = discovery.snapshot()["devices"][0]
    assert row["supported"] and row["configured"] and row["model"] == "ES5BB"
    assert not {"service_data", "manufacturer_data", "bindkey", "auth_token"} & row.keys()
    for number in range(300):
        discovery.observe(AdvertisementRecord(record.timestamp, f"AA:BB:CC:DD:{number//256:02X}:{number%256:02X}",
            "Unknown MiBeacon", -80, {MIBEACON_UUID: "0059ffff00"}, {}, []))
    assert len(discovery.snapshot()["devices"]) == 256
    clock[0] = 161
    before = discovery.snapshot()["devices"]
    discovery.observe(record)
    assert discovery.snapshot()["status"] == "stopped" and discovery.snapshot()["devices"] == before
    assert discovery.command("start")["devices"] == []
    assert discovery.command("stop")["status"] == "stopped"


def test_discovery_ignores_unrelated_ble_and_keeps_unknown_mibeacon_unconfirmed(inventory):
    discovery = inventory.discovery
    discovery.command("start")
    record = AdvertisementRecord("2026-10-23T00:00:00Z", ADDRESS, "Unrelated BLE", -60, {}, {}, [])
    discovery.observe(record)
    assert discovery.snapshot()["devices"] == []
    record.service_data = {MIBEACON_UUID: "0059ffff00"}
    discovery.observe(record)
    assert discovery.snapshot()["devices"][0]["supported"] is False
    assert discovery.snapshot()["devices"][0]["model"] is None


def test_hs3_direct_protocol_requires_explicit_confirmation(inventory):
    from linptech_ble.sensor_direct import SensorDirectController
    address = "AA:BB:CC:DD:EE:03"
    inventory.mutate({"action": "add", "address": address, "model": "HS3BB", "name": "Motion",
                      "auth_token": TOKEN})
    device = inventory.source()[-1]
    assert device.miot_model is None
    controller = SensorDirectController(device)
    assert controller.model_confirmed is False
    controller.loop = SimpleNamespace(call_soon_threadsafe=lambda *_: None)
    controller.commands = asyncio.Queue()
    controller.state.update(authenticated=True, status="ready")
    controller.state["properties"]["detection_interval"].update(status="read", value=10)
    with pytest.raises(DirectError, match="HS3BB"):
        controller.submit({"action": "set", "property": "detection_interval", "value": 20})
    inventory.mutate({"action": "update", "address": address, "miot_model": "linp.motion.hs1bb2"})
    assert inventory.source()[-1].miot_model == "linp.motion.hs1bb2"
    assert SensorDirectController(inventory.source()[-1]).model_confirmed is True
    assert load_config(inventory.path)[-1].miot_model == "linp.motion.hs1bb2"


def test_replay_inventory_and_discovery_are_readonly(inventory):
    inventory.state.mode = "replay"
    with serving(inventory.state) as port:
        assert request(port, "/api/discovery", {"action": "start"})[0] == 409
        assert request(port, "/api/devices", {"action": "remove", "address": ADDRESS})[0] == 409
        assert json.loads(request(port, "/api/devices")[1])["enabled"] is False


def test_capture_refreshes_credentials_without_starting_another_scanner(tmp_path, monkeypatch, make_record):
    import bleak
    current = [DeviceConfig(ADDRESS, "ES5BB", "No key yet")]
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    decoded, observed, lifecycle = [], [], []
    class Scanner:
        def __init__(self, *, detection_callback):
            self.callback = detection_callback
        async def __aenter__(self):
            nonlocal current
            advertisement = SimpleNamespace(local_name=record.name, rssi=-55, tx_power=None,
                service_data={MIBEACON_UUID: bytes.fromhex(record.service_data[MIBEACON_UUID])},
                manufacturer_data={}, service_uuids=[MIBEACON_UUID])
            device = SimpleNamespace(address=ADDRESS, name=record.name)
            self.callback(device, advertisement)
            current = [DeviceConfig(ADDRESS, "ES5BB", "New key", TEST_KEY)]
            self.callback(device, advertisement)
            # Unknown advertisements go to discovery but never into capture files.
            self.callback(SimpleNamespace(address="AA:BB:CC:DD:EE:99", name="Other"), advertisement)
            lifecycle.append("start")
        async def __aexit__(self, *_):
            lifecycle.append("stop")
    async def immediate(_):
        return None
    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    monkeypatch.setattr(runtime, "_wait_duration", immediate)
    output, raw = tmp_path / "events.jsonl", tmp_path / "raw.jsonl"
    asyncio.run(runtime.capture(mode="monitor", seconds=1, output=output, raw_output=raw, devices=current,
        configured_only=True, quiet=True, device_source=lambda: current,
        on_record=decoded.append, on_raw_record=observed.append))
    assert [packet["status"] for packet in decoded] == ["encrypted_no_key", "decoded"]
    assert len(observed) == 3 and lifecycle == ["start", "stop"]
    assert len(raw.read_text(encoding="utf-8").splitlines()) == 2
    assert decoded[-1]["name"] == "New key" and TEST_KEY not in output.read_text(encoding="utf-8")


def test_service_with_empty_inventory_exposes_management_without_creating_workers(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setattr(dashboard, "load_config", lambda _: [])
    class Worker:
        def __init__(self, state, *args, **kwargs):
            self.state = state
        def start(self):
            self.state.set_scanner("listening")
        def stop(self):
            pass
    class Server:
        def __init__(self, state, port):
            self.state, self.server_address, self.closing = state, ("127.0.0.1", port), threading.Event()
        def serve_forever(self, **kwargs):
            observed.update(self.state.snapshot())
        def server_close(self):
            pass
    monkeypatch.setattr(dashboard, "MonitorWorker", Worker)
    monkeypatch.setattr(dashboard, "DashboardServer", Server)
    dashboard.serve_dashboard(config=tmp_path / "not-created.local.json")
    assert observed["management"]["enabled"] and observed["devices"] == []
    assert not (tmp_path / "not-created.local.json").exists()


def test_auto_submit_skips_held_inventory_lock_so_stop_cannot_deadlock(inventory):
    from linptech_ble.auto_connect import AutoConnector
    controller = inventory.state.direct
    controller.state["token_configured"] = True
    commands, entered = [], threading.Event()
    controller.submit = lambda command: commands.append(command) or "operation-id"
    def submit(command):
        entered.set()
        return inventory.submit_auto(controller, command)
    auto = AutoConnector(controller, submit=submit)
    with inventory.lock:
        auto.start()
        assert entered.wait(1)
        auto.stop()
        assert auto.thread is not None and not auto.thread.is_alive()
    assert commands == []


def test_stale_auto_controller_cannot_connect_after_replacement_or_removal(inventory):
    from linptech_ble.auto_connect import AutoConnector
    old = inventory.state.direct
    old.state["token_configured"] = True
    commands = []
    old.submit = lambda command: commands.append(command) or "operation-id"
    auto = AutoConnector(old, submit=lambda command: inventory.submit_auto(old, command))
    inventory.mutate({"action": "update", "address": ADDRESS, "auth_token": "ab" * 12})
    auto.tick(0)
    inventory.mutate({"action": "remove", "address": ADDRESS})
    auto.tick(1)
    assert commands == []


def test_auto_guard_rechecks_manual_disconnect_and_authenticated_state(inventory):
    controller = inventory.state.direct
    controller.state["token_configured"] = True
    commands = []
    controller.submit = lambda command: commands.append(command) or "operation-id"
    controller.state["last_operation"] = {"action": "disconnect"}
    assert inventory.submit_auto(controller, {"action": "connect"}) is None
    controller.state["last_operation"] = None
    controller.state["authenticated"] = True
    assert inventory.submit_auto(controller, {"action": "connect"}) is None
    controller.state["authenticated"] = False
    assert inventory.submit_auto(controller, {"action": "connect"}) == "operation-id"
    assert commands == [{"action": "connect"}]


@pytest.mark.parametrize("mutation", ["replace_key", "remove_and_readd"])
def test_decode_publication_and_inventory_mutation_are_one_transaction(inventory, tmp_path, monkeypatch, make_record, mutation):
    import bleak
    from linptech_ble.decoder import Decoder
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    decoded, attempting, mutated = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_decode = Decoder.decode
    def paused_decode(self, value):
        result = original_decode(self, value)
        assert result["bindkey_verified"] is True
        decoded.set()
        assert attempting.wait(2)
        # The competing HTTP-equivalent mutation is now attempting to acquire
        # the inventory lock. It must remain blocked until state.ingest returns.
        assert not mutated.wait(.15)
        return result
    monkeypatch.setattr(Decoder, "decode", paused_decode)
    def change_inventory():
        try:
            assert decoded.wait(2)
            attempting.set()
            if mutation == "replace_key":
                inventory.mutate({"action": "update", "address": ADDRESS, "bindkey": "ab" * 16})
            else:
                inventory.mutate({"action": "remove", "address": ADDRESS})
                inventory.mutate({"action": "add", "address": ADDRESS, "model": "ES5BB", "name": "Replacement", "bindkey": "ab" * 16})
            mutated.set()
        except BaseException as error:
            errors.append(error)
    mutator = threading.Thread(target=change_inventory)
    class Scanner:
        def __init__(self, *, detection_callback):
            self.callback = detection_callback
        async def __aenter__(self):
            mutator.start()
            advertisement = SimpleNamespace(local_name=record.name, rssi=-55, tx_power=None,
                service_data={MIBEACON_UUID: bytes.fromhex(record.service_data[MIBEACON_UUID])},
                manufacturer_data={}, service_uuids=[MIBEACON_UUID])
            self.callback(SimpleNamespace(address=ADDRESS, name=record.name), advertisement)
        async def __aexit__(self, *_):
            pass
    def ingest(packet):
        assert not mutated.is_set()
        inventory.state.ingest(packet)
        assert inventory.state.devices[ADDRESS]["verified"] is True
    async def immediate(_):
        return None
    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    monkeypatch.setattr(runtime, "_wait_duration", immediate)
    try:
        asyncio.run(runtime.capture(mode="monitor", seconds=1, output=tmp_path / "transaction.events.jsonl",
            devices=inventory.source(), configured_only=True, quiet=True, device_source=inventory.source,
            callback_lock=inventory.lock, on_record=ingest))
    finally:
        mutator.join(3)
    assert not errors and not mutator.is_alive() and mutated.is_set()
    assert inventory.source()[0].bindkey == "ab" * 16
    assert inventory.state.devices[ADDRESS]["verified"] is False
    assert inventory.state.devices[ADDRESS]["packets"] == (1 if mutation == "replace_key" else 0)
