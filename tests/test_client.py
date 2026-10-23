"""Public async API against an independently encrypted synthetic BLE peer.

These tests never open an adapter or read a local credential file.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from linptech_ble.client import ClientError, LinptechClient, PropertyValue
from linptech_ble.config import DeviceConfig, MIOT_MODELS
from linptech_ble.standard_auth import DirectError, encode_value
from linptech_ble.transport import BluetoothConfig, ProxyConfig
from test_standard_auth import APP_RANDOM, Peer, TOKEN


KEY = "112233445566778899aabbccddeeff00"


def device(model="ES5BB", *, token=TOKEN.hex(), confirmed=False):
    return DeviceConfig("AA:BB:CC:DD:EE:FF", model, "Synthetic device", KEY, token,
                        MIOT_MODELS[model] if confirmed else None)


class Radio:
    """Fake physical context; authentication and spec traffic stay real."""
    def __init__(self, monkeypatch, *, peer_factory=Peer, opening=None):
        self.peers, self.closed, self.opening = [], 0, opening
        self.factory = peer_factory
        self.calls = []
        monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
        monkeypatch.setattr("linptech_ble.client.connected_client", self.context)

    @asynccontextmanager
    async def context(self, transport, address, name):
        self.calls.append((transport, address, name))
        if self.opening:
            await self.opening.wait()
        peer = self.factory()
        peer.props.setdefault((6, 5), encode_value("[0,1]", "string"))
        peer.props.setdefault((6, 8), encode_value("[0,0]", "string"))
        peer.is_connected = True
        self.peers.append(peer)
        try:
            yield peer
        finally:
            peer.is_connected = False
            self.closed += 1

    @property
    def peer(self):
        return self.peers[-1]


def add(peer, siid, piid, value, format):
    peer.props[(siid, piid)] = encode_value(value, format)


def requested(peer):
    return [(p[4], p[6], int.from_bytes(p[7:9], "little")) for p in peer.requests if p[4] != 0xF0]


def commands(peer):
    return [int.from_bytes(p[11:11 + p[9]], "little") for p in peer.requests
            if p[4] == 0 and p[6:9] == b"\x06\x09\0"]


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_catalog_no_io_whitelist_and_secret_free_metadata(monkeypatch):
    radio = Radio(monkeypatch)
    client = LinptechClient(device())
    assert client.model == "ES5BB" and client.protocol_model == MIOT_MODELS["ES5BB"]
    assert client.supported and not client.connected and not radio.calls
    assert not client.properties["pir_energy"].gatt_read
    assert client.properties["pir_energy"].notification_only
    assert client.properties["pir_energy"].format == "int32"
    assert not client.properties["cmd_to_device"].writable
    assert client.properties["no_one_duration"].allowed_values == (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120)
    assert client.get_property((6, 18)).status == "unread"
    assert KEY not in repr(client) and TOKEN.hex() not in repr(client)
    assert KEY not in json.dumps(client.get_property("pir_energy").to_dict())
    with pytest.raises(TypeError):
        client.properties["unsafe"] = client.properties["pir_energy"]
    for invalid in ("not-allowed", (1, 99), (True, 1), [6, 18]):
        with pytest.raises(ClientError) as error:
            client.get_property(invalid)
        assert error.value.code == "unknown_property"
    for kwargs in ({"queue_size": True}, {"queue_size": 0}, {"auth_timeout": float("nan")},
                   {"operation_timeout": 0}, {"transport": "secret"}):
        with pytest.raises((ValueError, TypeError)):
            LinptechClient(device(), **kwargs)


def test_advertisement_only_and_bindkey_not_auth_token_reject_before_radio(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        for config, code in ((device("MS1BB"), "advertisement_only"),
                             (device(token=None), "missing_auth_token")):
            client = LinptechClient(config)
            with pytest.raises(ClientError) as error:
                await client.connect()
            assert error.value.code == code
            await client.aclose()
    asyncio.run(run())
    assert not radio.calls


def test_encrypted_read_write_cache_and_idempotent_close(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        client = LinptechClient(device())
        async with client:
            assert await client.connect() is client
            assert len(radio.calls) == 1
            session = client._session
            assert (await client.read((6, 11))).value == 1
            before = len(radio.peer.requests)
            assert (await client.set("no_one_determine_time", 31)).value == 31
            # Subscription op2 is also a fresh snapshot/readback, as negotiated
            # by the shared session; neither notification is a cached write ACK.
            assert [p[4] for p in radio.peer.requests[before:]] == [2, 0, 2]
            assert (await client.read("no_one_determine_time")).value == 31
            add(radio.peer, 2, 1078, 0, "uint8")
            add(radio.peer, 6, 72, False, "bool")
            values = await client.refresh(["occupancy_status", "interest_enable", "pir_energy"])
            assert values["occupancy_status"].value == 0
            assert values["interest_enable"].value is False
            assert values["pir_energy"].status == "unread"
            assert not any((siid, piid) == (6, 18) for _, siid, piid in requested(radio.peer))
            assert client.get_property("interest_enable").valid
        assert not client.connected and radio.closed == 1
        assert not radio.peer.callbacks and session.cipher is None and session._token == b""
        assert client.get_property("interest_enable").status == "cached"
        await client.aclose()
        assert radio.closed == 1
        with pytest.raises(ClientError) as error:
            await client.refresh([])
        assert error.value.code == "not_connected"
        await client.connect()
        assert len(radio.peers) == 2
        assert (await client.read("sensitivity_level")).value == 1
        await client.aclose()
        assert radio.closed == 2
    asyncio.run(run())


def test_invalid_enum_type_null_and_unknown_push_do_not_become_valid(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            add(radio.peer, 2, 1082, 4, "uint8")
            with pytest.raises(ClientError) as error:
                await client.read("no_one_duration")
            assert error.value.code == "invalid_value"
            value = client.get_property("no_one_duration")
            assert value.value == 4 and value.status == "invalid_value" and not value.valid
            add(radio.peer, 6, 11, 1, "uint16")
            values = await client.refresh("sensitivity_level")
            assert values["sensitivity_level"].status == "invalid_value"
            radio.peer.props[(6, 4)] = (0xF0, b"")
            values = await client.refresh(["motion_energy"])
            assert values["motion_energy"].status == "unavailable"
            assert values["motion_energy"].value is None
            radio.peer.notify_property(99, 250, 99, *encode_value(42, "uint8"))
            await until(lambda: client.unknown_updates == 1)
            assert client.connected and client._session.error is None
    asyncio.run(run())


def test_model_write_guards_pir_reserved_bits_and_verified_readback(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            before = len(radio.peer.requests)
            for name, value in (("cmd_to_device", 512), ("sensitivity_level", True), ("sensitivity_level", 9)):
                with pytest.raises(DirectError):
                    await client.set(name, value)
            assert len(radio.peer.requests) == before
            add(radio.peer, 6, 60, 0x8000, "uint16")
            with pytest.raises(ClientError) as error:
                await client.set("no_one_report_mask", 1)
            assert error.value.code == "unknown_reserved_bits"
            assert not any(op == 0 for op, _, _ in requested(radio.peer))
            add(radio.peer, 6, 19, 42, "uint8")
            add(radio.peer, 6, 13, 1, "uint8")
            with pytest.raises(ClientError) as error:
                await client.set("pir_threshold", 43)
            assert error.value.code == "pir_not_custom"
            add(radio.peer, 6, 13, 0, "uint8")
            assert (await client.set("pir_threshold", 43)).value == 43
            radio.peer.reject_set = True
            with pytest.raises(DirectError, match="回讀不一致"):
                await client.set("no_one_determine_time", 32)
            assert client.connected
        async with LinptechClient(device("HS3BB")) as client:
            with pytest.raises(ClientError) as error:
                await client.set("indicator_light", False)
            assert error.value.code == "model_unconfirmed"
            assert not requested(radio.peer)
        async with LinptechClient(device("HS3BB", confirmed=True)) as client:
            add(radio.peer, 4, 1, True, "bool")
            assert (await client.set("indicator_light", False)).value is False
            with pytest.raises(DirectError):
                await client.set("detection_interval", 25)
        async with LinptechClient(device("KS2BB")) as client:
            add(radio.peer, 6, 2, 0, "uint8")
            assert (await client.set("temperature_unit", 1)).value == 1
            with pytest.raises(ClientError) as error:
                await client.set_stream("radar")
            assert error.value.code == "unsupported_stream"
    asyncio.run(run())


def test_stream_pir_notify_only_context_restore_and_fixed_calibration(monkeypatch):
    radio = Radio(monkeypatch, peer_factory=lambda: Peer(empty_subscribe=True))
    async def run():
        async with LinptechClient(device()) as client:
            add(radio.peer, 6, 18, -27, "int32")
            with pytest.raises(ClientError) as error:
                await client.read("pir_energy")
            assert error.value.code == "notification_only" and not requested(radio.peer)
            await client.set_stream("radar")
            for pair in ((6, 4), (6, 5), (6, 8)):
                assert (2, *pair) in requested(radio.peer)
                assert (1, *pair) not in requested(radio.peer)
            with pytest.raises(ClientError):
                await client.radar_calibrate()
            assert await client.radar_calibrate(empty_room=True)
            with pytest.raises(RuntimeError, match="body failed"):
                async with client.stream("pir"):
                    assert client.stream_mode == "pir"
                    assert (2, 6, 18) in requested(radio.peer)
                    assert (1, 6, 18) not in requested(radio.peer)
                    radio.peer.notify_property(100, 6, 18, *encode_value(-39, "int32"))
                    await until(lambda: client.get_property("pir_energy").value == -39)
                    assert client.get_property("pir_energy").valid
                    async with client.stream("off"):
                        assert client.stream_mode == "off"
                    assert client.stream_mode == "pir"
                    raise RuntimeError("body failed")
            assert client.stream_mode == "radar"
            assert commands(radio.peer) == [0, 1, 256, 0, 4, 0, 0, 4, 0, 1]
        assert commands(radio.peer)[-1] == 0
    asyncio.run(run())


def test_stream_body_cancellation_restores_and_rejects_concurrent_owner(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            add(radio.peer, 6, 18, 0, "int32")
            await client.set_stream("radar")
            entered = asyncio.Event()
            async def temporary():
                async with client.stream("pir"):
                    entered.set()
                    await asyncio.Event().wait()
            task = asyncio.create_task(temporary())
            await entered.wait()
            with pytest.raises(ClientError) as error:
                await client.set_stream("off")
            assert error.value.code == "stream_in_use"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client.connected and client.stream_mode == "radar"
            await client.set_stream("off")
    asyncio.run(run())


def test_failed_stream_switch_restores_previous_and_reports_unknown_if_restore_fails(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            await client.set_stream("radar")
            original = client._session.command
            async def declined(value):
                if value == 4:
                    return False
                return await original(value)
            add(radio.peer, 6, 18, -5, "int32")
            client._session.command = declined
            with pytest.raises(ClientError) as error:
                await client.set_stream("pir")
            assert error.value.code == "stream_unconfirmed"
            assert client.stream_mode == "radar"
            async def no_ack(value):
                return False
            client._session.command = no_ack
            with pytest.raises(ClientError):
                await client.set_stream("off")
            assert client.stream_mode == "unknown"
    asyncio.run(run())


def test_observers_callbacks_filter_overflow_errors_and_close(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        callbacks = []
        async def callback(value):
            await asyncio.sleep(0)
            callbacks.append(value.value)
            if value.value == 2:
                raise RuntimeError(TOKEN.hex())
        async with LinptechClient(device(), on_update=callback, queue_size=2) as client:
            observer = client.updates(["sensitivity_level"], replay=False)
            first = asyncio.create_task(anext(observer))
            await until(lambda: bool(client._observers))
            radio.peer.notify_property(99, 6, 11, *encode_value(2, "uint8"))
            assert (await first).value == 2
            await until(lambda: client.callback_errors == 1)
            assert TOKEN.hex() not in client.callback_error
            assert client.connected and client._session.error is None
            for value in (0, 1, 3):
                radio.peer.notify_property(99, 6, 11, *encode_value(value, "uint8"))
            await until(lambda: client.get_property("sensitivity_level").value == 3)
            assert (await anext(observer)).value == 1
            assert (await anext(observer)).value == 3
            assert client.dropped_updates >= 1
            replay = client.updates("sensitivity_level")
            assert (await anext(replay)).value == 3
            await replay.aclose()
            assert client.connected
            pending = asyncio.create_task(anext(observer))
            await client.aclose()
            with pytest.raises(StopAsyncIteration):
                await pending
            assert not client._observers
        assert radio.closed == 1
    asyncio.run(run())


def test_sync_callback_can_run_and_async_callback_may_close_client(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        seen = []
        async with LinptechClient(device(), on_update=seen.append) as client:
            await client.read("sensitivity_level")
            await until(lambda: len(seen) == 1)
            assert isinstance(seen[0], PropertyValue)
        closed = asyncio.Event()
        async def closing_callback(value):
            await client.aclose()
            closed.set()
        client = LinptechClient(device(), on_update=closing_callback)
        await client.connect()
        await client.read("sensitivity_level")
        await asyncio.wait_for(closed.wait(), 2)
        assert not client.connected and radio.closed == 2
    asyncio.run(run())


def test_callback_reconnect_does_not_keep_old_generation_consumer(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        reconnected = asyncio.Event()
        seen = []
        original_worker = None
        async def callback(value):
            nonlocal original_worker
            seen.append(value.value)
            if len(seen) == 1:
                original_worker = asyncio.current_task()
                await client.aclose()
                await client.connect()
                reconnected.set()
        async with LinptechClient(device(), on_update=callback) as client:
            await client.read("sensitivity_level")
            await reconnected.wait()
            await until(lambda: original_worker.done())
            assert client.connected and client._callback_task is not original_worker
            await client.read("sensitivity_level")
            await until(lambda: len(seen) == 2)
            assert seen == [1, 1]
        assert radio.closed == 2
    asyncio.run(run())


def test_old_observer_closes_even_if_reconnect_and_overflow_before_next_iteration(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        client = LinptechClient(device(), queue_size=1)
        await client.connect()
        observer = client.updates("sensitivity_level", replay=False)
        waiting = asyncio.create_task(anext(observer))
        await until(lambda: bool(client._observers))
        await client.read("sensitivity_level")
        assert (await waiting).value == 1
        await client.aclose()
        await client.connect()
        await client.read("sensitivity_level")
        with pytest.raises(StopAsyncIteration):
            await anext(observer)
        assert client.connected and not client._observers
        await client.aclose()
    asyncio.run(run())


def test_queued_calibration_rechecks_stream_under_wire_lock(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            add(radio.peer, 6, 18, 0, "int32")
            await client.set_stream("radar")
            await client._operation_lock.acquire()
            switching = asyncio.create_task(client.set_stream("pir"))
            await asyncio.sleep(0)
            calibrating = asyncio.create_task(client.radar_calibrate(empty_room=True))
            await asyncio.sleep(0)
            client._operation_lock.release()
            await switching
            with pytest.raises(ClientError) as error:
                await calibrating
            assert error.value.code == "calibration_preparation_required"
            assert client.stream_mode == "pir" and 256 not in commands(radio.peer)
    asyncio.run(run())


def test_queued_write_cannot_cross_close_reconnect_into_new_session(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            await client._operation_lock.acquire()
            queued = asyncio.create_task(client.set("no_one_determine_time", 31))
            await asyncio.sleep(0)
            await client.aclose()
            await client.connect()
            client._operation_lock.release()
            with pytest.raises(ClientError) as error:
                await queued
            assert error.value.code == "session_changed"
            assert not requested(radio.peer) and client.connected
    asyncio.run(run())


def test_cancelling_old_queued_work_and_exiting_stream_never_mutates_new_session(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            await client.set_stream("radar")
            add(radio.peer, 6, 18, 0, "int32")
            async with client.stream("pir"):
                await client._operation_lock.acquire()
                queued = asyncio.create_task(client.read("sensitivity_level"))
                await asyncio.sleep(0)
                await client.aclose()
                await client.connect()
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
                client._operation_lock.release()
                assert client.connected and not requested(radio.peer)
                # A stale old context does not retain stream ownership over a
                # new connection, even when the new caller is another task.
                await asyncio.create_task(client.set_stream("radar"))
                after = list(radio.peer.requests)
            assert radio.peer.requests == after
            assert client.stream_mode == "radar" and client.connected
    asyncio.run(run())


def test_wire_timeout_and_cancel_close_auth_and_context(monkeypatch):
    radio = Radio(monkeypatch, peer_factory=lambda: Peer(ignore_get=True))
    async def run():
        client = LinptechClient(device(), operation_timeout=.04)
        await client.connect()
        session = client._session
        with pytest.raises(asyncio.TimeoutError):
            await client.read("sensitivity_level", subscribe=False)
        assert not client.connected and radio.closed == 1 and session.cipher is None
        await client.connect()
        client.operation_timeout = 5
        task = asyncio.create_task(client.read("sensitivity_level", subscribe=False))
        await until(lambda: any(p[4] == 1 for p in radio.peer.requests))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.connected and radio.closed == 2 and not radio.peer.callbacks
    asyncio.run(run())


def test_open_failure_cancellation_external_close_and_remote_config_forwarding(monkeypatch):
    class BrokenPeer(Peer):
        async def start_notify(self, uuid, callback):
            raise RuntimeError(TOKEN.hex())
    radio = Radio(monkeypatch, peer_factory=BrokenPeer)
    async def run():
        client = LinptechClient(device())
        with pytest.raises(ClientError) as error:
            await client.connect()
        assert error.value.code == "connection_failed" and TOKEN.hex() not in str(error.value)
        assert radio.closed == 1 and not client.connected
        radio.factory = Peer
        entered = asyncio.Event()
        original = Peer.start_notify
        async def stalled(peer, uuid, callback):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(Peer, "start_notify", stalled)
        opening = asyncio.create_task(client.connect())
        await entered.wait()
        await asyncio.wait_for(client.aclose(), 2)
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert radio.closed == 2 and not client.connected
        monkeypatch.setattr(Peer, "start_notify", original)
        await client.connect()
        radio.peer.is_connected = False
        await client.connect()
        assert radio.closed == 3 and len(radio.peers) == 4
        await client.aclose()
        assert radio.closed == 4
    asyncio.run(run())


def test_remote_choice_forwarded_without_local_fallback_and_keepalive(monkeypatch):
    radio = Radio(monkeypatch)
    remote = BluetoothConfig("esphome", (ProxyConfig("room", "192.0.2.10", password="synthetic-password"),))
    async def run():
        async with LinptechClient(device(), transport=remote) as client:
            assert radio.calls == [(remote, "AA:BB:CC:DD:EE:FF", "Synthetic device")]
            assert not requested(radio.peer)  # Connect does not scan/read/start report.
            add(radio.peer, 6, 7, 0xFFFFFFFF, "uint32")
            assert (await client.keepalive()).value == 0xFFFFFFFF
            assert requested(radio.peer) == [(2, 6, 7)]
        async with LinptechClient(device("KS2BB")) as client:
            with pytest.raises(ClientError) as error:
                await client.keepalive()
            assert error.value.code == "unsupported_keepalive"
    asyncio.run(run())


def test_auth_timeout_connect_cancellation_and_repeated_close_cancellation_release(monkeypatch):
    radio = Radio(monkeypatch)
    started = None
    async def stalled(peer, uuid, callback):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(Peer, "start_notify", stalled)
    async def run():
        nonlocal started
        started = asyncio.Event()
        client = LinptechClient(device(), auth_timeout=.02)
        with pytest.raises(ClientError) as error:
            await client.connect()
        assert error.value.code == "connection_failed" and radio.closed == 1
        client.auth_timeout = 2
        started.clear()
        connecting = asyncio.create_task(client.connect())
        await started.wait()
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert radio.closed == 2 and not client.connected
    asyncio.run(run())


def test_close_remains_protected_when_caller_is_cancelled_repeatedly(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        client = LinptechClient(device())
        await client.connect()
        session = client._session
        original = session.close
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow_close():
            started.set()
            await finish.wait()
            await original()
        session.close = slow_close
        closing = asyncio.create_task(client.aclose())
        await started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done() and radio.closed == 0
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert session.cipher is None and not radio.peer.callbacks and radio.closed == 1
        await client.aclose()
    asyncio.run(run())


def test_external_close_does_not_deadlock_callback_finally_also_closing(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        entered, exited = asyncio.Event(), asyncio.Event()
        async def callback(value):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await client.aclose()
                exited.set()
        client = LinptechClient(device(), on_update=callback)
        await client.connect()
        await client.read("sensitivity_level")
        await entered.wait()
        await asyncio.wait_for(client.aclose(), .5)
        await asyncio.wait_for(exited.wait(), .5)
        assert not client.connected and radio.closed == 1 and not radio.peer.callbacks
    asyncio.run(run())


def test_client_refuses_another_event_loop(monkeypatch):
    radio = Radio(monkeypatch)
    client = LinptechClient(device())
    async def first():
        async with client:
            await client.read("sensitivity_level")
    asyncio.run(first())
    async def second():
        with pytest.raises(ClientError) as error:
            await client.connect()
        assert error.value.code == "wrong_loop"
        with pytest.raises(ClientError) as error:
            await client.read("sensitivity_level")
        assert error.value.code == "wrong_loop"
    asyncio.run(second())
    assert radio.closed == 1


def test_refresh_hard_deadline_closes_and_observer_cancel_keeps_connection(monkeypatch):
    radio = Radio(monkeypatch, peer_factory=lambda: Peer(ignore_get=True, empty_subscribe=True))
    async def run():
        async with LinptechClient(device()) as client:
            observer = client.updates(replay=False)
            task = asyncio.create_task(anext(observer))
            await until(lambda: bool(client._observers))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client.connected and not client._observers
            with pytest.raises(asyncio.TimeoutError):
                await client.refresh(["sensitivity_level"], timeout=.04)
            assert not client.connected and radio.closed == 1
    asyncio.run(run())
