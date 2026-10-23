"""Windows cold-cache discovery recovery; no native BLE calls or credentials."""
from __future__ import annotations

import asyncio
from collections import namedtuple
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
from types import SimpleNamespace

from bleak.backends.device import BLEDevice
from bleak.exc import BleakDeviceNotFoundError
import pytest

from linptech_ble import models, runtime, transport
from linptech_ble.config import DeviceConfig
from linptech_ble.decoder import Decoder
from linptech_ble.standard_auth import DirectError
from linptech_ble.transport import BluetoothConfig, ProxyConfig, TransportError, connected_client, scanner_context
from conftest import TEST_KEY, object_bytes


ADDRESS = "AA:BB:CC:DD:EE:FF"
RawAdvData = namedtuple("RawAdvData", ("adv", "scan"), module="bleak.backends.winrt.scanner")


@pytest.fixture
def local_api(monkeypatch):
    import bleak

    state = SimpleNamespace(instances=[], events=[], outcomes=[], discovery="found",
        found=BLEDevice(ADDRESS, "Discovered device", {"native": "synthetic-advertisement"}),
        connect_started=None, discovery_started=None)

    class Client:
        def __init__(self, target, **kwargs):
            self.target, self.kwargs = target, kwargs
            self.number, self.is_connected, self.disconnects = len(state.instances), False, 0
            state.instances.append(self)

        async def connect(self):
            state.events.append(("connect", self.number))
            outcome = state.outcomes.pop(0) if state.outcomes else None
            if outcome == "wait":
                state.connect_started.set()
                await asyncio.Event().wait()
            if isinstance(outcome, BaseException):
                raise outcome
            self.is_connected = True

        async def disconnect(self):
            state.events.append(("disconnect", self.number))
            self.disconnects += 1
            self.is_connected = False

    class Scanner:
        @staticmethod
        async def find_device_by_address(address, *, timeout):
            assert address == ADDRESS and 0 < timeout <= 10
            state.events.append(("discovery_start", timeout))
            try:
                if state.discovery == "wait":
                    state.discovery_started.set()
                    await asyncio.Event().wait()
                return state.found if state.discovery == "found" else None
            finally:
                state.events.append(("discovery_stop", timeout))

    monkeypatch.setattr(bleak, "BleakClient", Client)
    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    monkeypatch.setattr(transport.sys, "platform", "win32")
    return state


def missing():
    return BleakDeviceNotFoundError(ADDRESS, "Synthetic system cache miss")


def test_warm_windows_cache_preserves_fast_path_without_discovery(local_api):
    async def run():
        async with connected_client(None, ADDRESS, "Test") as client:
            assert client.is_connected and client.target.details is None
            assert client.kwargs["winrt"] == {"use_cached_services": False}
        assert local_api.events == [("connect", 0), ("disconnect", 0)]
    asyncio.run(run())


def test_cold_windows_cache_disconnects_before_discovery_and_retries_once(local_api):
    local_api.outcomes = [missing(), None]
    async def run():
        async with connected_client(None, ADDRESS) as client:
            assert client is local_api.instances[1] and client.target is local_api.found
            assert client.is_connected
        assert local_api.events == [("connect", 0), ("disconnect", 0),
            ("discovery_start", 10.0), ("discovery_stop", 10.0),
            ("connect", 1), ("disconnect", 1)]
        assert all(client.disconnects == 1 for client in local_api.instances)
    asyncio.run(run())


def test_cache_miss_with_no_advertisement_does_not_make_second_client(local_api):
    local_api.outcomes, local_api.discovery = [missing()], "absent"
    async def run():
        with pytest.raises(TransportError, match="未找到"):
            async with connected_client(None, ADDRESS):
                pytest.fail("An absent device cannot be ready")
        assert len(local_api.instances) == 1 and local_api.instances[0].disconnects == 1
        assert [event[0] for event in local_api.events].count("discovery_start") == 1
    asyncio.run(run())


def test_second_not_found_is_not_retried_and_both_clients_cleaned(local_api):
    local_api.outcomes = [missing(), missing()]
    async def run():
        with pytest.raises(BleakDeviceNotFoundError):
            async with connected_client(None, ADDRESS):
                pytest.fail("Second failure cannot be ready")
        assert len(local_api.instances) == 2
        assert all(client.disconnects == 1 for client in local_api.instances)
        assert [event[0] for event in local_api.events].count("discovery_start") == 1
    asyncio.run(run())


