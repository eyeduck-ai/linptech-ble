"""Payload-free session diagnostics, tested with independent encrypted peers."""
from __future__ import annotations

import asyncio
import json
import struct

from linptech_ble.client import LinptechClient
from linptech_ble.standard_auth import DirectError, NOTIFY, StandardSession, encode_value, parse_properties
from test_client import KEY, Radio, device
from test_pir_routing import AckPeer, packet, until
from test_standard_auth import APP_RANDOM, Peer, TOKEN


LIMIT = (1 << 63) - 1
MARKER = "SYNTHETIC_PAYLOAD_MUST_NOT_APPEAR"


def null_packet(tid, *, op=3):
    body = bytes([op, 1, 6]) + struct.pack("<H", 18)
    if op == 3:
        body += b"\0\0"
    body += b"\0\xf0"
    return struct.pack("<HH", 0x2000 | len(body) + 4, tid) + body


class NullPeer(AckPeer):
    def __init__(self):
        super().__init__(empty_subscribe=True)
        self.props[(6, 18)] = encode_value(-39, "int32")

    def notify_property(self, tid, siid, piid, code, raw):
        if (siid, piid, code) == (6, 18, 0xF0):
            self.encrypted(null_packet(tid))
        else:
            super().notify_property(tid, siid, piid, code, raw)


def assert_private(snapshot):
    text = json.dumps(snapshot, sort_keys=True)
    for secret in (TOKEN.hex(), KEY, "AA:BB:CC:DD:EE:FF", MARKER):
        assert secret not in text
    assert "payload" not in text and "device_key" not in text and "app_iv" not in text


def test_constructor_diagnostics_are_detached_and_perform_no_io(monkeypatch):
    radio = Radio(monkeypatch)
    client = LinptechClient(device())
    before = client.diagnostics()
    assert before["session_state"] == "not_started" and before["session"] is None
    assert before["valid_op4_updates"] == {} and not before["pir_data_received"]
    assert before["stream_mode_is_acknowledgement_only"] and not radio.calls
    before["counts"]["callback_errors"] = 99
    before["valid_op4_updates"]["injected"] = {"count": 99}
    assert client.diagnostics()["counts"]["callback_errors"] == 0
    assert client.diagnostics()["valid_op4_updates"] == {}
    assert_private(client.diagnostics())


def test_ack_and_null_are_distinct_from_valid_model_checked_pir_data(monkeypatch):
    radio = Radio(monkeypatch, peer_factory=NullPeer)
    async def run():
        async with LinptechClient(device()) as client:
            await client.set_stream("pir")
            before = client.diagnostics()
            assert before["stream_mode"] == "pir" and not before["pir_data_received"]
            assert before["valid_op4_updates"] == {}
            counts = before["session"]["counts"]
            assert counts["write_acknowledgements"] == counts["command_acknowledgements"] == 2
            assert counts["null_values"] == 1 and counts["op4_properties"] == 0
            assert before["session"]["plaintext_operations"] == {"240": 1, "1": 2, "3": 1}
            for value in (-39, 0):
                radio.peer.encrypted(packet(80, 4, [(6, 18, "int32", value)]))
                await until(lambda: client.get_property("pir_energy").value == value)
            after = client.diagnostics()
            assert after["pir_data_received"]
            assert after["valid_op4_updates"]["pir_energy"]["count"] == 2
            assert after["valid_op4_updates"]["pir_energy"]["last_at"]
            # Snapshots, NULL, invalid type/range, command-status values and
            # unknown pairs must not inflate the model-valid op4 data counts.
            radio.peer.encrypted(packet(80, 3, [(6, 18, "int32", -5)]))
            radio.peer.encrypted(null_packet(80, op=4))
            failed = bytearray(packet(80, 3, [(6, 18, "int32", -6)]))
            failed[9:11] = b"\x01\0"
            radio.peer.encrypted(bytes(failed))
            radio.peer.encrypted(packet(80, 4, [(6, 18, "uint8", 1)]))
            radio.peer.encrypted(packet(80, 4, [(6, 18, "int32", 1001)]))
            radio.peer.encrypted(packet(80, 4, [(6, 9, "uint32", 4)]))
            radio.peer.encrypted(packet(80, 4, [(6, 65000, "uint8", 1)]))
            radio.peer.encrypted(packet(80, 4, [(6, 4, "string", MARKER)]))
            await until(lambda: client.get_property("motion_energy").value == MARKER)
            observed = client.diagnostics()
            assert observed["valid_op4_updates"]["pir_energy"] == after["valid_op4_updates"]["pir_energy"]
            assert set(observed["valid_op4_updates"]) == {"pir_energy", "motion_energy"}
            assert observed["counts"]["unknown_updates"] == 1
            assert observed["session"]["counts"]["nonzero_status"] == 1
            assert observed["session"]["counts"]["null_values"] == 2
            assert client.get_property("pir_energy").status == "invalid_value"
            assert_private(observed)
            # Deep mutation cannot change either layer's diagnostic state.
            observed["valid_op4_updates"]["pir_energy"]["count"] = 99
            observed["session"]["channels"]["notify"]["counts"]["raw_notifications"] = 99
            assert client.diagnostics()["valid_op4_updates"]["pir_energy"]["count"] == 2
            assert client.diagnostics()["session"] != observed["session"]
        closed = client.diagnostics()
        assert closed["session_state"] == "last_closed" and not closed["connected"]
        assert closed["stream_mode_before_close"] == "pir" and closed["pir_data_received"]
        assert not closed["session"]["authenticated"] and not closed["session"]["reader_running"]
        assert closed["session"]["counts"]["command_acknowledgements"] == 3
        assert_private(closed)
        await client.connect()
        fresh = client.diagnostics()
        assert fresh["session_state"] == "current" and not fresh["pir_data_received"]
        assert fresh["valid_op4_updates"] == {} and fresh["stream_mode_before_close"] is None
        await client.aclose()
    asyncio.run(run())


