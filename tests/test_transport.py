"""Remote Native API protocol tests; no ESP32, local radio or real secrets."""
from __future__ import annotations

import asyncio
import base64
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
            self.instances.append(self)

        async def connect(self, on_stop, **kwargs):
            self.on_stop = on_stop
            if self.config.host in self.fail_hosts:
                raise OSError(f"sensitive {PSK} {PASSWORD}")
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
            return api.DeviceInfo(name="test-proxy", mac_address="00:11:22:33:44:55",
                bluetooth_mac_address="00:11:22:33:44:56", bluetooth_proxy_feature_flags=int(self.feature_flags))

        def subscribe_bluetooth_scanner_state(self, callback):
            self.state_callback = callback
            return lambda: setattr(self, "state_callback", None)

        def subscribe_bluetooth_le_advertisements(self, callback):
            self.owners[self.config.host] = self
            self.ad_subscriptions += 1
            self.ad_callback = callback
            for record in self.pending:
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
            if self.feature_flags & api.BluetoothProxyFeature.RAW_ADVERTISEMENTS:
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
            callback(3, 3, [])
            return lambda: setattr(self, "slots_callback", None)

        async def bluetooth_device_connect(self, address, callback, **kwargs):
            assert asyncio.get_running_loop() is self.loop
            # Real firmware routes all BLE replies to its advertisement owner.
            assert self.owners[self.config.host] is self
            self.ble_callback = callback
            callback(True, 23, 0)
            self.slots_callback(2, 3, [address])
            return lambda: None

        async def bluetooth_device_disconnect(self, address):
            self.disconnect_count += 1
            self.ble_callback(False, 23, 0)
            self.slots_callback(3, 3, [])

        def bluetooth_device_disconnect_no_wait(self, address):
            self.ble_callback(False, 23, 0)

        async def bluetooth_gatt_get_services(self, address):
            assert self.owners[self.config.host] is self
            return SimpleNamespace(services=[SimpleNamespace(handle=1, uuid=uuid16(0xFE95), characteristics=[
                SimpleNamespace(handle=handle, uuid=uuid, properties=0x16, descriptors=[])
                for handle, uuid in self.handles.items()])])

        async def bluetooth_gatt_write(self, address, handle, value, response):
            assert asyncio.get_running_loop() is self.loop
            assert self.owners[self.config.host] is self
            await self.peer.write_gatt_char(self.handles[handle], value, response=response)

        async def bluetooth_gatt_start_notify(self, address, handle, callback, timeout):
            assert asyncio.get_running_loop() is self.loop
            assert self.owners[self.config.host] is self
            uuid = self.handles[handle]
            await self.peer.start_notify(uuid, lambda _, data: callback(handle, data))
            async def stop():
                await self.peer.stop_notify(uuid)
            def abort():
                self.peer.callbacks.pop(uuid, None)
            return stop, abort

    monkeypatch.setattr(transport, "_api_client", FakeAPI)
    # Any accidental local discovery/backend selection fails this entire fixture.
    monkeypatch.setattr(bleak, "BleakScanner", lambda *a, **kw: pytest.fail("Remote transport used local scanner"))
    yield FakeAPI
    assert transport._broker_instance is None or not transport._broker().sessions


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
            # Wake a deterministic fast reconnect rather than wait for a timer.
            for _ in range(100):
                if len(remote_api.instances) > 2:
                    break
                await asyncio.sleep(.025)
            assert len(remote_api.instances) == 3
            assert scanner.state["one"]["connected"]
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