@pytest.mark.parametrize("error", [TimeoutError("Synthetic connect timeout"), DirectError("Synthetic authentication failure")])
def test_timeout_or_authentication_error_never_triggers_discovery(local_api, error):
    local_api.outcomes = [error]
    async def run():
        with pytest.raises(type(error)):
            async with connected_client(None, ADDRESS):
                pytest.fail("A failed connection cannot be ready")
        assert local_api.events == [("connect", 0), ("disconnect", 0)]
    asyncio.run(run())


def test_body_not_found_error_is_not_mistaken_for_connect_cache_miss(local_api):
    async def run():
        with pytest.raises(BleakDeviceNotFoundError):
            async with connected_client(None, ADDRESS):
                raise missing()
        assert local_api.events == [("connect", 0), ("disconnect", 0)]
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["initial_connect", "discovery", "retry_connect"])
def test_cancellation_cleans_each_created_client_and_stops_watcher(local_api, phase):
    async def run():
        local_api.connect_started, local_api.discovery_started = asyncio.Event(), asyncio.Event()
        if phase == "initial_connect":
            local_api.outcomes = ["wait"]
        else:
            local_api.outcomes = [missing(), "wait"]
        if phase == "discovery":
            local_api.discovery = "wait"
        async def opening():
            async with connected_client(None, ADDRESS):
                pytest.fail("Cancelled opening cannot yield a client")
        task = asyncio.create_task(opening())
        await asyncio.wait_for((local_api.discovery_started if phase == "discovery"
                               else local_api.connect_started).wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(local_api.instances) == (2 if phase == "retry_connect" else 1)
        assert all(client.disconnects == 1 for client in local_api.instances)
        if phase != "initial_connect":
            assert [event[0] for event in local_api.events].count("discovery_stop") == 1
    asyncio.run(run())


def test_discovery_is_bounded_even_when_scanner_ignores_its_timeout(local_api):
    async def run():
        local_api.outcomes, local_api.discovery = [missing()], "wait"
        local_api.discovery_started = asyncio.Event()
        with pytest.raises(TimeoutError):
            async with connected_client(BluetoothConfig(connect_timeout=1), ADDRESS):
                pytest.fail("Timed out discovery cannot yield a client")
        assert local_api.events == [("connect", 0), ("disconnect", 0),
                                    ("discovery_start", 1), ("discovery_stop", 1)]
        assert local_api.instances[0].disconnects == 1
    asyncio.run(run())


def test_non_windows_existing_discovery_path_is_not_retried(local_api, monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "linux")
    local_api.outcomes = [missing()]
    async def run():
        with pytest.raises(BleakDeviceNotFoundError):
            async with connected_client(None, ADDRESS):
                pytest.fail("A failed connection cannot be ready")
        assert len(local_api.instances) == 1 and local_api.instances[0].target is local_api.found
        assert local_api.events == [("discovery_start", 10), ("discovery_stop", 10),
                                    ("connect", 0), ("disconnect", 0)]
    asyncio.run(run())


def native_advertisement(*, rssi=-127, raw_rssi=-127, scan=False, metadata=True):
    raw_event = SimpleNamespace(raw_signal_strength_in_dbm=raw_rssi)
    raw = RawAdvData(None if scan else raw_event, raw_event if scan else None)
    return SimpleNamespace(rssi=rssi, platform_data=(object(), raw) if metadata else ())


def install_scanner(monkeypatch, packets, *, before=None):
    """Deliver synthetic scanner callbacks, preserving async callback behavior."""
    import bleak
    state = SimpleNamespace(started=0, stopped=0)
    class Scanner:
        def __init__(self, *, detection_callback):
            self.callback = detection_callback
        async def __aenter__(self):
            state.started += 1
            for index, advertisement in enumerate(packets):
                if before:
                    before(index)
                result = self.callback(SimpleNamespace(address=ADDRESS, name="Synthetic"), advertisement)
                if inspect.isawaitable(result):
                    await result
            return self
        async def __aexit__(self, *_):
            state.stopped += 1
    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    return state


@pytest.mark.parametrize("scan", [False, True])
def test_winrt_native_out_of_range_events_do_not_reach_local_callback(monkeypatch, scan):
    monkeypatch.setattr(transport.sys, "platform", "win32")
    packet = native_advertisement(scan=scan)
    state = install_scanner(monkeypatch, [packet])
    async def run():
        received = []
        async with scanner_context(None, lambda device, adv: received.append(adv)):
            pass
        assert received == [] and state.started == state.stopped == 1
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["weak_packet", "missing_platform", "foreign_pair", "missing_event",
                                  "raw_mismatch", "non_windows"])
