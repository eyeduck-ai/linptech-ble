"""Public injected GATT contexts against an independently encrypted fake peer."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from linptech_ble.client import ClientError, LinptechClient
from linptech_ble.config import DeviceConfig
from linptech_ble.standard_auth import DirectError, encode_value
from linptech_ble.transport import BluetoothConfig, TransportError
from test_standard_auth import APP_RANDOM, Peer, TOKEN


MAC = "AA:BB:CC:DD:EE:91"
KEY = "112233445566778899aabbccddeeff00"
PRIVATE = "private-api-cookie-and-auth-token"


def device(address=MAC):
    return DeviceConfig(address, "ES5BB", "Synthetic custom connection", KEY, TOKEN.hex())


class Factory:
    def __init__(self, peer_factory=Peer):
        self.peer_factory, self.calls, self.peers, self.exits = peer_factory, [], [], []

    @asynccontextmanager
    async def __call__(self, address, name):
        self.calls.append((address, name))
        peer = self.peer_factory()
        peer.is_connected = True
        peer.props.update({(6, 5): encode_value("[0,1]", "string"), (6, 8): encode_value("[0,0]", "string")})
        self.peers.append(peer)
        try:
            yield peer
        finally:
            # The factory owns GATT, but core auth/notify cleanup happens first.
            self.exits.append({"callbacks": list(peer.callbacks), "requests": list(peer.requests)})
            peer.is_connected = False


@pytest.fixture(autouse=True)
def no_builtin_bluetooth(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("A custom connection factory must not touch built-in BLE discovery/transport")
    monkeypatch.setattr("linptech_ble.client.connected_client", forbidden)
    monkeypatch.setattr("bleak.BleakScanner", forbidden)
    monkeypatch.setattr("bleak.BleakClient", forbidden)
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_factory_is_explicit_no_io_on_construction_and_receives_no_credentials():
    factory = Factory()
    client = LinptechClient(device(), connection_factory=factory)
    assert not factory.calls and not client.connected
    assert client.diagnostics()["connection_cleanup"] == {"status": "not_started", "error_type": None}
    assert client.properties["sensitivity_level"].name == "sensitivity_level"
    async def run():
        async with client:
            session = client._session
            assert (await client.read("sensitivity_level")).value == 1
            assert (await client.set("sensitivity_level", 2)).value == 2
        assert session.cipher is None and session._token == b""
        assert factory.exits[0]["callbacks"] == []
        assert factory.calls == [(MAC, "Synthetic custom connection")]
        assert KEY not in json.dumps(factory.calls) and TOKEN.hex() not in json.dumps(factory.calls)
        await client.aclose()
        assert len(factory.exits) == 1
        async with client:
            assert client.connected
        assert len(factory.exits) == 2
        diagnostic = client.diagnostics()
        assert diagnostic["connection_cleanup"] == {"status": "closed", "error_type": None}
        diagnostic["connection_cleanup"]["status"] = "changed outside the client"
        assert client.diagnostics()["connection_cleanup"]["status"] == "closed"
    asyncio.run(run())


@pytest.mark.parametrize("options", [
    {"connection_factory": object()}, {"connection_factory": Factory(), "transport": BluetoothConfig()},
    {"connection_timeout": True}, {"connection_timeout": 0}, {"connection_timeout": float("nan")},
    {"connection_timeout": 10 ** 400},
])
def test_invalid_factory_configuration_fails_before_opening(options):
    with pytest.raises((TypeError, ValueError)):
        LinptechClient(device(), **options)


def test_core_stops_owned_stream_and_notifications_before_factory_disconnect():
    factory = Factory()
    async def run():
        async with LinptechClient(device(), connection_factory=factory) as client:
            await client.set_stream("radar")
            assert client.stream_mode == "radar"
        exit = factory.exits[0]
        commands = [int.from_bytes(request[11:11 + request[9]], "little")
                    for request in exit["requests"] if request[4] == 0 and request[6:9] == b"\x06\x09\0"]
        assert commands[-1] == 0 and not exit["callbacks"]
    asyncio.run(run())


@pytest.mark.parametrize("error_type", [RuntimeError, DirectError, TransportError])
def test_factory_entry_errors_are_sanitized_without_exiting_an_unentered_context(error_type):
    calls = []
    class Broken:
        async def __aenter__(self):
            calls.append("enter")
            raise error_type(PRIVATE)
        async def __aexit__(self, *_):
            calls.append("exit")
    async def run():
        client = LinptechClient(device(), connection_factory=lambda *_: Broken())
        with pytest.raises(ClientError) as error:
            await client.connect()
        assert error.value.code == "connection_failed" and PRIVATE not in str(error.value)
        assert error.value.__cause__ is None
        assert calls == ["enter"]
        assert client.diagnostics()["connection_cleanup"] == {"status": "not_started", "error_type": None}
        async with LinptechClient(device(), connection_factory=Factory()):
            pass  # The failed opening released its same-device lease.
    asyncio.run(run())


def test_authentication_failure_cleans_subscriptions_and_factory_context():
    class BrokenPeer(Peer):
        async def start_notify(self, *_):
            raise RuntimeError(PRIVATE)
    factory = Factory(BrokenPeer)
    async def run():
        client = LinptechClient(device(), connection_factory=factory)
        with pytest.raises(ClientError) as error:
            await client.connect()
        assert error.value.code == "connection_failed" and PRIVATE not in str(error.value)
        assert len(factory.exits) == 1 and factory.exits[0]["callbacks"] == []
        async with LinptechClient(device(), connection_factory=Factory()):
            pass
    asyncio.run(run())


def test_cleanup_failure_does_not_replace_primary_authentication_failure():
    class BrokenPeer(Peer):
        async def start_notify(self, *_):
            raise RuntimeError(PRIVATE)
    factory = Factory(BrokenPeer)
    @asynccontextmanager
    async def failing_cleanup(address, name):
        try:
            async with factory(address, name) as peer:
                yield peer
        finally:
            raise OSError("private-cleanup-endpoint")
    async def run():
        client = LinptechClient(device(), connection_factory=failing_cleanup)
        with pytest.raises(ClientError) as error:
            await client.connect()
        assert error.value.code == "connection_failed"
        assert isinstance(error.value.__context__, RuntimeError)
        assert PRIVATE not in str(error.value) and "private-cleanup-endpoint" not in str(error.value)
        assert client.diagnostics()["connection_cleanup"] == {"status": "error", "error_type": "OSError"}
        assert "private" not in json.dumps(client.diagnostics())
        async with LinptechClient(device(), connection_factory=Factory()):
            pass
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["timeout", "cancel", "external_close"])
def test_custom_open_timeout_or_cancellation_cleans_partial_entry_and_lease(mode):
    async def run():
        entered, cleaned = asyncio.Event(), asyncio.Event()
        exits = []
        class Opening:
            async def __aenter__(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()  # Factory owns partial-entry cleanup.
            async def __aexit__(self, *_):
                exits.append(True)
        client = LinptechClient(device(), connection_factory=lambda *_: Opening(),
                               connection_timeout=.03 if mode == "timeout" else 2)
        task = asyncio.create_task(client.connect())
        await entered.wait()
        if mode == "timeout":
            with pytest.raises(ClientError) as error:
                await task
            assert error.value.code == "connection_timeout"
        else:
            if mode == "cancel":
                task.cancel()
            else:
                await asyncio.wait_for(client.aclose(), 1)
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cleaned.is_set() and not exits and not client.connected
        async with LinptechClient(device(), connection_factory=Factory()):
            pass
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["closed", "timeout", "error", "cancel"])
def test_custom_factory_exit_is_bounded_and_cannot_leak_the_device_lease(mode):
    async def run():
        cleaned = asyncio.Event()
        peer = Peer()
        peer.is_connected = True
        class Closing:
            async def __aenter__(self):
                return peer
            async def __aexit__(self, *_):
                assert not peer.callbacks
                try:
                    if mode == "timeout":
                        await asyncio.Event().wait()
                    if mode == "cancel":
                        raise asyncio.CancelledError()
                    if mode == "closed":
                        return
                    raise RuntimeError(PRIVATE)
                finally:
                    peer.is_connected = False
                    cleaned.set()
        client = LinptechClient(device(), connection_factory=lambda *_: Closing(), connection_timeout=.03)
        await client.connect()
        await asyncio.wait_for(client.aclose(), .5)
        assert cleaned.is_set() and not client.connected
        status, error_type = {"closed": ("closed", None), "timeout": ("timeout", "TimeoutError"),
                              "error": ("error", "RuntimeError"), "cancel": ("cancelled", "CancelledError")}[mode]
        diagnostic = client.diagnostics()
        assert diagnostic["connection_cleanup"] == {"status": status, "error_type": error_type}
        assert PRIVATE not in json.dumps(diagnostic) and TOKEN.hex() not in json.dumps(diagnostic)
        async with LinptechClient(device(), connection_factory=Factory()):
            pass
    asyncio.run(run())


def test_connection_cleanup_diagnostic_resets_on_reconnect():
    failure = [True]
    @asynccontextmanager
    async def factory(*_):
        peer = Peer()
        peer.is_connected = True
        try:
            yield peer
        finally:
            peer.is_connected = False
            if failure[0]:
                raise RuntimeError(PRIVATE)
    async def run():
        client = LinptechClient(device(), connection_factory=factory)
        await client.connect()
        await client.aclose()
        assert client.diagnostics()["connection_cleanup"]["status"] == "error"
        failure[0] = False
        await client.connect()
        assert client.diagnostics()["connection_cleanup"] == {"status": "not_started", "error_type": None}
        await client.aclose()
        assert client.diagnostics()["connection_cleanup"] == {"status": "closed", "error_type": None}
    asyncio.run(run())


def test_cleanup_failure_does_not_replace_the_callers_cancellation():
    async def run():
        exiting, finish = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def factory(*_):
            peer = Peer()
            peer.is_connected = True
            try:
                yield peer
            finally:
                exiting.set()
                await finish.wait()
                peer.is_connected = False
                raise RuntimeError(PRIVATE)
        client = LinptechClient(device(), connection_factory=factory)
        await client.connect()
        closing = asyncio.create_task(client.aclose())
        await exiting.wait()
        closing.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert client.diagnostics()["connection_cleanup"] == {"status": "error", "error_type": "RuntimeError"}
        assert PRIVATE not in json.dumps(client.diagnostics())
        async with LinptechClient(device(), connection_factory=Factory()):
            pass
    asyncio.run(run())


def test_same_device_is_busy_until_factory_exit_and_distinct_devices_are_parallel():
    async def run():
        factory, second_factory = Factory(), Factory()
        first = LinptechClient(device(), connection_factory=factory)
        second = LinptechClient(device(), connection_factory=second_factory)
        other = LinptechClient(device("AA:BB:CC:DD:EE:92"), connection_factory=second_factory)
        await asyncio.gather(first.connect(), other.connect())
        assert first.connected and other.connected and len(second_factory.calls) == 1
        with pytest.raises(ClientError) as error:
            await second.connect()
        assert error.value.code == "device_busy" and len(second_factory.calls) == 1
        await first.aclose()
        await second.connect()
        assert second.connected
        await asyncio.gather(second.aclose(), other.aclose())
    asyncio.run(run())


def test_same_mac_lease_also_guards_other_event_loops():
    async def run():
        factory = Factory()
        async with LinptechClient(device(), connection_factory=factory):
            async def competing():
                with pytest.raises(ClientError) as error:
                    await LinptechClient(device(), connection_factory=Factory()).connect()
                assert error.value.code == "device_busy"
            await asyncio.to_thread(lambda: asyncio.run(competing()))
    asyncio.run(run())


def test_lease_is_retained_until_context_exit_finishes_and_busy_clients_cannot_release_it():
    async def run():
        exiting, finish = asyncio.Event(), asyncio.Event()
        peer = Peer()
        peer.is_connected = True
        @asynccontextmanager
        async def factory(*_):
            try:
                yield peer
            finally:
                exiting.set()
                await finish.wait()
                peer.is_connected = False
        owner = LinptechClient(device(), connection_factory=factory)
        await owner.connect()
        closing = asyncio.create_task(owner.aclose())
        await exiting.wait()
        # Default transports share the same guard and must fail before opening
        # a built-in scanner. Their cleanup cannot release another owner's token.
        for competitor in (LinptechClient(device()), LinptechClient(device(), connection_factory=Factory())):
            with pytest.raises(ClientError) as error:
                await competitor.connect()
            assert error.value.code == "device_busy"
        finish.set()
        await closing
        async with LinptechClient(device(), connection_factory=Factory()):
            pass
    asyncio.run(run())


def test_waiting_opening_already_owns_lease_until_its_partial_cleanup_finishes():
    async def run():
        entered, cleanup_started, cleanup_finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def opening(*_):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield None
            finally:
                cleanup_started.set()
                await cleanup_finish.wait()
        first = LinptechClient(device(), connection_factory=opening)
        task = asyncio.create_task(first.connect())
        await entered.wait()
        task.cancel()
        await cleanup_started.wait()
        with pytest.raises(ClientError) as error:
            await LinptechClient(device(), connection_factory=Factory()).connect()
        assert error.value.code == "device_busy"
        cleanup_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with LinptechClient(device(), connection_factory=Factory()):
            pass
    asyncio.run(run())


def test_invalid_factory_return_and_disconnected_yield_are_cleaned_without_fallback():
    async def run():
        for factory in (lambda *_: None, lambda *_: object()):
            with pytest.raises(ClientError) as error:
                await LinptechClient(device(), connection_factory=factory).connect()
            assert error.value.code == "connection_failed"
        async def wrong_coroutine(*_):
            return object()
        with pytest.raises(ClientError):
            await LinptechClient(device(), connection_factory=wrong_coroutine).connect()
        exited = []
        @asynccontextmanager
        async def disconnected(*_):
            peer = Peer()
            peer.is_connected = False
            try:
                yield peer
            finally:
                exited.append(True)
        with pytest.raises(ClientError):
            await LinptechClient(device(), connection_factory=disconnected).connect()
        assert exited == [True]
        async with LinptechClient(device(), connection_factory=Factory()):
            pass
    asyncio.run(run())


def test_custom_wire_timeout_and_foreign_error_close_auth_before_releasing_context():
    async def run():
        factory = Factory(lambda: Peer(ignore_get=True))
        client = LinptechClient(device(), connection_factory=factory, operation_timeout=.03)
        await client.connect()
        with pytest.raises(asyncio.TimeoutError):
            await client.read("sensitivity_level", subscribe=False)
        assert not client.connected and not factory.exits[0]["callbacks"]
        factory = Factory()
        client = LinptechClient(device(), connection_factory=factory)
        await client.connect()
        async def fail(*_, **__):
            raise RuntimeError(PRIVATE)
        factory.peers[0].write_gatt_char = fail
        with pytest.raises(ClientError) as error:
            await client.read("sensitivity_level", subscribe=False)
        assert error.value.code == "connection_failed" and PRIVATE not in str(error.value)
        assert not client.connected and not factory.exits[0]["callbacks"]
    asyncio.run(run())
