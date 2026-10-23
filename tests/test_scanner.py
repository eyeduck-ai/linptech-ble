"""Public scanner lifecycle and source-preserving feeds; no Bluetooth or files."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from linptech_ble import AdvertisementScanner, Decoder, DeviceConfig
from linptech_ble.transport import TransportError
from conftest import ADDRESS, TEST_KEY, object_bytes


class Radio:
    def __init__(self, monkeypatch):
        self.callback = None
        self.enters = self.exits = 0
        self.enter_error = None
        self.exit_gate = None
        self.healthy = True
        monkeypatch.setattr("linptech_ble.scanner.scanner_context", self.context)

    @asynccontextmanager
    async def context(self, transport, callback):
        self.enters += 1
        if self.enter_error:
            raise self.enter_error
        self.callback = callback
        try:
            yield self
        finally:
            if self.exit_gate:
                await self.exit_gate.wait()
            self.exits += 1

    def check_health(self):
        if not self.healthy:
            raise TransportError("Synthetic receivers unavailable.")

    def health(self):
        return {"backend": "esphome", "connected": self.healthy,
                "proxies": [{"name": "room", "connected": self.healthy}]}

    def emit(self, record, source="room", **overrides):
        device = SimpleNamespace(address=record.address, name=record.name, details={"source": source})
        adv = SimpleNamespace(local_name=record.name, rssi=record.rssi,
            service_data={k: bytes.fromhex(v) for k, v in record.service_data.items()},
            manufacturer_data={}, service_uuids=record.service_uuids, tx_power=None)
        for key, value in overrides.items():
            setattr(adv, key, value)
        self.callback(device, adv)


def test_construction_is_lazy_and_queue_is_bounded(monkeypatch, make_record):
    async def run():
        radio = Radio(monkeypatch)
        scanner = AdvertisementScanner(queue_size=2)
        assert radio.enters == 0
        async with scanner:
            for value in range(3):
                radio.emit(make_record("KS2BB", object_bytes(0x560C, bytes([value])), counter=value))
            first, second = await anext(scanner), await anext(scanner)
            assert first.service_data != second.service_data
            assert scanner.health()["counts"] == {"received": 3, "selected": 3, "yielded": 2,
                "dropped": 1, "invalid": 0, "discarded_on_close": 0}
            assert first.receiver == "room"
        assert radio.exits == 1 and scanner.health()["closed"]
        await scanner.aclose()
        assert radio.exits == 1
    asyncio.run(run())


def test_three_receivers_keep_rssi_but_emit_one_button_event(monkeypatch, make_record):
    async def run():
        radio = Radio(monkeypatch)
        decoder = Decoder([DeviceConfig(ADDRESS, "KS2BB", "Button", TEST_KEY)])
        record = make_record("KS2BB", object_bytes(0x560C, b"\x01"))
        async with AdvertisementScanner() as scanner:
            for name, rssi in [("room", -50), ("hall", -70), ("office", -85)]:
                radio.emit(replace(record, rssi=rssi), name)
            results = [decoder.decode(await anext(scanner)) for _ in range(3)]
        assert [row["receiver"] for row in results] == ["room", "hall", "office"]
        assert [row["rssi"] for row in results] == [-50, -70, -85]
        assert [row["status"] for row in results] == ["decoded", "duplicate", "duplicate"]
        assert sum(len(row["events"]) for row in results) == 1
    asyncio.run(run())


def test_filtering_and_bad_data_do_not_poison_subsequent_packets(monkeypatch, make_record):
    async def run():
        radio = Radio(monkeypatch)
        async with AdvertisementScanner(addresses=[ADDRESS.lower()]) as scanner:
            record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
            radio.emit(record, rssi=True)
            radio.emit(replace(record, address="AA:BB:CC:DD:EE:02"))
            radio.emit(record, "correct-source")
            assert (await anext(scanner)).receiver == "correct-source"
            assert scanner.health()["counts"]["invalid"] == 1
            assert scanner.health()["counts"]["received"] == 2
            assert scanner.health()["counts"]["selected"] == 1
    asyncio.run(run())


def test_closing_wakes_waiter_and_cancelling_one_wait_does_not_close(monkeypatch, make_record):
    async def run():
        radio = Radio(monkeypatch)
        async with AdvertisementScanner() as scanner:
            waiter = asyncio.create_task(anext(scanner))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            radio.emit(make_record("ES5BB", object_bytes(0x484E, b"\x01")))
            assert (await anext(scanner)).receiver == "room"
            waiter = asyncio.create_task(anext(scanner))
            await asyncio.sleep(0)
            await scanner.aclose()
            with pytest.raises(StopAsyncIteration):
                await waiter
        assert radio.exits == 1
    asyncio.run(run())


def test_threaded_callback_and_late_delivery_after_close(monkeypatch, make_record):
    async def run():
        radio = Radio(monkeypatch)
        async with AdvertisementScanner() as scanner:
            record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
            await asyncio.to_thread(radio.emit, record, "remote-thread")
            assert (await asyncio.wait_for(anext(scanner), 1)).receiver == "remote-thread"
        radio.emit(record, "late")
        assert scanner.health()["queued"] == 0
    asyncio.run(run())


def test_all_receiver_loss_ends_iteration_with_visible_error(monkeypatch):
    async def run():
        radio = Radio(monkeypatch)
        async with AdvertisementScanner() as scanner:
            radio.healthy = False
            with pytest.raises(TransportError, match="unavailable"):
                await asyncio.wait_for(anext(scanner), 2)
            with pytest.raises(StopAsyncIteration):
                await anext(scanner)
            assert scanner.health()["error"]
        assert radio.exits == 1
    asyncio.run(run())


def test_caller_cancel_cannot_cancel_cleanup(monkeypatch):
    async def run():
        radio = Radio(monkeypatch)
        radio.exit_gate = asyncio.Event()
        scanner = await AdvertisementScanner().__aenter__()
        closing = asyncio.create_task(scanner.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        radio.exit_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await scanner.aclose()
        assert radio.exits == 1 and scanner.health()["closed"]
    asyncio.run(run())


def test_concurrent_iterators_fail_fast_instead_of_hanging_at_close(monkeypatch):
    async def run():
        Radio(monkeypatch)
        async with AdvertisementScanner() as scanner:
            first = asyncio.create_task(anext(scanner))
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError, match="one iterator"):
                await anext(scanner)
            await scanner.aclose()
            with pytest.raises(StopAsyncIteration):
                await first
    asyncio.run(run())


def test_open_failure_is_sanitized_and_failed_enter_is_not_exited(monkeypatch):
    async def run():
        radio = Radio(monkeypatch)
        radio.enter_error = RuntimeError("SYNTHETIC_SECRET_DO_NOT_ECHO")
        scanner = AdvertisementScanner()
        with pytest.raises(TransportError) as caught:
            await scanner.__aenter__()
        assert "SYNTHETIC_SECRET" not in str(caught.value)
        await scanner.aclose()
        assert radio.exits == 0
    asyncio.run(run())


def test_external_close_cancels_pending_entry_and_awaits_its_partial_cleanup(monkeypatch):
    async def run():
        entering, cleaning, finish_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        class Opening:
            async def __aenter__(self):
                entering.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await finish_cleanup.wait()
                    calls.append("partial_entry_cleaned")
            async def __aexit__(self, *_):
                calls.append("exit")
        monkeypatch.setattr("linptech_ble.scanner.scanner_context", lambda *_: Opening())
        scanner = AdvertisementScanner()
        opening = asyncio.create_task(scanner.__aenter__())
        await entering.wait()
        closing = asyncio.create_task(scanner.aclose())
        await cleaning.wait()
        assert not opening.done() and not closing.done()
        finish_cleanup.set()
        await asyncio.wait_for(closing, 1)
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert calls == ["partial_entry_cleaned"]
        assert scanner.health()["closed"] and not scanner.health()["listening"]
        await scanner.aclose()
        assert calls == ["partial_entry_cleaned"]
    asyncio.run(run())


def test_external_close_during_entry_does_not_wait_for_owners_reentrant_finally(monkeypatch):
    async def run():
        entering, cleaned = asyncio.Event(), asyncio.Event()

        class Opening:
            async def __aenter__(self):
                entering.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

            async def __aexit__(self, *_):
                pytest.fail("Failed entry must not be exited")

        monkeypatch.setattr("linptech_ble.scanner.scanner_context", lambda *_: Opening())
        scanner = AdvertisementScanner()

        async def owner():
            try:
                await scanner.__aenter__()
            finally:
                # This ordinary defensive cleanup formerly formed a cycle:
                # close -> whole owner task -> the same close task.
                await scanner.aclose()

        opening = asyncio.create_task(owner())
        await entering.wait()
        closing = asyncio.create_task(scanner.aclose())
        await asyncio.wait_for(closing, 1)
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert cleaned.is_set() and scanner.health()["closed"]

    asyncio.run(run())


def test_repeated_owner_cancel_cannot_interrupt_partial_entry_cleanup(monkeypatch):
    async def run():
        entering, cleaning, allow_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        class Opening:
            async def __aenter__(self):
                entering.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await allow_cleanup.wait()
                    calls.append("cleaned")

            async def __aexit__(self, *_):
                pytest.fail("Failed entry must not be exited")

        monkeypatch.setattr("linptech_ble.scanner.scanner_context", lambda *_: Opening())
        scanner = AdvertisementScanner()

        async def owner():
            try:
                await scanner.__aenter__()
            finally:
                await scanner.aclose()

        opening = asyncio.create_task(owner())
        await entering.wait()
        opening.cancel()
        await cleaning.wait()
        opening.cancel()
        await asyncio.sleep(0)
        assert not opening.done()
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(opening, 1)
        assert calls == ["cleaned"] and scanner.health()["closed"]

    asyncio.run(run())


def test_foreign_thread_burst_is_bounded_before_event_loop_scheduling(monkeypatch, make_record):
    import threading

    async def run():
        radio = Radio(monkeypatch)
        async with AdvertisementScanner(queue_size=2) as scanner:
            record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
            loop = asyncio.get_running_loop()
            before = len(loop._ready)

            def burst():
                for index in range(20000):
                    radio.emit(replace(record, rssi=-30 - index % 60), "foreign-thread")

            producer = threading.Thread(target=burst)
            producer.start()
            producer.join()
            assert len(loop._ready) - before == 1
            health = scanner.health()
            assert health["ingress"]["received"] == 20000
            assert health["ingress"]["queued"] == 2
            assert health["ingress"]["dropped"] == 19998
            assert health["queued"] == health["counts"]["dropped"] == 0
            first = await asyncio.wait_for(anext(scanner), 1)
            second = await asyncio.wait_for(anext(scanner), 1)
            assert [first.rssi, second.rssi] == [-48, -49]
            assert first.receiver == second.receiver == "foreign-thread"
            assert scanner.health()["counts"]["dropped"] == 0
        assert scanner.health()["ingress"]["closed"]

    asyncio.run(run())


def test_backend_that_completes_entry_after_cancellation_is_exited_without_revival(monkeypatch):
    async def run():
        entering = asyncio.Event()
        calls = []
        class LateEntry:
            async def __aenter__(self):
                entering.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    calls.append("late_enter")
                    return self
            async def __aexit__(self, *_):
                calls.append("exit")
        monkeypatch.setattr("linptech_ble.scanner.scanner_context", lambda *_: LateEntry())
        scanner = AdvertisementScanner()
        opening = asyncio.create_task(scanner.__aenter__())
        await entering.wait()
        await asyncio.wait_for(scanner.aclose(), 1)
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert calls == ["late_enter", "exit"]
        assert scanner.health()["closed"] and not scanner.health()["listening"]
        with pytest.raises(StopAsyncIteration):
            await anext(scanner)
        await scanner.aclose()
        assert calls == ["late_enter", "exit"]
    asyncio.run(run())


def test_instances_are_single_use_and_loop_affine(monkeypatch):
    scanner = AdvertisementScanner()
    async def first():
        Radio(monkeypatch)
        async with scanner:
            pass
        with pytest.raises(RuntimeError, match="new AdvertisementScanner"):
            await scanner.__aenter__()
    asyncio.run(first())
    with pytest.raises(RuntimeError, match="event loop"):
        asyncio.run(scanner.aclose())


@pytest.mark.parametrize("options", [{"queue_size": 0}, {"queue_size": True}, {"transport": object()},
    {"addresses": "AA:BB:CC:DD:EE:01"}, {"addresses": ["not-a-private-address"]}])
def test_invalid_options_fail_without_opening(options):
    with pytest.raises((ValueError, TypeError)):
        AdvertisementScanner(**options)