def test_only_native_winrt_sentinel_is_filtered_not_generic_weak_rssi(monkeypatch, kind):
    monkeypatch.setattr(transport.sys, "platform", "linux" if kind == "non_windows" else "win32")
    packet = native_advertisement(rssi=-126 if kind == "weak_packet" else -127,
                                  raw_rssi=-126 if kind == "weak_packet" else -127)
    if kind == "missing_platform":
        packet.platform_data = ()
    elif kind == "foreign_pair":
        packet.platform_data = (object(), SimpleNamespace(adv=SimpleNamespace(raw_signal_strength_in_dbm=-127), scan=None))
    elif kind == "missing_event":
        packet.platform_data = (object(), RawAdvData(None, None))
    elif kind == "raw_mismatch":
        packet.platform_data = (object(), RawAdvData(SimpleNamespace(raw_signal_strength_in_dbm=-80), None))
    install_scanner(monkeypatch, [packet])
    async def run():
        received = []
        async with scanner_context(None, lambda device, adv: received.append(adv)):
            pass
        assert received == [packet]
    asyncio.run(run())


def test_native_filter_preserves_async_detection_callback(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "win32")
    ordinary = native_advertisement(rssi=-80, raw_rssi=-80)
    install_scanner(monkeypatch, [native_advertisement(), ordinary])
    async def run():
        received = []
        async def callback(device, advertisement):
            await asyncio.sleep(0)
            received.append(advertisement)
        async with scanner_context(None, callback):
            pass
        assert received == [ordinary]
    asyncio.run(run())


def test_esphome_callbacks_are_not_subject_to_native_windows_sentinel_filter(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "win32")
    packet = native_advertisement()
    class Remote:
        def __init__(self, config, callback):
            self.callback = callback
        async def __aenter__(self):
            self.callback(None, packet)
            return self
        async def __aexit__(self, *_):
            pass
    monkeypatch.setattr(transport, "_RemoteScanner", Remote)
    async def run():
        received = []
        config = BluetoothConfig("esphome", (ProxyConfig("room", "192.0.2.10"),))
        async with scanner_context(config, lambda device, adv: received.append(adv)):
            pass
        assert received == [packet]
    asyncio.run(run())


@pytest.mark.parametrize("model,object_id,payload", [("KS2BB", 0x560C, b"\x01"), ("MS1BB", 0x4A12, b"\x02")])
def test_pseudo_event_after_dedup_window_cannot_replay_press_or_refresh_door_state(
        monkeypatch, tmp_path, make_record, model, object_id, payload):
    monkeypatch.setattr(transport.sys, "platform", "win32")
    record = make_record(model, object_bytes(object_id, payload), address=ADDRESS)
    baseline = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    class Clock:
        current = baseline
        @classmethod
        def now(cls, tz):
            return cls.current
    monkeypatch.setattr(models, "datetime", Clock)
    ordinary = native_advertisement(rssi=-80, raw_rssi=-80)
    pseudo = native_advertisement()
    for packet in (ordinary, pseudo):
        packet.local_name, packet.tx_power = record.name, None
        packet.service_data = {key: bytes.fromhex(value) for key, value in record.service_data.items()}
        packet.manufacturer_data, packet.service_uuids = {}, record.service_uuids
    state = install_scanner(monkeypatch, [ordinary, pseudo],
                            before=lambda index: setattr(Clock, "current", baseline + timedelta(seconds=index * 8)))
    output = tmp_path / "native-events.jsonl"
    observed = []
    stats = asyncio.run(runtime.capture(mode="monitor", seconds=.01, output=output, quiet=True,
        devices=[DeviceConfig(ADDRESS, model, "Synthetic", TEST_KEY)], on_record=observed.append))
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert stats["received"] == stats["written"] == 1
    assert len(rows) == len(observed) == 1 and rows[0]["status"] == "decoded"
    assert rows[0]["timestamp"] == baseline.isoformat() and rows[0]["rssi"] == -80
    if model == "KS2BB":
        assert [event["event_type"] for event in rows[0]["events"]] == ["press"]
    else:
        assert rows[0]["binary_sensors"][0]["value"] is False and rows[0]["events"] == []
    assert state.started == state.stopped == 1


def test_portable_offline_record_with_minus127_remains_decodable(make_record):
    record = replace(make_record("KS2BB", object_bytes(0x560C, b"\x01")), rssi=-127)
    decoded = Decoder([DeviceConfig(record.address, "KS2BB", "Synthetic", TEST_KEY)]).decode(record)
    assert decoded["status"] == "decoded" and decoded["rssi"] == -127
    assert decoded["events"][0]["event_type"] == "press"
