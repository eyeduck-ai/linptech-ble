"""SDK teardown regressions using tasks/queues only, never a BLE adapter."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from linptech_ble.standard_auth import AUTH, NOTIFY, UPNP, WRITE, DirectError, FramedChannel, StandardSession


class NotifyClient:
    def __init__(self, *, fail=None, block=None):
        self.fail, self.block = fail, block
        self.callbacks, self.stopped = {}, []
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.stopped.append(uuid)
        if uuid == self.block:
            self.entered.set()
            await self.release.wait()
        if uuid == self.fail:
            raise RuntimeError("SYNTHETIC_BACKEND_DETAIL_MUST_NOT_ESCAPE")
        self.callbacks.pop(uuid, None)


async def started_session(client):
    session = StandardSession(client, b"synthetic123")
    session.cipher = object()  # No authentication/crypto or wire I/O is needed.
    await client.start_notify(UPNP, session._upnp)
    for channel in (session.auth, session.write, session.notify):
        await channel.start()
    pending = asyncio.get_running_loop().create_future()
    session.pending_reads[(1, 6, 2)] = pending
    return session, pending


def test_queue_completion_and_reader_cancellation_cannot_be_swallowed():
    async def run():
        channel = FramedChannel(SimpleNamespace(), NOTIFY)
        reader = None
        received = []
        class ReadyOnce:
            first = True
            async def get(self):
                if self.first:
                    self.first = False
                    # With wait_for(queue.get()), the child completes before
                    # its reader is cancelled: Python 3.11 returned the ready
                    # result and lost cancellation. Direct await preserves it.
                    asyncio.get_running_loop().call_soon(reader.cancel)
                    return b"ready"
                await asyncio.Event().wait()
        queue = ReadyOnce()
        async def consume():
            while True:
                received.append(await channel._get(queue))
        reader = asyncio.create_task(consume())
        try:
            done, _ = await asyncio.wait((reader,), timeout=.5)
            assert reader in done and reader.cancelled()
            assert received == [b"ready"]
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    asyncio.run(run())


def test_close_recancels_reader_attempts_all_notifications_and_preserves_protocol_failure():
    async def run():
        client = NotifyClient(fail=AUTH)
        session, pending = await started_session(client)
        original_error = session.error = DirectError("Existing authenticated protocol failure.")
        started, swallowed = asyncio.Event(), asyncio.Event()
        async def reader_loop():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                swallowed.set()
                await asyncio.Event().wait()
        reader = session.reader = asyncio.create_task(reader_loop())
        await started.wait()
        with pytest.raises(DirectError) as error:
            await session.close()
        assert "SYNTHETIC_BACKEND_DETAIL" not in str(error.value)
        assert swallowed.is_set() and reader.done() and reader.cancelled()
        assert set(client.stopped) == {AUTH, WRITE, NOTIFY, UPNP}
        assert set(client.callbacks) == {AUTH}  # Failed unsubscribe is reported.
        assert all(c.task.done() for c in (session.auth, session.write, session.notify))
        assert pending.cancelled() and not session.pending_reads
        assert session.reader is None and session.cipher is None and session._token == b""
        assert session.error is original_error
    asyncio.run(run())


@pytest.mark.parametrize("unsubscribe_fails", [False, True])
def test_cancelling_close_finishes_every_subscription_before_propagating_cancellation(unsubscribe_fails):
    async def run():
        client = NotifyClient(block=AUTH, fail=AUTH if unsubscribe_fails else None)
        session, pending = await started_session(client)
        closing = asyncio.create_task(session.close())
        await client.entered.wait()
        closing.cancel()
        client.release.set()
        done, _ = await asyncio.wait((closing,), timeout=1)
        assert closing in done
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert set(client.stopped) == {AUTH, WRITE, NOTIFY, UPNP}
        assert set(client.callbacks) == ({AUTH} if unsubscribe_fails else set())
        assert all(c.task.done() for c in (session.auth, session.write, session.notify))
        assert pending.cancelled() and not session.pending_reads
        assert session.cipher is None and session._token == b""
    asyncio.run(run())


def test_uncooperative_reader_has_bounded_cleanup_and_cannot_retain_credentials():
    async def run():
        client = NotifyClient()
        session, pending = await started_session(client)
        started, release = asyncio.Event(), asyncio.Event()
        async def stubborn():
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        reader = session.reader = asyncio.create_task(stubborn())
        await started.wait()
        closing = asyncio.create_task(session.close())
        try:
            done, _ = await asyncio.wait((closing,), timeout=1)
            assert closing in done
            with pytest.raises(DirectError):
                await closing
            assert not client.callbacks
            assert pending.cancelled() and not session.pending_reads
            assert session.cipher is None and session._token == b""
            assert all(c.task.done() for c in (session.auth, session.write, session.notify))
        finally:
            release.set()
            await reader
    asyncio.run(run())
