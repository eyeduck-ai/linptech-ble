"""Remote Native API protocol tests; no ESP32, local radio or real secrets."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import bleak
import pytest

from linptech_ble import runtime, transport
from linptech_ble.config import DeviceConfig
from linptech_ble.transport import BluetoothConfig, ProxyConfig, TransportError, connected_client, scanner_context
from conftest import TEST_KEY, object_bytes

PSK = base64.b64encode(bytes(range(32))).decode()
PASSWORD = "public-test-password"


@pytest.fixture
def remote_api(monkeypatch):
    api = pytest.importorskip("aioesphomeapi")
    pytest.importorskip("bleak_esphome")
    from test_standard_auth import Peer
    from linptech_ble.standard_auth import AUTH, NOTIFY, UPNP, WRITE, uuid16

    class FakeAPI:
        instances = []
        pending = []
        fail_hosts = set()
        block_hosts = set()
        feature_by_host = {}
        pending_by_host = {}
        physical_by_host = {}
        connection_limit = 3
        report_connect_slots = True
        feature_flags = (api.BluetoothProxyFeature.PASSIVE_SCAN | api.BluetoothProxyFeature.ACTIVE_CONNECTIONS
                         | api.BluetoothProxyFeature.FEATURE_STATE_AND_MODE)
        confirm_subscription = True
        scanner_state = api.BluetoothScannerState.RUNNING
        owners = {}

        def __init__(self, config):
            self.config = config
            self.loop = asyncio.get_running_loop()
            self.api_version = api.APIVersion(1, 12)
            self.connected, self.closed = False, False
            self.ad_subscriptions, self.slot_subscriptions = 0, 0
            self.ad_callback, self.state_callback, self.slots_callback = None, None, None
            self.peer = Peer()
            self.handles = {index + 2: uuid for index, uuid in enumerate((AUTH, NOTIFY, UPNP, WRITE, uuid16(4)))}
            self.disconnect_count = 0
            self.ble_callbacks = {}
            self.peers = {}
            self.connect_calls = []
            self.flags = self.feature_by_host.get(config.host, self.feature_flags)
            self.instances.append(self)

        async def connect(self, on_stop, **kwargs):
            self.on_stop = on_stop
            if self.config.host in self.fail_hosts:
                raise OSError(f"sensitive {PSK} {PASSWORD}")
            if self.config.host in self.block_hosts:
                await asyncio.Event().wait()
            self.connected = True

        async def disconnect(self):
            self.closed, self.connected = True, False
            await self.on_stop(True)

        async def drop(self):
            if asyncio.get_running_loop() is not self.loop:
                return await transport._broker().call(self.drop())
            self.connected = False
            await self.on_stop(False)

        async def device_info(self):
            suffix = hashlib.sha256(self.config.host.encode()).digest()[:5]
            physical = ":".join(f"{byte:02X}" for byte in b"\x02" + suffix)
            identity = self.physical_by_host.get(self.config.host, {"mac_address": physical, "bluetooth_mac_address": physical})
            return api.DeviceInfo(name="test-proxy", **identity, bluetooth_proxy_feature_flags=int(self.flags))

        def subscribe_bluetooth_scanner_state(self, callback):
            self.state_callback = callback
            return lambda: setattr(self, "state_callback", None)

        def subscribe_bluetooth_le_advertisements(self, callback):
            self.owners[self.config.host] = self
            self.ad_subscriptions += 1
            self.ad_callback = callback
            for record in self.pending_by_host.get(self.config.host, self.pending):
                self.emit(record)
            if self.confirm_subscription and self.state_callback:
                self.state_callback(api.BluetoothScannerStateResponse(state=self.scanner_state))
            return lambda: setattr(self, "ad_callback", None)

        def subscribe_bluetooth_le_raw_advertisements(self, callback):
            return self.subscribe_bluetooth_le_advertisements(callback)

        def emit(self, record):
            if asyncio.get_running_loop() is not self.loop:
                self.loop.call_soon_threadsafe(self.emit, record)
                return
            if self.flags & api.BluetoothProxyFeature.RAW_ADVERTISEMENTS:
                service = next(iter(record.service_data.values()))
                data = b"\x03\x03\x95\xfe"  # Complete 16-bit service UUID list.
                payload = b"\x95\xfe" + bytes.fromhex(service)
                data += bytes([len(payload) + 1, 0x16]) + payload
                data += b"\x02\x0a\xf8"  # Measured advertisement TX power -8.
                self.ad_callback(SimpleNamespace(advertisements=[SimpleNamespace(
                    address=int(record.address.replace(":", ""), 16), rssi=record.rssi,
                    address_type=0, data=data)]))
                return
            self.ad_callback(api.BluetoothLEAdvertisement(
                address=int(record.address.replace(":", ""), 16), rssi=record.rssi,
                address_type=0, name=record.name,
                service_uuids=record.service_uuids,
                service_data={k: bytes.fromhex(v) for k, v in record.service_data.items()},
                manufacturer_data={int(k): bytes.fromhex(v) for k, v in record.manufacturer_data.items()}))

        def subscribe_bluetooth_connections_free(self, callback):
            self.slot_subscriptions += 1
            self.slots_callback = callback
            callback(self.connection_limit, self.connection_limit, [])
            return lambda: setattr(self, "slots_callback", None)

        async def bluetooth_device_connect(self, address, callback, **kwargs):
            assert asyncio.get_running_loop() is self.loop
            # Real firmware routes all BLE replies to its advertisement owner.
            assert self.owners[self.config.host] is self
            self.connect_calls.append(address)
            assert len(self.ble_callbacks) < self.connection_limit, "Proxy slot overcommitted"
            # Yield before the allocation update so concurrent callers would
            # all observe the same free count without transport reservations.
            await asyncio.sleep(.005)
            self.ble_callback = callback
            self.ble_callbacks[address] = callback
            self.peers.setdefault(address, self.peer if not self.peers else Peer())
            callback(True, 23, 0)
            if self.report_connect_slots:
                self.slots_callback(self.connection_limit - len(self.ble_callbacks), self.connection_limit, list(self.ble_callbacks))
            return lambda: None

        async def bluetooth_device_disconnect(self, address):
            self.disconnect_count += 1
            callback = self.ble_callbacks.pop(address, None)
            if callback:
                callback(False, 23, 0)
            self.slots_callback(self.connection_limit - len(self.ble_callbacks), self.connection_limit, list(self.ble_callbacks))

        def bluetooth_device_disconnect_no_wait(self, address):
            callback = self.ble_callbacks.pop(address, None)
            if callback:
                callback(False, 23, 0)

        async def bluetooth_gatt_get_services(self, address):
            assert self.owners[self.config.host] is self
            return SimpleNamespace(services=[SimpleNamespace(handle=1, uuid=uuid16(0xFE95), characteristics=[
                SimpleNamespace(handle=handle, uuid=uuid, properties=0x16, descriptors=[])
                for handle, uuid in self.handles.items()])])

        async def bluetooth_gatt_write(self, address, handle, value, response):
            assert asyncio.get_running_loop() is self.loop
            assert self.owners[self.config.host] is self
            await self.peers[address].write_gatt_char(self.handles[handle], value, response=response)

        async def bluetooth_gatt_start_notify(self, address, handle, callback, timeout):
            assert asyncio.get_running_loop() is self.loop
            assert self.owners[self.config.host] is self
            uuid = self.handles[handle]
            peer = self.peers[address]
            await peer.start_notify(uuid, lambda _, data: callback(handle, data))
            async def stop():
                await peer.stop_notify(uuid)
            def abort():
                peer.callbacks.pop(uuid, None)
            return stop, abort

    monkeypatch.setattr(transport, "_api_client", FakeAPI)
    # Any accidental local discovery/backend selection fails this entire fixture.
    monkeypatch.setattr(bleak, "BleakScanner", lambda *a, **kw: pytest.fail("Remote transport used local scanner"))
    yield FakeAPI
    assert transport._broker_instance is None or not transport._broker().sessions
    assert transport._broker_instance is None or not transport._broker().physical_sessions


def config(*names, gatt_proxy=None):
    return BluetoothConfig("esphome", tuple(ProxyConfig(n, n + ".local", noise_psk=PSK, password=PASSWORD) for n in names), gatt_proxy)


def test_config_repr_errors_and_public_descriptor_exclude_credentials():
    c = config("bedroom")
    assert PSK not in repr(c) and PASSWORD not in repr(c)
    assert c.public_info() == {"backend": "esphome", "proxies": ["bedroom"], "gatt_proxy": None}
    for kwargs in ({"backend": "other"}, {"backend": "esphome"}, {"connect_timeout": float("nan")}, {"connect_timeout": True}):
        with pytest.raises(ValueError):
            BluetoothConfig(**kwargs)
    for kwargs in ({"host": "http://user:secret@device"}, {"noise_psk": PASSWORD}, {"port": True}, {"name": PASSWORD + "/secret"}):
        data = {"name": "room", "host": "room.local", **kwargs}
        with pytest.raises(ValueError) as error:
            ProxyConfig(**data)
        assert PASSWORD not in str(error.value)
    with pytest.raises(ValueError):
        BluetoothConfig("esphome", (ProxyConfig("one", "same.local"), ProxyConfig("two", "same.local.")))
    with pytest.raises(TransportError, match="gatt_proxy"):
        config("one", "two").direct_proxy()


@pytest.mark.parametrize("raw", [False, True])
def test_remote_capture_decrypts_all_four_models_without_local_adapter(tmp_path, make_record, remote_api, monkeypatch, raw):
    if raw:
        from aioesphomeapi import BluetoothProxyFeature
        remote_api.feature_flags |= BluetoothProxyFeature.RAW_ADVERTISEMENTS
    definitions = [("ES5BB", 0x484E, b"\x01"), ("MS1BB", 0x4A12, b"\x01"),
                   ("KS2BB", 0x560C, b"\x01"), ("HS3BB", 0x4C03, b"\x64")]
    devices = []
    for index, (model, obj, value) in enumerate(definitions, 1):
        address = f"AA:BB:CC:DD:EE:{index:02X}"
        remote_api.pending.append(make_record(model, object_bytes(obj, value), address=address))
        devices.append(DeviceConfig(address, model, model, TEST_KEY))
    async def immediately(_):
        return None
    monkeypatch.setattr(runtime, "_wait_duration", immediately)
    output = tmp_path / "remote.jsonl"
    started = []
    stats = asyncio.run(runtime.capture(mode="monitor", seconds=1, output=output, devices=devices,
        transport=config("room"), on_started=lambda: started.append(True), quiet=True))
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4 and {r["model"] for r in rows} == {d[0] for d in definitions}
    assert all(r["status"] == "decoded" and r["bindkey_verified"] for r in rows)
    assert stats["written"] == 4 and started == [True]
    assert TEST_KEY not in output.read_text(encoding="utf-8")
    assert all(c.closed for c in remote_api.instances)


def test_proxy_failures_do_not_claim_started_or_expose_credentials(tmp_path, remote_api):
    remote_api.fail_hosts.add("room.local")
    started = []
    with pytest.raises(TransportError) as error:
        asyncio.run(runtime.capture(mode="scan", seconds=1, output=tmp_path / "failure.jsonl",
            transport=config("room"), on_started=lambda: started.append(True), quiet=True))
    assert not started and all(c.closed for c in remote_api.instances)
    assert PSK not in str(error.value) and PASSWORD not in str(error.value)


def test_proxy_drop_health_partial_operation_reconnect_and_cleanup(remote_api):
    async def run():
        async with scanner_context(config("one", "two"), lambda *_: None) as scanner:
            assert scanner.health()["connected"]
            first, second = remote_api.instances
            await first.drop()
            scanner.check_health()  # The second room is still available.
            assert not scanner.state["one"]["connected"]
            # Construction precedes connect, scanner-state acknowledgement and
            # publication of readiness on the broker thread. Wait for the
            # actual replacement subscription, within the original 2.5s bound.
            async with asyncio.timeout(2.5):
                while (len(remote_api.instances) < 3 or not scanner.state["one"]["connected"]
                       or not scanner.state["one"]["subscription_confirmed"]):
                    await asyncio.sleep(.025)
            assert len(remote_api.instances) == 3
            assert scanner.state["one"]["connected"]
            assert remote_api.instances[-1].connected
            assert remote_api.instances[-1].ad_subscriptions == 1
            await remote_api.instances[-1].drop()
            await second.drop()
            with pytest.raises(TransportError):
                scanner.check_health()
        assert all(c.closed for c in remote_api.instances)
        assert all(task.done() for task in scanner.tasks)
    asyncio.run(run())


def test_real_esphome_backend_full_es5_auth_read_write_while_ads_continue(remote_api, make_record, monkeypatch):
    from bleak_esphome.backend.client import ESPHomeClient
    from linptech_ble.standard_auth import StandardSession
    from test_standard_auth import APP_RANDOM, TOKEN
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    async def run():
        received = []
        async with scanner_context(config("room"), lambda device, adv: received.append((device, adv))):
            ad_api = remote_api.instances[0]
            async with connected_client(config("room"), "AA:BB:CC:DD:EE:01") as client:
                assert isinstance(client.client._backend, ESPHomeClient)
                assert len(remote_api.instances) == 1
                gatt_api = ad_api
                assert ad_api.ad_subscriptions == 1
                assert gatt_api.slot_subscriptions == 1
                caller_loop = asyncio.get_running_loop()
                notification_loops = []
                session = StandardSession(client, TOKEN,
                    on_property=lambda _: notification_loops.append(asyncio.get_running_loop()))
                try:
                    await session.authenticate()
                    assert (await session.read(6, 11))["value"] == 1
                    assert (await session.set(6, 11, 2, "uint8"))["value"] == 2
                    assert (await session.read(6, 4))["value"] == "[5,9]"
                    ad_api.emit(make_record("MS1BB", object_bytes(0x4A12, b"\x01")))
                    await asyncio.sleep(.03)
                    assert received[-1][0].address == "AA:BB:CC:DD:EE:01"
                    assert client.is_connected and ad_api.connected
                    assert notification_loops and all(loop is caller_loop for loop in notification_loops)
                finally:
                    await session.close()
                assert not gatt_api.peer.callbacks
            assert not gatt_api.closed and gatt_api.disconnect_count >= 1
            assert ad_api.connected  # GATT cleanup never disconnects the scanner.
        assert ad_api.closed
    asyncio.run(run())


def test_initial_nonrunning_state_is_not_ready_and_transient_pause_recovers(tmp_path, remote_api):
    from aioesphomeapi import BluetoothScannerState, BluetoothScannerStateResponse
    remote_api.scanner_state = BluetoothScannerState.IDLE
    with pytest.raises(TransportError):
        asyncio.run(runtime.capture(mode="scan", seconds=1, output=tmp_path / "idle.jsonl",
            transport=replace(config("room"), connect_timeout=1), quiet=True))
    remote_api.scanner_state = BluetoothScannerState.RUNNING
    async def run():
        async with scanner_context(config("room"), lambda *_: None) as scanner:
            api = remote_api.instances[-1]
            async def pause_then_run():
                api.state_callback(BluetoothScannerStateResponse(state=BluetoothScannerState.STOPPING))
                api.state_callback(BluetoothScannerStateResponse(state=BluetoothScannerState.STARTING))
                api.state_callback(BluetoothScannerStateResponse(state=BluetoothScannerState.RUNNING))
            await transport._broker().call(pause_then_run())
            scanner.check_health()
            async def fail():
                api.state_callback(BluetoothScannerStateResponse(state=BluetoothScannerState.FAILED))
            await transport._broker().call(fail())
            with pytest.raises(TransportError):
                scanner.check_health()
    asyncio.run(run())


def test_malformed_proxy_advertisement_is_dropped_without_callback_error(remote_api, make_record):
    async def run():
        received = []
        async with scanner_context(config("room"), lambda *args: received.append(args)) as scanner:
            api = remote_api.instances[-1]
            async def malformed():
                api.ad_callback(SimpleNamespace(address=-1, rssi=-30))
            await transport._broker().call(malformed())
            await asyncio.sleep(.01)
            assert received == []
            assert scanner.health()["proxies"][0]["malformed_advertisements"] == 1
            api.emit(make_record("ES5BB", object_bytes(0x484E, b"\x01")))
            await asyncio.sleep(.01)
            assert len(received) == 1
            scanner.check_health()
    asyncio.run(run())


def test_two_capture_handles_share_one_owner_and_release_independently(remote_api, make_record):
    async def run():
        first, second = [], []
        async with scanner_context(config("room"), lambda *args: first.append(args)):
            async with scanner_context(config("room"), lambda *args: second.append(args)):
                assert len(remote_api.instances) == 1
                api = remote_api.instances[0]
                api.emit(make_record("MS1BB", object_bytes(0x4A12, b"\x01")))
                await asyncio.sleep(.02)
                assert len(first) == len(second) == 1
            assert not api.closed
            api.emit(make_record("MS1BB", object_bytes(0x4A12, b"\x00"), counter=2))
            await asyncio.sleep(.02)
            assert len(first) == 2 and len(second) == 1
        assert api.closed and api.ad_subscriptions == 1
    asyncio.run(run())


def test_threaded_direct_controller_and_capture_share_api_and_keep_crypto_on_controller_loop(tmp_path, remote_api, monkeypatch):
    from linptech_ble.es5_worker import ProcessDirectController
    from test_standard_auth import TOKEN, APP_RANDOM
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    loops = []
    c = ProcessDirectController(DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Synthetic", TEST_KEY, TOKEN.hex()),
        transport=config("room"), recording_dir=tmp_path,
        on_value=lambda _: loops.append(asyncio.get_running_loop()))
    async def refresh():
        # Limit the fake peer's property catalog; exercise the real auth/read,
        # verified-write, _accept, command queue and threaded controller paths.
        c._accept(await c.client.read("sensitivity_level"))
        c._update(status="ready")
    async def start_reporting():
        pass
    c._refresh, c._start_reporting = refresh, start_reporting
    async def wait_operation():
        async with asyncio.timeout(5):
            while c.snapshot()["last_operation"]["status"] == "pending":
                await asyncio.sleep(.01)
        assert c.snapshot()["last_operation"]["status"] == "done"
    async def run():
        async with scanner_context(config("room"), lambda *_: None):
            c.start()
            try:
                c.submit({"action": "connect"})
                await wait_operation()
                c.submit({"action": "set", "property": "sensitivity_level", "value": 2})
                await wait_operation()
                assert c.snapshot()["properties"]["sensitivity_level"]["value"] == 2
                assert loops and all(loop is c.loop for loop in loops)
                assert c.loop is not transport._broker().loop
                assert len(remote_api.instances) == 1 and c.worker is None
            finally:
                c.stop()
            assert not c.thread.is_alive() and not remote_api.instances[0].closed
        assert remote_api.instances[0].closed
    asyncio.run(run())


def test_advertisement_only_proxy_fails_gatt_without_fallback(remote_api):
    from aioesphomeapi import BluetoothProxyFeature
    remote_api.feature_flags = BluetoothProxyFeature.PASSIVE_SCAN
    async def run():
        async with scanner_context(config("room"), lambda *_: None) as scanner:
            scanner.check_health()
        with pytest.raises(TransportError, match="active: true"):
            async with connected_client(config("room"), "AA:BB:CC:DD:EE:01"):
                pytest.fail("Scan-only proxy established GATT")
        assert all(c.closed for c in remote_api.instances)
    asyncio.run(run())


def test_gatt_explicit_selection_and_link_drop_invalidate_client(remote_api):
    async def run():
        async with connected_client(config("one", "two", gatt_proxy="two"), "AA:BB:CC:DD:EE:01") as client:
            api = remote_api.instances[-1]
            assert api.config.name == "two" and client.is_connected
            await api.drop()
            assert not client.is_connected
        # A deliberate reconnect creates a fresh API/backend on the same loop.
        async with connected_client(config("two"), "AA:BB:CC:DD:EE:01") as client:
            assert client.is_connected
        assert len(remote_api.instances) == 2 and all(c.closed for c in remote_api.instances)
    asyncio.run(run())


def test_unconfirmed_advert_subscription_never_signals_ready(tmp_path, remote_api):
    remote_api.confirm_subscription = False
    started = []
    with pytest.raises(TransportError):
        asyncio.run(runtime.capture(mode="scan", seconds=1, output=tmp_path / "no-subscriber.jsonl",
            transport=replace(config("room"), connect_timeout=1), quiet=True,
            on_started=lambda: started.append(True)))
    assert not started and all(c.closed for c in remote_api.instances)


def test_non_proxy_esphome_device_is_rejected(remote_api):
    remote_api.feature_flags = 0
    async def run():
        with pytest.raises(TransportError):
            async with scanner_context(config("room"), lambda *_: None):
                pytest.fail("ESPHome device with no Bluetooth feature was accepted")
        assert all(c.closed for c in remote_api.instances)
    asyncio.run(run())


def test_runtime_exits_on_all_proxy_loss_so_supervisor_can_retry(tmp_path, remote_api):
    async def run():
        def on_started():
            asyncio.create_task(remote_api.instances[0].drop())
        with pytest.raises(TransportError, match="Proxy"):
            await asyncio.wait_for(runtime.capture(mode="scan", seconds=0,
                output=tmp_path / "lost.jsonl", transport=config("room"), quiet=True,
                on_started=on_started), timeout=3)
        assert all(c.closed for c in remote_api.instances)
    asyncio.run(run())


def test_missing_optional_dependency_is_sanitized_and_never_falls_back(monkeypatch):
    import builtins
    original = builtins.__import__
    def without_remote(name, *args, **kwargs):
        if name == "aioesphomeapi":
            raise ImportError("synthetic missing remote package")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_remote)
    monkeypatch.setattr(bleak, "BleakScanner", lambda *a, **kw: pytest.fail("Fallback used local BLE"))
    async def run():
        with pytest.raises(TransportError, match=r"\[proxy\]"):
            async with scanner_context(config("room"), lambda *_: None):
                pytest.fail("Missing dependency accepted")
    asyncio.run(run())


def test_per_address_gatt_routes_are_immutable_normalized_and_override_default():
    import pickle
    routes = {"aa:bb:cc:dd:ee:01": "two"}
    value = replace(config("one", "two", gatt_proxy="one"), gatt_routes=routes)
    routes["aa:bb:cc:dd:ee:01"] = "one"
    assert value.direct_proxy("AA:BB:CC:DD:EE:01").name == "two"
    assert value.direct_proxy("AA:BB:CC:DD:EE:02").name == "one"
    assert value.gatt_routes == (("AA:BB:CC:DD:EE:01", "two"),)
    restored = pickle.loads(pickle.dumps(value))
    assert restored == value and hash(restored) == hash(value)
    routed_only = replace(config("one", "two"), gatt_routes={"AA:BB:CC:DD:EE:01": "two"})
    assert routed_only.direct_proxy("AA:BB:CC:DD:EE:01").name == "two"
    with pytest.raises(TransportError, match="gatt_routes"):
        routed_only.direct_proxy("AA:BB:CC:DD:EE:02")


@pytest.mark.parametrize("routes", [None, "private-placeholder", {"invalid": "one"},
    {"AA:BB:CC:DD:EE:01": "missing"}, [("AA:BB:CC:DD:EE:01", "one"), ("aa:bb:cc:dd:ee:01", "two")]])
def test_invalid_routes_fail_before_io_without_echoing_source_values(routes):
    with pytest.raises(ValueError) as error:
        replace(config("one", "two"), gatt_routes=routes)
    assert "private-placeholder" not in str(error.value)


def test_canonical_endpoint_identity_reuses_case_and_ipv6_spellings():
    assert ProxyConfig("one", "ROOM.local.") == ProxyConfig("one", "room.local")
    assert ProxyConfig("one", "2001:0db8:0:0:0:0:0:1") == ProxyConfig("one", "2001:db8::1")
    with pytest.raises(ValueError, match="位址"):
        BluetoothConfig("esphome", (ProxyConfig("one", "2001:0db8::1"), ProxyConfig("two", "2001:db8::1")))


def test_healthy_proxy_starts_while_another_initial_connection_is_still_pending(remote_api, make_record):
    remote_api.block_hosts.add("offline.local")

    async def run():
        received = []
        context = scanner_context(replace(config("offline", "room", "other"), connect_timeout=4),
                                  lambda *args: received.append(args))
        # A stalled connect would otherwise delay this whole scanner by 4s.
        async with asyncio.timeout(.8):
            scanner = await context.__aenter__()
        try:
            scanner.check_health()
            assert set(scanner.state) == {"offline", "room", "other"}
            assert not scanner.state["offline"]["connected"]
            api = next(api for api in remote_api.instances if api.config.name == "room")
            api.emit(make_record("MS1BB", object_bytes(0x4A12, b"\x01")))
            async with asyncio.timeout(1):
                while not received:
                    await asyncio.sleep(.005)
            assert received[0][0].details["source"] == "room"
        finally:
            await context.__aexit__(None, None, None)
        assert all(api.closed for api in remote_api.instances)

    asyncio.run(run())


@pytest.mark.parametrize("raw", [False, True])
def test_three_proxies_preserve_all_four_models_overlap_rssi_and_source(tmp_path, make_record, remote_api, monkeypatch, raw):
    from aioesphomeapi import BluetoothProxyFeature
    if raw:
        remote_api.feature_flags |= BluetoothProxyFeature.RAW_ADVERTISEMENTS
    definitions = [("ES5BB", 0x484E, b"\x01"), ("MS1BB", 0x4A12, b"\x01"),
                   ("KS2BB", 0x560C, b"\x01"), ("HS3BB", 0x4C03, b"\x64")]
    records = [make_record(model, object_bytes(obj, value), address=f"AA:BB:CC:DD:EE:{index:02X}")
               for index, (model, obj, value) in enumerate(definitions, 1)]
    strengths = {"living": -48, "bedroom": -72, "hall": -88}
    for name, rssi in strengths.items():
        remote_api.pending_by_host[name + ".local"] = [replace(record, rssi=rssi) for record in records]
    devices = [DeviceConfig(record.address, model, model, TEST_KEY) for record, (model, _, _) in zip(records, definitions)]

    async def settled(_):
        await asyncio.sleep(.03)

    monkeypatch.setattr(runtime, "_wait_duration", settled)
    decoded_path, raw_path = tmp_path / "decoded.jsonl", tmp_path / "raw.jsonl"
    stats = asyncio.run(runtime.capture(mode="monitor", seconds=1, output=decoded_path, raw_output=raw_path,
        devices=devices, transport=config(*strengths), quiet=True))
    decoded = [json.loads(row) for row in decoded_path.read_text(encoding="utf-8").splitlines()]
    raw_rows = [json.loads(row) for row in raw_path.read_text(encoding="utf-8").splitlines()]
    assert stats["received"] == stats["written"] == stats["raw_written"] == 12
    assert len(remote_api.instances) == 3 and all(api.ad_subscriptions == 1 and api.closed for api in remote_api.instances)
    for name, strength in strengths.items():
        selected = [row for row in raw_rows if row["receiver"] == name]
        assert len(selected) == 4 and {row["rssi"] for row in selected} == {strength}
        assert {row["address"] for row in selected} == {record.address for record in records}
    for record in records:
        selected = [row for row in decoded if row["address"] == record.address]
        assert len(selected) == 3
        assert any(row["status"] == "decoded" and row["bindkey_verified"] for row in selected)
        assert {row["receiver"] for row in selected} == set(strengths)
    assert TEST_KEY not in decoded_path.read_text(encoding="utf-8") and TEST_KEY not in raw_path.read_text(encoding="utf-8")


def test_retired_proxy_callbacks_cannot_revoke_or_publish_after_reconnect(remote_api, make_record):
    from aioesphomeapi import BluetoothScannerState, BluetoothScannerStateResponse

    async def run():
        received = []
        async with scanner_context(config("one", "two", "three"), lambda *args: received.append(args)) as scanner:
            first = next(api for api in remote_api.instances if api.config.name == "one")
            stale_advertisement, stale_state, stale_slots, stale_stop = (
                first.ad_callback, first.state_callback, first.slots_callback, first.on_stop)
            record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
            await first.drop()
            scanner.check_health()
            async with asyncio.timeout(3):
                while not scanner.state["one"]["connected"]:
                    await asyncio.sleep(.01)
            replacement = next(api for api in reversed(remote_api.instances) if api.config.name == "one")
            assert replacement is not first

            async def late_callbacks():
                await stale_stop(False)
                stale_state(BluetoothScannerStateResponse(state=BluetoothScannerState.FAILED))
                stale_slots(0, 3, [])
                stale_advertisement(SimpleNamespace(address=int(record.address.replace(":", ""), 16),
                    rssi=-99, address_type=0, name=record.name, service_uuids=record.service_uuids,
                    service_data={key: bytes.fromhex(value) for key, value in record.service_data.items()}, manufacturer_data={}))

            await transport._broker().call(late_callbacks())
            await asyncio.sleep(.02)
            assert scanner.state["one"]["connected"] and scanner.state["one"]["connections_free"] == 3
            assert received == []
            replacement.emit(record)
            async with asyncio.timeout(1):
                while not received:
                    await asyncio.sleep(.005)
            assert received[0][0].details["source"] == "one"
            assert len(remote_api.instances) == 4
        assert all(api.closed for api in remote_api.instances)

    asyncio.run(run())


@pytest.mark.parametrize("change", [{"name": "alias"}, {"password": "different-private-password"},
    {"noise_psk": base64.b64encode(b"different".ljust(32, b"\0")).decode()}])
def test_endpoint_setting_conflict_never_replaces_existing_subscription(remote_api, make_record, change):
    async def run():
        received = []
        original = config("room")
        conflicting = replace(original, proxies=(replace(original.proxies[0], **change),))
        async with scanner_context(original, lambda *args: received.append(args)) as healthy:
            with pytest.raises(TransportError, match="不同設定") as failure:
                async with scanner_context(conflicting, lambda *_: None):
                    pytest.fail("Conflicting proxy was accepted")
            assert "different-private-password" not in str(failure.value)
            healthy.check_health()
            assert len(remote_api.instances) == 1 and remote_api.instances[0].ad_subscriptions == 1
            remote_api.instances[0].emit(make_record("MS1BB", object_bytes(0x4A12, b"\x01")))
            await asyncio.sleep(.02)
            assert len(received) == 1

    asyncio.run(run())


def test_three_routed_gatt_connections_use_independent_apis_and_keep_scanning(remote_api, monkeypatch, make_record):
    from contextlib import AsyncExitStack
    from linptech_ble.standard_auth import StandardSession
    from test_standard_auth import TOKEN, APP_RANDOM
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    addresses = [f"AA:BB:CC:DD:EE:{index:02X}" for index in range(1, 4)]
    routes = dict(zip(addresses, ("living", "bedroom", "hall")))
    settings = replace(config("living", "bedroom", "hall"), gatt_routes=routes)

    async def run():
        received = []
        async with scanner_context(settings, lambda *args: received.append(args)) as scanner:
            async with AsyncExitStack() as stack:
                clients = await asyncio.gather(*(stack.enter_async_context(connected_client(settings, address)) for address in addresses))
                sessions = [StandardSession(client, TOKEN) for client in clients]
                try:
                    await asyncio.gather(*(session.authenticate() for session in sessions))
                    assert [result["value"] for result in await asyncio.gather(*(session.read(6, 11) for session in sessions))] == [1, 1, 1]
                    assert len(remote_api.instances) == 3
                    for api in remote_api.instances:
                        expected = int(next(address for address, proxy in routes.items() if proxy == api.config.name).replace(":", ""), 16)
                        assert api.connect_calls == [expected] and api.ad_subscriptions == 1
                        api.emit(make_record("MS1BB", object_bytes(0x4A12, b"\x01")))
                    await asyncio.sleep(.02)
                    assert len(received) == 3
                    scanner.check_health()
                finally:
                    await asyncio.gather(*(session.close() for session in sessions))
            assert all(api.connected and api.disconnect_count == 1 for api in remote_api.instances)
        assert all(api.closed for api in remote_api.instances)

    asyncio.run(run())


def test_three_devices_share_one_api_with_independent_authenticated_sessions(remote_api, monkeypatch):
    from contextlib import AsyncExitStack
    from linptech_ble.standard_auth import StandardSession
    from test_standard_auth import TOKEN, APP_RANDOM
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)

    async def run():
        async with scanner_context(config("room"), lambda *_: None) as scanner:
            async with AsyncExitStack() as stack:
                clients = await asyncio.gather(*(stack.enter_async_context(connected_client(config("room"), f"AA:BB:CC:DD:EE:{index:02X}"))
                                                for index in range(1, 4)))
                sessions = [StandardSession(client, TOKEN) for client in clients]
                try:
                    await asyncio.gather(*(session.authenticate() for session in sessions))
                    await asyncio.gather(*(session.set(6, 11, index, "uint8") for index, session in enumerate(sessions, 1)))
                    assert [result["value"] for result in await asyncio.gather(*(session.read(6, 11) for session in sessions))] == [1, 2, 3]
                    api = remote_api.instances[0]
                    assert len(api.peers) == 3 and api.ad_subscriptions == api.slot_subscriptions == 1
                    assert scanner.health()["proxies"][0]["gatt_reserved"] == 3
                finally:
                    await asyncio.gather(*(session.close() for session in sessions))
            scanner.check_health()
            assert scanner.health()["proxies"][0]["gatt_reserved"] == 0

    asyncio.run(run())


def test_pending_slot_reservation_prevents_overcommit_before_firmware_update(remote_api):
    remote_api.connection_limit = 1
    remote_api.report_connect_slots = False

    async def run():
        async with scanner_context(config("room"), lambda *_: None):
            first = connected_client(config("room"), "AA:BB:CC:DD:EE:01")
            second = connected_client(config("room"), "AA:BB:CC:DD:EE:02")
            await first.__aenter__()
            opening = asyncio.create_task(second.__aenter__())
            try:
                await asyncio.sleep(.04)
                api = remote_api.instances[0]
                assert len(api.connect_calls) == 1 and not opening.done()
                await first.__aexit__(None, None, None)
                client = await asyncio.wait_for(opening, 2)
                assert client.is_connected and len(api.connect_calls) == 2
                await second.__aexit__(None, None, None)
            finally:
                if not opening.done():
                    opening.cancel()
                    await asyncio.gather(opening, return_exceptions=True)
                await first.__aexit__(None, None, None)

    asyncio.run(run())


def test_cancelled_slot_waiter_releases_ownership_and_keeps_other_client_alive(remote_api):
    remote_api.connection_limit = 1

    async def run():
        async with scanner_context(config("room"), lambda *_: None) as scanner:
            async with connected_client(config("room"), "AA:BB:CC:DD:EE:01") as first:
                blocked = connected_client(config("room"), "AA:BB:CC:DD:EE:02")
                task = asyncio.create_task(blocked.__aenter__())
                await asyncio.sleep(.04)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert first.is_connected
                scanner.check_health()
                assert scanner.health()["proxies"][0]["gatt_reserved"] == 1
                assert len(remote_api.instances[0].connect_calls) == 1
            async with connected_client(config("room"), "AA:BB:CC:DD:EE:03") as third:
                assert third.is_connected
            assert len(remote_api.instances) == 1

    asyncio.run(run())


def test_remote_async_callback_runs_on_caller_loop_and_failure_is_subscriber_local(remote_api, make_record):
    async def run():
        received, loops = [], []
        caller = asyncio.get_running_loop()

        async def callback(device, advertisement):
            await asyncio.sleep(0)
            received.append(device.address)
            loops.append(asyncio.get_running_loop())

        def broken(*_):
            raise RuntimeError("private-callback-detail")

        async with scanner_context(config("room"), callback) as healthy:
            async with scanner_context(config("room"), broken) as failed:
                remote_api.instances[0].emit(make_record("KS2BB", object_bytes(0x560C, b"\x01")))
                async with asyncio.timeout(1):
                    while not received or not failed.callback_error:
                        await asyncio.sleep(.005)
                assert received and loops == [caller]
                healthy.check_health()
                with pytest.raises(TransportError) as error:
                    failed.check_health()
                assert "private-callback-detail" not in str(error.value)
                assert len(remote_api.instances) == 1

    asyncio.run(run())


def test_unknown_owner_release_never_closes_other_scanner_handles(remote_api):
    async def run():
        settings = config("room")
        async with scanner_context(settings, lambda *_: None) as first:
            async with scanner_context(settings, lambda *_: None) as second:
                broker = transport._broker()
                await broker.call(broker.release(settings.proxies[0], object()))
                first.check_health()
                second.check_health()
                assert not remote_api.instances[0].closed
                assert await broker.call(_references(broker, settings.proxies[0])) == 2

    async def _references(broker, proxy):
        return broker.sessions[proxy].refs

    asyncio.run(run())


def test_closing_one_subscriber_drops_its_already_queued_delivery(remote_api, make_record):
    async def run():
        first_rows, second_rows = [], []
        first_context = scanner_context(config("room"), lambda *args: first_rows.append(args))
        first = await first_context.__aenter__()
        try:
            async with scanner_context(config("room"), lambda *args: second_rows.append(args)):
                broker = transport._broker()

                async def retained_callback():
                    return broker.sessions[config("room").proxies[0]].callbacks[first.token]

                callback = await broker.call(retained_callback())
                await first_context.__aexit__(None, None, None)
                record = make_record("MS1BB", object_bytes(0x4A12, b"\x01"))

                async def late_delivery():
                    callback(None, None)

                await broker.call(late_delivery())
                remote_api.instances[0].emit(record)
                await asyncio.sleep(.03)
                assert first_rows == [] and len(second_rows) == 1
        finally:
            await first_context.__aexit__(None, None, None)

    asyncio.run(run())


def test_mixed_raw_and_decoded_proxy_firmware_share_one_capture(remote_api, make_record):
    from aioesphomeapi import BluetoothProxyFeature
    remote_api.feature_by_host["raw.local"] = remote_api.feature_flags | BluetoothProxyFeature.RAW_ADVERTISEMENTS
    remote_api.feature_by_host["legacy.local"] = BluetoothProxyFeature.PASSIVE_SCAN

    async def run():
        received = []
        async with scanner_context(config("raw", "decoded", "legacy"), lambda device, adv: received.append((device, adv))) as scanner:
            for api in remote_api.instances:
                api.emit(make_record("ES5BB", object_bytes(0x484E, b"\x01")))
            async with asyncio.timeout(1):
                while len(received) != 3:
                    await asyncio.sleep(.005)
            assert {device.details["source"] for device, _ in received} == {"raw", "decoded", "legacy"}
            assert len({next(iter(adv.service_data.values())) for _, adv in received}) == 1
            assert scanner.state["raw"]["subscription_confirmed"]
            assert scanner.state["decoded"]["subscription_confirmed"]
            assert not scanner.state["legacy"]["subscription_confirmed"]
            scanner.check_health()

    asyncio.run(run())


def test_slow_async_callback_backlog_is_bounded_without_stopping_another_subscriber(remote_api, make_record):
    async def run():
        other = []

        async def slow(*_):
            await asyncio.Event().wait()

        async with scanner_context(config("room"), slow) as overloaded:
            async with scanner_context(config("room"), lambda *args: other.append(args)) as healthy:
                record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))

                async def burst():
                    for _ in range(150):
                        remote_api.instances[0].emit(record)

                await transport._broker().call(burst())
                async with asyncio.timeout(1):
                    while len(other) != 150:
                        await asyncio.sleep(.005)
                assert len(overloaded.callback_tasks) == 128
                with pytest.raises(TransportError, match="處理速度"):
                    overloaded.check_health()
                healthy.check_health()
        assert not overloaded.callback_tasks

    asyncio.run(run())


def test_physical_proxy_ip_and_hostname_aliases_never_steal_subscription(remote_api, make_record):
    physical = {"bluetooth_mac_address": "02:11:22:33:44:55", "mac_address": "02:11:22:33:44:54"}
    remote_api.physical_by_host.update({"192.0.2.10": physical, "room.local": physical})
    settings = BluetoothConfig("esphome", (ProxyConfig("ip", "192.0.2.10"), ProxyConfig("dns", "room.local")))

    async def run():
        received = []
        async with scanner_context(settings, lambda *args: received.append(args)) as scanner:
            async with asyncio.timeout(1):
                while not scanner.state["dns"]["error"]:
                    await asyncio.sleep(.005)
            scanner.check_health()
            assert scanner.state["ip"]["connected"]
            assert not scanner.state["dns"]["connected"]
            assert "同一實體" in scanner.state["dns"]["error"]
            owner = next(instance for instance in remote_api.instances if instance.config.name == "ip")
            rejected = next(instance for instance in remote_api.instances if instance.config.name == "dns")
            assert owner.ad_subscriptions == 1
            assert rejected.ad_subscriptions == rejected.slot_subscriptions == 0
            owner.emit(make_record("MS1BB", object_bytes(0x4A12, b"\x01")))
            async with asyncio.timeout(1):
                while not received:
                    await asyncio.sleep(.005)
            assert received[0][0].details["source"] == "ip"
        assert not transport._broker().physical_sessions

    asyncio.run(run())


def test_physical_alias_is_reusable_after_owner_cleanup_and_old_callbacks_are_harmless(remote_api, make_record):
    from aioesphomeapi import BluetoothScannerState, BluetoothScannerStateResponse
    physical = {"bluetooth_mac_address": "02:11:22:33:44:55", "mac_address": "02:11:22:33:44:54"}
    remote_api.physical_by_host.update({"192.0.2.10": physical, "room.local": physical})
    first_settings = BluetoothConfig("esphome", (ProxyConfig("ip", "192.0.2.10"),))
    second_settings = BluetoothConfig("esphome", (ProxyConfig("dns", "room.local"),))

    async def run():
        async with scanner_context(first_settings, lambda *_: None):
            old = remote_api.instances[-1]
            stale_state, stale_stop = old.state_callback, old.on_stop
        assert old.closed and not transport._broker().physical_sessions
        received = []
        async with scanner_context(second_settings, lambda *args: received.append(args)) as scanner:
            current = remote_api.instances[-1]

            async def delayed_old_callbacks():
                await stale_stop(False)
                stale_state(BluetoothScannerStateResponse(state=BluetoothScannerState.FAILED))

            await transport._broker().call(delayed_old_callbacks())
            scanner.check_health()
            assert current.ad_subscriptions == 1 and scanner.state["dns"]["connected"]
            current.emit(make_record("MS1BB", object_bytes(0x4A12, b"\x01")))
            async with asyncio.timeout(1):
                while not received:
                    await asyncio.sleep(.005)
            assert received[0][0].details["source"] == "dns"
        assert not transport._broker().physical_sessions

    asyncio.run(run())


@pytest.mark.parametrize("bluetooth_id", ["", "invalid-private-device-id", "00:00:00:00:00:00"])
def test_physical_identity_uses_valid_station_mac_fallback(remote_api, bluetooth_id):
    remote_api.physical_by_host["room.local"] = {"bluetooth_mac_address": bluetooth_id, "mac_address": "02:11:22:33:44:54"}

    async def run():
        async with scanner_context(config("room"), lambda *_: None) as scanner:
            scanner.check_health()
            assert set(transport._broker().physical_sessions) == {"02:11:22:33:44:54"}
            assert scanner.sessions[0].data.bluetooth_device.mac_address == "02:11:22:33:44:54"

    asyncio.run(run())


def test_missing_physical_identifiers_fail_without_empty_identity_group_or_subscription(remote_api):
    remote_api.physical_by_host["room.local"] = {"bluetooth_mac_address": "", "mac_address": "invalid-private-device-id"}

    async def run():
        with pytest.raises(TransportError):
            async with scanner_context(config("room"), lambda *_: None):
                pytest.fail("Missing physical identity accepted")
        assert not transport._broker().physical_sessions
        assert remote_api.instances[0].ad_subscriptions == 0
        assert remote_api.instances[0].slot_subscriptions == 0

    asyncio.run(run())


def test_cleanup_releases_physical_claim_even_if_session_task_already_ended(remote_api):
    async def run():
        context = scanner_context(config("room"), lambda *_: None)
        scanner = await context.__aenter__()
        try:
            async def end_session():
                session = scanner.sessions[0]
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            await transport._broker().call(end_session())
        finally:
            await context.__aexit__(None, None, None)
        assert not transport._broker().physical_sessions
        async with scanner_context(config("room"), lambda *_: None) as replacement:
            replacement.check_health()

    asyncio.run(run())
