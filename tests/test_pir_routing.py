"""PIR routing diagnostics against an independent synthetic encrypted peer.

No local files, account, adapter or physical peripheral are used. These tests
check the host's routing capability; a peer generating data is not evidence
that any ES5 firmware actually emits PIR energy.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from linptech_ble.client import LinptechClient
from linptech_ble.standard_auth import NOTIFY, StandardSession, encode_value
from test_client import Radio, device
from test_standard_auth import APP_RANDOM, Peer, TOKEN


def packet(tid, op, entries):
    """Independent device-side BLE Spec framing, including multi-entry replies."""
    body = bytes([op, len(entries)])
    for siid, piid, format, value in entries:
        if op == 1:
            body += struct.pack("<BHH", siid, piid, value)
            continue
        code, raw = encode_value(value, format)
        body += struct.pack("<BH", siid, piid)
        if op == 3:
            body += b"\0\0"
        body += struct.pack("<H", len(raw) | code << 8) + raw
    return struct.pack("<HH", 0x2000 | len(body) + 4, tid) + body


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


class AckPeer(Peer):
    """Return the real ES5 op1 write ACK format instead of a value echo."""
    def notify_property(self, tid, siid, piid, code, raw):
        if (siid, piid) == (6, 9):
            self.encrypted(packet(tid, 1, [(6, 9, None, 0)]))
        else:
            super().notify_property(tid, siid, piid, code, raw)


@pytest.mark.parametrize("order", ["pir_then_ack", "ack_then_pir"])
@pytest.mark.parametrize("value", [-39, 0])
def test_same_tid_pir_and_command_ack_route_in_both_orders(monkeypatch, order, value):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    class InterleavedPeer(AckPeer):
        def notify_property(self, tid, siid, piid, code, raw):
            if (siid, piid) != (6, 9):
                return super().notify_property(tid, siid, piid, code, raw)
            if order == "pir_then_ack":
                self.encrypted(packet(tid, 4, [(6, 18, "int32", value)]))
            super().notify_property(tid, siid, piid, code, raw)
            if order == "ack_then_pir":
                self.encrypted(packet(tid, 4, [(6, 18, "int32", value)]))
    async def run():
        peer, pushed, frames = InterleavedPeer(), [], []
        session = StandardSession(peer, TOKEN, on_property=pushed.append, on_frame=frames.append)
        try:
            await session.authenticate()
            # Authentication already enables CCCD. There is deliberately NO
            # property op2 before command4 in this test.
            assert await session.command(4)
            await until(lambda: len(pushed) == 1)
            assert [(p["siid"], p["piid"], p["value"], p["type_code"]) for p in pushed] == [(6, 18, value, 0x60)]
            assert pushed[0]["tid"] == int.from_bytes(peer.requests[-1][2:4], "little")
            assert [f[4] for f in frames] == ([4, 1] if order == "pir_then_ack" else [1, 4])
            assert [request[4] for request in peer.requests] == [0xF0, 0]
            assert session.error is None and not session.pending_reads
        finally:
            await session.close()
        assert not peer.callbacks
    asyncio.run(run())


@pytest.mark.parametrize("order", ["pir_then_null", "null_then_pir"])
def test_same_tid_null_snapshot_and_live_pir_are_both_preserved_by_public_client(monkeypatch, order):
    class SnapshotPeer(AckPeer):
        def __init__(self):
            super().__init__(empty_subscribe=True)
            self.props[(6, 18)] = encode_value(-39, "int32")
        def notify_property(self, tid, siid, piid, code, raw):
            if (siid, piid, code) != (6, 18, 0xF0):
                return super().notify_property(tid, siid, piid, code, raw)
            if order == "pir_then_null":
                super().notify_property(tid, 6, 18, *encode_value(-39, "int32"))
            super().notify_property(tid, siid, piid, code, raw)
            if order == "null_then_pir":
                super().notify_property(tid, 6, 18, *encode_value(-39, "int32"))
    radio = Radio(monkeypatch, peer_factory=SnapshotPeer)
    async def run():
        received = []
        async with LinptechClient(device(), on_update=received.append) as client:
            await client.set_stream("pir")
            await until(lambda: any(p.name == "pir_energy" for p in received))
            value = client.get_property("pir_energy")
            assert value.valid and value.value == -39
            assert len([p for p in received if p.name == "pir_energy"]) == 1
            assert client._session.error is None
            assert [(p[4], p[6], int.from_bytes(p[7:9], "little"))
                    for p in radio.peer.requests if p[4] != 0xF0] == [(0, 6, 9), (2, 6, 18), (0, 6, 9)]
    asyncio.run(run())


@pytest.mark.parametrize("op", [3, 4])
@pytest.mark.parametrize("pir_first", [False, True])
def test_multi_property_command_result_and_signed_pir_route_independently(monkeypatch, op, pir_first):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    class MultiPeer(Peer):
        def notify_property(self, tid, siid, piid, code, raw):
            if (siid, piid) != (6, 9):
                return super().notify_property(tid, siid, piid, code, raw)
            entries = [(6, 9, "uint32", 4), (6, 18, "int32", -39)]
            self.encrypted(packet(tid, op, entries[::-1] if pir_first else entries))
    async def run():
        pushed = []
        session = StandardSession(MultiPeer(), TOKEN, on_property=pushed.append)
        try:
            await session.authenticate()
            # A BLE Spec message has one opcode for all entries. An op1 write
            # ACK has statuses only and cannot legally contain an int32 value;
            # op3/op4 can carry both a command result and a PIR value.
            assert await session.command(4)
            await until(lambda: len(pushed) == 1)
            assert pushed[0]["siid"] == 6 and pushed[0]["piid"] == 18
            assert pushed[0]["value"] == -39 and pushed[0]["type_code"] == 0x60
            assert session.error is None and not session.pending_reads
        finally:
            await session.close()
    asyncio.run(run())


def test_nonzero_transport_kind_remains_pending_before_property_decoder(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    class KindPeer(AckPeer):
        kind = 0
        def emit(self, uuid, value):
            if uuid == NOTIFY and value[:4] == b"\0\0\x02\0":
                value = value[:3] + bytes([self.kind]) + value[4:]
            super().emit(uuid, value)
    async def run():
        peer, pushed, frames = KindPeer(), [], []
        session = StandardSession(peer, TOKEN, on_property=pushed.append, on_frame=frames.append)
        try:
            await session.authenticate()
            assert await session.command(4)
            frame_count, counter = len(frames), session.cipher.rx_counter
            peer.kind = 7  # Synthetic unknown kind, not an assumed ES5 command.
            peer.notify_property(0, 6, 18, *encode_value(-39, "int32"))
            await until(lambda: len(session.notify.pending) == 1)
            assert session.notify.pending[0].kind == 7
            assert len(frames) == frame_count and pushed == []
            assert session.cipher.rx_counter == counter and session.error is None
            # A later normal-kind read continues working. Thus healthy
            # keepalive plus no on_frame/PIR alone cannot exclude raw frames.
            peer.kind = 0
            assert (await session.read(6, 11))["value"] == 1
            assert session.error is None and len(session.notify.pending) == 1
        finally:
            await session.close()
    asyncio.run(run())