def test_raw_kind_pending_verified_and_retransmission_counters_are_visible(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    class KindPeer(Peer):
        kind = 0
        def emit(self, uuid, value):
            if uuid == NOTIFY and value[:4] == b"\0\0\x02\0":
                value = value[:3] + bytes([self.kind]) + value[4:]
            super().emit(uuid, value)
    async def run():
        peer = KindPeer()
        session = StandardSession(peer, TOKEN)
        try:
            await session.authenticate()
            assert (await session.read(6, 11))["value"] == 1
            before = session.diagnostics()
            oldest = peer.last_notification
            peer.emit(NOTIFY, oldest)
            await until(lambda: session.diagnostics()["counts"]["exact_retransmissions"] == 1)
            peer.kind = 7
            peer.notify_property(0, 6, 18, *encode_value(-39, "int32"))
            await until(lambda: bool(session.diagnostics()["channels"]["notify"]["pending_kinds"]))
            pending = session.diagnostics()
            assert pending["channels"]["notify"]["pending_kinds"] == {"7": 1}
            assert pending["channels"]["notify"]["message_kinds"]["7"] == 1
            assert pending["counts"]["verified_messages"] == before["counts"]["verified_messages"]
            assert pending["channels"]["notify"]["counts"]["raw_notifications"] == before["channels"]["notify"]["counts"]["raw_notifications"] + 2
            assert pending["error_type"] is None
            assert_private(pending)
            peer.kind = 0
            assert (await session.read(6, 11))["value"] == 1
            session.verified_packets.clear()  # Exercise authenticated obsolete, outside exact-cache handling.
            peer.emit(NOTIFY, oldest)
            await until(lambda: session.diagnostics()["counts"]["obsolete_messages"] == 1)
            session.error = DirectError(MARKER + TOKEN.hex())
            session.notify.error = DirectError(MARKER)
            failed = session.diagnostics()
            assert failed["error_type"] == failed["channels"]["notify"]["error_type"] == "DirectError"
            assert_private(failed)
        finally:
            await session.close()
    asyncio.run(run())


def test_parser_contract_is_unchanged_but_session_adds_operation_metadata(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    pure = parse_properties(packet(8, 4, [(6, 18, "int32", -39)]))[0]
    assert "operation" not in pure
    async def run():
        peer, received = Peer(), []
        session = StandardSession(peer, TOKEN, on_property=received.append)
        try:
            await session.authenticate()
            peer.encrypted(packet(8, 4, [(6, 18, "int32", -39)]))
            await until(lambda: len(received) == 1)
            assert received[0]["operation"] == 4
            assert {k: v for k, v in received[0].items() if k != "operation"} == pure
        finally:
            await session.close()
    asyncio.run(run())


def test_diagnostic_counters_saturate_and_property_maps_stay_catalog_bounded(monkeypatch):
    radio = Radio(monkeypatch)
    async def run():
        async with LinptechClient(device()) as client:
            client._session._counts["verified_messages"] = LIMIT
            client._session.notify._counts["raw_notifications"] = LIMIT
            client._valid_op4_updates["pir_energy"] = {"count": LIMIT, "last_at": None}
            client.unknown_updates = LIMIT
            radio.peer.encrypted(packet(0, 4, [(6, 18, "int32", 0)]))
            for piid in range(64000, 64020):
                radio.peer.encrypted(packet(0, 4, [(6, piid, "uint8", 1)]))
            await until(lambda: client._session._counts["properties_received"] == 21)
            snapshot = client.diagnostics()
            assert snapshot["session"]["counts"]["verified_messages"] == LIMIT
            assert snapshot["session"]["channels"]["notify"]["counts"]["raw_notifications"] == LIMIT
            assert snapshot["valid_op4_updates"]["pir_energy"]["count"] == LIMIT
            assert snapshot["counts"]["unknown_updates"] == LIMIT
            assert len(snapshot["valid_op4_updates"]) == 1 <= len(client.properties)
            assert_private(snapshot)
    asyncio.run(run())
