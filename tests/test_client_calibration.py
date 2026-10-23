"""Fixed public threshold commit against encrypted synthetic peers; no BLE."""
from __future__ import annotations

import asyncio

import pytest

from linptech_ble.client import ClientError, LinptechClient
from linptech_ble.standard_auth import DirectError, encode_value
from test_client import Radio, add, commands, device, requested


THRESHOLD_FIELDS = ("motion_threshold", "presence_threshold", "radar_motion_sens", "radar_presence_sens")
THRESHOLD_PAIRS = ((6, 2), (6, 3), (6, 14), (6, 15))
MOTION = ",".join(["0A"] * 32)
PRESENCE = ",".join(["20"] * 32)


def custom_settings(peer):
    for piid, value, format in ((2, MOTION, "string"), (3, PRESENCE, "string"),
                                (14, 0, "uint8"), (15, 0, "uint8")):
        add(peer, 6, piid, value, format)


def test_commit_is_fixed_fresh_validated_command_without_property_writes(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            custom_settings(radio.peer)
            async with client.stream("radar"):
                # Even fully populated caches cannot substitute for the four
                # fresh current-session reads preceding the commit.
                await client.refresh(THRESHOLD_FIELDS)
                before = len(radio.peer.requests)
                assert await client.commit_radar_thresholds(empty_room=True) is True
                relevant = requested(radio.peer)[-5:]
                assert relevant == [(2, *pair) for pair in THRESHOLD_PAIRS] + [(0, 6, 9)]
                sent = radio.peer.requests[before:]
                assert [p[4] for p in sent] == [2, 2, 2, 2, 0]
                assert sent[-1][10] == encode_value(512, "uint32")[0]
                assert sent[-1][11:] == encode_value(512, "uint32")[1]
                assert commands(radio.peer) == [0, 1, 512]
                for name in THRESHOLD_FIELDS:
                    assert client.get_property(name).valid
                assert client.connected and client.stream_mode == "radar"
            assert commands(radio.peer)[-1] == 0
    asyncio.run(run())


def test_commit_requires_es5_auth_explicit_empty_room_and_radar_before_reads(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        for model in ("KS2BB", "HS3BB", "MS1BB"):
            with pytest.raises(ClientError) as error:
                await LinptechClient(device(model)).commit_radar_thresholds(empty_room=True)
            assert error.value.code == "unsupported_stream"
        assert not radio.calls
        client = LinptechClient(device())
        async with client:
            custom_settings(radio.peer)
            for confirmation in (False, 1, None, "yes"):
                with pytest.raises(ClientError) as error:
                    await client.commit_radar_thresholds(empty_room=confirmation)
                assert error.value.code == "calibration_preparation_required"
            with pytest.raises(ClientError) as error:
                await client.commit_radar_thresholds(empty_room=True)
            assert error.value.code == "calibration_preparation_required"
            assert not requested(radio.peer)
        with pytest.raises(ClientError) as error:
            await client.commit_radar_thresholds(empty_room=True)
        assert error.value.code == "not_connected"
        assert 512 not in commands(radio.peer)
    asyncio.run(run())


@pytest.mark.parametrize("piid,value,format,code", [
    (2, ",".join(["0A"] * 31), "string", "invalid_value"),
    (3, ",".join(["20"] * 33), "string", "invalid_value"),
    (2, ",".join(["GG"] * 32), "string", "invalid_value"),
    (2, 42, "uint8", "invalid_value"),
    (14, 4, "uint8", "invalid_value"),
    (15, False, "bool", "invalid_value"),
    (14, 1, "uint8", "radar_not_custom"),
    (15, 3, "uint8", "radar_not_custom"),
])
def test_commit_rejects_fresh_invalid_or_noncustom_settings_despite_valid_cache(monkeypatch, piid, value, format, code):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            custom_settings(radio.peer)
            await client.set_stream("radar")
            await client.refresh(THRESHOLD_FIELDS)
            add(radio.peer, 6, piid, value, format)
            with pytest.raises(ClientError) as error:
                await client.commit_radar_thresholds(empty_room=True)
            assert error.value.code == code
            assert 512 not in commands(radio.peer)
            assert client.connected and client.stream_mode == "radar"
    asyncio.run(run())


def test_commit_null_fresh_read_is_not_allowed_to_reuse_old_threshold(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            custom_settings(radio.peer)
            await client.set_stream("radar")
            await client.refresh(THRESHOLD_FIELDS)
            radio.peer.props[(6, 3)] = (0xF0, b"")
            with pytest.raises(DirectError):
                await client.commit_radar_thresholds(empty_room=True)
            assert 512 not in commands(radio.peer) and client.connected
    asyncio.run(run())


def test_commit_does_not_claim_success_for_unacknowledged_command(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            custom_settings(radio.peer)
            await client.set_stream("radar")
            original = client._session.command
            seen = []
            async def decline_commit(value):
                seen.append(value)
                return False if value == 512 else await original(value)
            monkeypatch.setattr(client._session, "command", decline_commit)
            with pytest.raises(ClientError) as error:
                await client.commit_radar_thresholds(empty_room=True)
            assert error.value.code == "calibration_unconfirmed" and seen == [512]
            assert client.connected and client.stream_mode == "radar"
            assert requested(radio.peer)[-4:] == [(2, *pair) for pair in THRESHOLD_PAIRS]
    asyncio.run(run())


def test_commit_cannot_borrow_another_tasks_stream_context(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            custom_settings(radio.peer)
            entered, release = asyncio.Event(), asyncio.Event()
            async def owner():
                async with client.stream("radar"):
                    entered.set()
                    await release.wait()
            owned = asyncio.create_task(owner())
            await entered.wait()
            before = list(radio.peer.requests)
            try:
                with pytest.raises(ClientError) as error:
                    await client.commit_radar_thresholds(empty_room=True)
                assert error.value.code == "stream_in_use" and radio.peer.requests == before
            finally:
                release.set()
                await owned
            assert 512 not in commands(radio.peer)
    asyncio.run(run())


def test_queued_commit_rechecks_mode_and_session_before_any_fresh_reads(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            custom_settings(radio.peer)
            await client.set_stream("radar")
            await client._operation_lock.acquire()
            switching = asyncio.create_task(client.set_stream("off"))
            await asyncio.sleep(0)
            committing = asyncio.create_task(client.commit_radar_thresholds(empty_room=True))
            await asyncio.sleep(0)
            client._operation_lock.release()
            await switching
            with pytest.raises(ClientError) as error:
                await committing
            assert error.value.code == "calibration_preparation_required"
            assert not any((siid, piid) in THRESHOLD_PAIRS for _, siid, piid in requested(radio.peer))
            await client.set_stream("radar")
            await client._operation_lock.acquire()
            old_commit = asyncio.create_task(client.commit_radar_thresholds(empty_room=True))
            await asyncio.sleep(0)
            await client.aclose()
            await client.connect()
            client._operation_lock.release()
            with pytest.raises(ClientError) as error:
                await old_commit
            assert error.value.code == "session_changed"
            assert not requested(radio.peer) and client.connected
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_commit_closes_before_sending_512(monkeypatch, cancel):
    radio = Radio(monkeypatch)
    async def run():
        client = LinptechClient(device(), operation_timeout=5)
        async with client:
            custom_settings(radio.peer)
            await client.set_stream("radar")
            client.operation_timeout = 5 if cancel else .04
            session = client._session
            started = asyncio.Event()
            async def stalled(*args, **kwargs):
                started.set()
                await asyncio.Event().wait()
            monkeypatch.setattr(session, "read", stalled)
            committing = asyncio.create_task(client.commit_radar_thresholds(empty_room=True))
            await started.wait()
            if cancel:
                committing.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
                await committing
            assert 512 not in commands(radio.peer)
            assert not client.connected and radio.closed == 1
            assert session.cipher is None and not radio.peer.callbacks
    asyncio.run(run())
