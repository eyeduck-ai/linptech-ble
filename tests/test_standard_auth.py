"""Synthetic peer tests; no real credentials, BLE adapter or Xiaomi account."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import struct
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from linptech_ble.standard_auth import (
    ACK, AUTH, NOTIFY, OK, READY, UPNP, WRITE, DirectError, FramedChannel,
    SessionCipher, StandardSession, PropertyUnavailable, build_request, decode_value, encode_value, parse_properties,
)

TOKEN = bytes.fromhex("00112233445566778899aabb")
APP_RANDOM, DEVICE_RANDOM = bytes(range(16)), bytes(range(16, 32))


def material(app=APP_RANDOM):
    # Independent RFC 5869 extract/expand, rather than the implementation's HKDF class.
    prk = hmac.digest(app + DEVICE_RANDOM, TOKEN, "sha256")
    first = hmac.digest(prk, b"mible-login-info\x01", "sha256")
    return first + hmac.digest(prk, first + b"mible-login-info\x02", "sha256")


def cipher():
    key = material()
    proof = hmac.digest(key[:16], DEVICE_RANDOM + APP_RANDOM, "sha256")
    return SessionCipher.derive(TOKEN, APP_RANDOM, DEVICE_RANDOM, proof)[0]


def device_message(plain, counter=0):
    key = material()
    nonce = key[32:36] + b"\0" * 4 + struct.pack("<I", counter)
    return struct.pack("<H", counter) + AESCCM(key[:16], tag_length=4).encrypt(nonce, plain, None)


def test_mutual_authentication_and_session_keys_agree_with_independent_hkdf():
    key = material()
    proof = hmac.digest(key[:16], DEVICE_RANDOM + APP_RANDOM, "sha256")
    result, app_proof = SessionCipher.derive(TOKEN, APP_RANDOM, DEVICE_RANDOM, proof)
    assert (result.device_key, result.app_key, result.device_iv, result.app_iv) == (key[:16], key[16:32], key[32:36], key[36:40])
    assert app_proof == hmac.digest(key[16:32], APP_RANDOM + DEVICE_RANDOM, "sha256")
    assert TOKEN.hex() not in repr(result) and key.hex() not in repr(result)
    with pytest.raises(DirectError, match="認證驗證失敗"):
        SessionCipher.derive(TOKEN, APP_RANDOM, DEVICE_RANDOM, b"\0" * 32)
    with pytest.raises(DirectError):
        SessionCipher.derive(b"\0" * 16, APP_RANDOM, DEVICE_RANDOM, proof)


def test_integrity_replay_and_counter_exhaustion_are_enforced():
    session = cipher()
    packet = device_message(b"actual response", 5)
    corrupt = packet[:-1] + bytes([packet[-1] ^ 1])
    with pytest.raises(DirectError, match="AES-CCM"):
        session.decrypt(corrupt)
    assert session.rx_counter == -1
    assert session.decrypt(packet) == b"actual response"
    with pytest.raises(DirectError, match="重複或倒退"):
        session.decrypt(packet)
    with pytest.raises(DirectError):
        session.decrypt(device_message(b"older", 4))
    encrypted = session.encrypt(b"request")
    assert encrypted[:2] == b"\0\0"
    key = material()
    assert AESCCM(key[16:32], tag_length=4).decrypt(key[36:40] + b"\0" * 8, encrypted[2:], None) == b"request"
    session.tx_counter = 65535
    with pytest.raises(DirectError, match="耗盡"):
        session.encrypt(b"never wrap")


@pytest.mark.parametrize("format,value", [("bool", False), ("uint8", 0), ("uint16", 10000), ("int32", -99), ("float", 2.5), ("string", "[0,42]")])
def test_typed_value_roundtrips(format, value):
    code, raw = encode_value(value, format)
    assert decode_value(code, raw) == value


@pytest.mark.parametrize("format,value", [("bool", 1), ("uint8", True), ("uint8", 256), ("uint16", -1), ("float", float("nan")), ("string", 10)])
def test_wrong_types_and_ranges_are_rejected(format, value):
    with pytest.raises(DirectError):
        encode_value(value, format)


def test_parser_preserves_false_zero_and_high_piid_and_rejects_truncation():
    body = b"\x03\x02" + struct.pack("<BHHBB", 2, 1078, 0, 1, 0x10) + b"\0"
    body += struct.pack("<BHHBB", 6, 72, 0, 1, 0) + b"\0"
    packet = bytes([4 + len(body), 0x20, 7, 0]) + body
    values = parse_properties(packet)
    assert values[0]["value"] == 0 and values[0]["piid"] == 1078
    assert values[1]["value"] is False
    assert build_request(1, 1, 2, 1078)[6:9] == b"\x02\x36\x04"
    for malformed in (packet[:-1], packet + b"\0", packet[:4] + b"\xff" + packet[5:]):
        with pytest.raises(DirectError):
            parse_properties(malformed)
    with pytest.raises(DirectError):
        decode_value(0, b"\x02")


def test_es5_firmware_empty_property_is_not_zero_or_false():
    # Real ES5BB 2.1.2_0156 response, containing no identifiers or credentials.
    prop = parse_properties(bytes.fromhex("0d2007000301060400000000f0"))[0]
    assert (prop["siid"], prop["piid"], prop["status"]) == (6, 4, 0)
    assert prop["available"] is False and prop["value"] is None
    valid = parse_properties(bytes.fromhex("0e2006000301060b000000011000"))[0]
    assert valid["available"] is True and valid["value"] == 0


class Peer:
    """Device-side transport + independently calculated authentication/CCM."""
    def __init__(self, *, ignore_get=False, reject_set=False, stale_push=False, empty_subscribe=False):
        self.callbacks = {}
        self.frames = {}
        self.requests = []
        self.ignore_get, self.reject_set, self.stale_push = ignore_get, reject_set, stale_push
        self.empty_subscribe = empty_subscribe
        self.services = SimpleNamespace(get_characteristic=lambda _: SimpleNamespace(properties=["notify", "write-without-response"]))
        self.props = {(6, 11): (0x10, b"\x01"), (6, 4): (0xA0, b"[5,9]"), (2, 5): (0x30, b"\x3c\0")}
        self.rx, self.tx = -1, -1
        self.key = None

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid, None)

    def emit(self, uuid, value):
        self.callbacks[uuid](None, bytearray(value))

    def notify_property(self, tid, siid, piid, code, raw):
        body = bytes([4, 1, siid]) + struct.pack("<H", piid) + bytes([len(raw), code]) + raw
        self.encrypted(bytes([len(body) + 4, 0x20, tid, 0]) + body)

    def encrypted(self, plain):
        self.tx += 1
        nonce = self.key[32:36] + b"\0" * 4 + struct.pack("<I", self.tx)
        message = struct.pack("<H", self.tx) + AESCCM(self.key[:16], tag_length=4).encrypt(nonce, plain, None)
        self.last_notification = b"\0\0\x02\0" + message
        self.emit(NOTIFY, self.last_notification)

    async def write_gatt_char(self, uuid, value, **_):
        if uuid == UPNP or value in (ACK, READY, OK):
            return
        if value[:3] == b"\0\0\0":
            self.frames[uuid] = [value[3], int.from_bytes(value[4:], "little"), []]
            self.emit(uuid, READY)
            return
        kind, count, chunks = self.frames[uuid]
        assert int.from_bytes(value[:2], "little") == len(chunks) + 1
        chunks.append(value[2:])
        if len(chunks) != count:
            return
        self.emit(uuid, OK)
        data = b"".join(chunks)
        if uuid == AUTH:
            if kind == 0x0B:
                self.key = material(data)
                self.app_random = data
                self.emit(AUTH, b"\0\0\x02\x0d" + DEVICE_RANDOM)
                self.emit(AUTH, b"\0\0\x02\x0c" + hmac.digest(self.key[:16], DEVICE_RANDOM + data, "sha256"))
            elif kind == 0x0A:
                assert data == hmac.digest(self.key[16:32], self.app_random + DEVICE_RANDOM, "sha256")
                self.emit(UPNP, b"\x21\0\0\0")
            return
        assert uuid == WRITE
        counter = int.from_bytes(data[:2], "little")
        assert counter > self.rx
        self.rx = counter
        nonce = self.key[36:40] + b"\0" * 4 + struct.pack("<I", counter)
        plain = AESCCM(self.key[16:32], tag_length=4).decrypt(nonce, data[2:], None)
        self.requests.append(plain)
        if plain[4] == 0xF0:
            self.encrypted(plain)
            return
        tid, op, siid, piid = plain[2], plain[4], plain[6], int.from_bytes(plain[7:9], "little")
        if op == 1 and self.ignore_get:
            return
        if op == 0 and not self.reject_set:
            self.props[(siid, piid)] = (plain[10], plain[11:11 + plain[9]])
        code, raw = self.props[(siid, piid)]
        if op == 2 and self.empty_subscribe:
            code, raw = 0xF0, b""
        if self.stale_push and op in (1, 2):
            self.notify_property((tid - 1) & 255, siid, piid, code, b"\x03")
        self.notify_property(tid, siid, piid, code, raw)


class StartupPeer(Peer):
    """A device publishing properties while its F0 response is pending."""

    def __init__(self, *, before=(), acknowledge=True, transform_ack=None):
        super().__init__()
        self.before = before
        self.acknowledge = acknowledge
        self.transform_ack = transform_ack or (lambda frame: frame)

    def encrypted(self, plain):
        if plain[4] != 0xF0:
            return super().encrypted(plain)
        for action in self.before:
            action(self)
        if self.acknowledge:
            super().encrypted(self.transform_ack(plain))


def startup_property(peer):
    peer.notify_property(3, 6, 63, 0x10, b"\x02")


def test_session_start_preserves_early_property_after_exact_ack_without_redecrypt(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)

    async def run():
        pushes, frames = [], []

        def before_ack(peer):
            startup_property(peer)
            assert pushes == frames == []

        peer = StartupPeer(before=(before_ack,))
        session = StandardSession(peer, TOKEN, on_property=pushes.append, on_frame=frames.append)
        try:
            await session.authenticate()
            assert len(pushes) == len(frames) == 1
            assert (pushes[0]["siid"], pushes[0]["piid"], pushes[0]["value"], pushes[0]["operation"]) == (6, 63, 2, 4)
            assert session.cipher.rx_counter == 1
            assert session.diagnostics()["counts"]["op4_properties"] == 1
            assert session.diagnostics()["plaintext_operations"] == {"4": 1, "240": 1}
            # The next reply advances the same cipher; buffered startup data
            # must not be decrypted again after the acknowledgement counter.
            assert (await session.read(6, 11))["value"] == 1
            assert session.cipher.rx_counter == 2
        finally:
            await session.close()
        assert not peer.callbacks

    asyncio.run(run())


def test_session_start_skips_exact_retransmissions_and_later_ack_duplicate(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)

    async def run():
        peer = StartupPeer(before=(startup_property,) + (lambda p: p.emit(NOTIFY, p.last_notification),) * 3)
        pushes = []
        session = StandardSession(peer, TOKEN, on_property=pushes.append)
        try:
            await session.authenticate()
            assert len(pushes) == 1
            peer.emit(NOTIFY, peer.last_notification)  # Repeat the verified F0 echo.
            assert (await session.read(6, 11))["value"] == 1
            assert session.diagnostics()["counts"]["exact_retransmissions"] == 4
            assert session.error is None
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.parametrize("transform", [
    lambda plain: build_request(2, 0xF0),
    lambda plain: bytes([plain[0] + 1]) + plain[1:] + b"\0",
])
def test_session_start_rejects_mismatched_f0_without_publishing_early_data(monkeypatch, transform):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)

    async def run():
        peer = StartupPeer(before=(startup_property,), transform_ack=transform)
        pushes, frames = [], []
        session = StandardSession(peer, TOKEN, on_property=pushes.append, on_frame=frames.append)
        try:
            with pytest.raises(DirectError, match="確認內容不符"):
                await session.authenticate()
            assert pushes == frames == [] and session.reader is None
        finally:
            await session.close()
        assert not peer.callbacks and session.cipher is None and session._token == b""

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["mic", "malformed", "counter"])
def test_session_start_authenticates_and_validates_every_early_notification(monkeypatch, failure):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)

    async def run():
        def invalid(peer):
            if failure == "malformed":
                Peer.encrypted(peer, b"\x05\x20\0\0\x04")
            else:
                counter = peer.tx + 1 if failure == "mic" else peer.tx
                packet = device_message(b"\x05\x20\0\0\xf0", counter)
                if failure == "mic":
                    packet = packet[:-1] + bytes([packet[-1] ^ 1])
                peer.emit(NOTIFY, b"\0\0\x02\0" + packet)

        peer = StartupPeer(before=(startup_property, invalid))
        pushes = []
        session = StandardSession(peer, TOKEN, on_property=pushes.append)
        try:
            with pytest.raises(DirectError, match={"mic": "AES-CCM", "malformed": "操作型別", "counter": "重複或倒退"}[failure]):
                await session.authenticate()
            assert pushes == [] and session.reader is None
        finally:
            await session.close()
        assert not peer.callbacks and session.cipher is None

    asyncio.run(run())


def test_session_start_silence_uses_one_bounded_deadline_and_discards_buffer(monkeypatch):
    from linptech_ble.standard_auth import DirectTimeout
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    monkeypatch.setattr("linptech_ble.standard_auth._SESSION_START_TIMEOUT", .06)

    async def run():
        peer = StartupPeer(before=(startup_property,), acknowledge=False)
        pushes = []
        session = StandardSession(peer, TOKEN, on_property=pushes.append)
        try:
            await asyncio.wait_for(_expect_auth_timeout(session), 1.5)
            assert pushes == [] and session.reader is None
        finally:
            await session.close()
        assert not peer.callbacks

    async def _expect_auth_timeout(session):
        with pytest.raises(DirectTimeout):
            await session.authenticate()

    asyncio.run(run())


@pytest.mark.parametrize("exact_duplicates", [False, True])
def test_session_start_notification_flood_has_message_bound(monkeypatch, exact_duplicates):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    monkeypatch.setattr("linptech_ble.standard_auth._SESSION_START_MESSAGE_LIMIT", 4)

    async def run():
        flood = (lambda peer: peer.emit(NOTIFY, peer.last_notification)) if exact_duplicates else startup_property
        peer = StartupPeer(before=(startup_property,) + (flood,) * 4)
        pushes = []
        session = StandardSession(peer, TOKEN, on_property=pushes.append)
        try:
            with pytest.raises(DirectError, match="過多訊息"):
                await asyncio.wait_for(session.authenticate(), 1.5)
            assert pushes == [] and session.reader is None
            counts = session.diagnostics()["counts"]
            assert counts["verified_messages"] == (1 if exact_duplicates else 4)
            assert counts["exact_retransmissions"] == (3 if exact_duplicates else 0)
        finally:
            await session.close()
        assert not peer.callbacks

    asyncio.run(run())


def test_full_session_mutual_login_read_push_and_verified_write(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    async def run():
        peer = Peer(stale_push=True)
        pushes = []
        session = StandardSession(peer, TOKEN, on_property=pushes.append)
        try:
            await session.authenticate()
            result = await session.read(6, 11)
            assert result["value"] == 1  # A previous-TID push with value 3 is not the read reply.
            assert pushes[0]["value"] == 3
            result = await session.set(6, 11, 2, "uint8")
            assert result["value"] == 2
            assert [p[4] for p in peer.requests] == [0xF0, 1, 0, 1]
        finally:
            await session.close()
        assert not peer.callbacks and session.cipher is None and session._token == b""
    asyncio.run(run())


def test_notify_only_subscribe_accepts_null_without_get_and_separates_early_pir_push(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    class PirPeer(Peer):
        def notify_property(self, tid, siid, piid, code, raw):
            if (siid, piid, code) == (6, 18, 0xF0):
                # A live report can arrive before the matching NULL snapshot.
                pir_code, pir_raw = encode_value(-37, "int32")
                super().notify_property((tid + 3) & 65535, siid, piid, pir_code, pir_raw)
            super().notify_property(tid, siid, piid, code, raw)
    async def run():
        peer = PirPeer(empty_subscribe=True)
        peer.props[(6, 18)] = encode_value(-37, "int32")
        pushes = []
        session = StandardSession(peer, TOKEN, on_property=pushes.append)
        try:
            await session.authenticate()
            assert await session.subscribe(6, 18) is None
            assert [p[4] for p in peer.requests] == [0xF0, 2]
            assert pushes and pushes[-1]["value"] == -37
            assert session.read_ops[(6, 18)] == 2
            assert await session.command(4)
            assert peer.requests[-1][4] == 0 and peer.requests[-1][-4:] == struct.pack("<I", 4)
            with pytest.raises(DirectError, match="未開放"):
                await session.command(8)
        finally:
            await session.close()
        assert not peer.callbacks
    asyncio.run(run())


def test_notification_only_subscription_without_initial_reply_does_not_get_or_invent_value():
    from linptech_ble.standard_auth import DirectTimeout
    async def run():
        session = StandardSession(Peer(), TOKEN)
        calls = []
        async def no_snapshot(op, siid, piid):
            calls.append((op, siid, piid))
            raise DirectTimeout("synthetic notify-only field has no snapshot")
        session._exchange = no_snapshot
        assert await session.subscribe(6, 18) is None
        assert calls == [(2, 6, 18)] and session.read_ops[(6, 18)] == 2
    asyncio.run(run())


def test_subscribe_fallback_uses_only_read_operations(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    async def run():
        peer, session = Peer(ignore_get=True), None
        session = StandardSession(peer, TOKEN)
        try:
            await session.authenticate()
            assert (await session.read(2, 5))["value"] == 60
            assert session.read_op == 2
            assert [p[4] for p in peer.requests] == [0xF0, 1, 2]
        finally:
            await session.close()
    asyncio.run(run())


def test_empty_subscribe_falls_back_per_property_and_empty_stays_unavailable(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    async def run():
        peer = Peer(empty_subscribe=True)
        session = StandardSession(peer, TOKEN)
        try:
            await session.authenticate()
            assert (await session.read(6, 4, subscribe=True))["value"] == "[5,9]"
            assert session.read_ops[(6, 4)] == 1
            peer.props[(6, 4)] = (0xF0, b"")
            with pytest.raises(PropertyUnavailable):
                await session.read(6, 4, subscribe=True)
            assert all(p[4] in (0xF0, 1, 2) for p in peer.requests)
        finally:
            await session.close()
    asyncio.run(run())


def test_an_echo_without_agreeing_fresh_read_never_confirms_setting(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    async def run():
        peer = Peer(reject_set=True)
        session = StandardSession(peer, TOKEN)
        try:
            await session.authenticate()
            with pytest.raises(DirectError, match="回讀不一致"):
                await session.set(6, 11, 2, "uint8")
        finally:
            await session.close()
    asyncio.run(run())


def test_fragment_sequence_failure_and_queue_bounds():
    async def run():
        peer = Peer()
        channel = FramedChannel(peer, AUTH, timeout=.2)
        # For this test the peer's writes are just acknowledgements of incoming data.
        async def ack(*_, **__):
            pass
        peer.write_gatt_char = ack
        await channel.start()
        try:
            peer.emit(AUTH, b"\0\0\0\x0c\x02\0")
            peer.emit(AUTH, b"\x02\0out-of-order")
            with pytest.raises(DirectError, match="順序"):
                await channel.receive()
        finally:
            await channel.close()
        other = FramedChannel(peer, AUTH)
        for _ in range(513):
            other._notify(None, bytearray(b"\0"))
        assert other.incoming.qsize() == 512
        assert other.error is not None
    asyncio.run(run())


def test_long_es5_threshold_frame_and_16_bit_transaction_are_parsed():
    # Firmware sends 96 preset bytes as 287 ASCII characters: length lives in
    # 12 bits, and the string format shares the high nibble of its length word.
    value = ",".join(["78"] * 96).encode()
    body = b"\x03\x01" + struct.pack("<BHHH", 6, 45, 0, 0xA000 | len(value)) + value
    packet = struct.pack("<HH", 0x2000 | (len(body) + 4), 1025) + body
    parsed = parse_properties(packet)[0]
    assert len(packet) == 300 and packet[:2] == b"\x2c\x21"
    assert parsed["type_code"] == 0xA0 and parsed["value"] == value.decode() and parsed["tid"] == 1025
    request = build_request(1025, 0, 6, 2, value=value.decode(), format="string")
    assert request[2:4] == b"\x01\x04" and request[9:11] == b"\x1f\xa1"
    with pytest.raises(DirectError):
        parse_properties(packet[:-1])


def test_real_es5_write_ack_is_not_a_property_value():
    prop = parse_properties(bytes.fromhex("0b20270001010609000000"))[0]
    assert prop["write_ack"] and prop["status"] == 0 and prop["piid"] == 9
    assert prop["value"] is None and not prop["available"]


def test_stream_bursts_and_exact_retransmission_do_not_block_fresh_read(monkeypatch):
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    async def run():
        peer, pushes = Peer(), []
        session = StandardSession(peer, TOKEN, on_property=pushes.append)
        try:
            await session.authenticate()
            for i in range(150):
                peer.notify_property(0, 6, 4, 0xA0, f"sample-{i}".encode())
                if i == 0:
                    oldest = peer.last_notification
            peer.emit(NOTIFY, peer.last_notification)
            assert (await session.read(6, 11))["value"] == 1
            assert len(pushes) == 150 and session.error is None
            peer.emit(NOTIFY, oldest)  # No longer in the bounded ciphertext cache.
            assert (await session.read(2, 5))["value"] == 60
            assert session.obsolete_count == 1 and len(pushes) == 150
            peer.props[(6, 9)] = (0x50, b"\0" * 4)
            assert await session.command(1) is True
            with pytest.raises(DirectError, match="未開放"):
                await session.command(8)
        finally:
            await session.close()
    asyncio.run(run())


def test_obsolete_messages_must_still_authenticate_and_never_move_counter_back():
    session = cipher()
    assert session.decrypt(device_message(b"latest", 80)) == b"latest"
    assert session.verify_obsolete(device_message(b"old", 2)) is True
    assert session.rx_counter == 80
    packet = device_message(b"old", 2)
    with pytest.raises(DirectError, match="AES-CCM"):
        session.verify_obsolete(packet[:-1] + bytes([packet[-1] ^ 1]))


def test_auth_transport_diagnostics_contain_only_routing_metadata(monkeypatch):
    import json
    monkeypatch.setattr("linptech_ble.standard_auth.secrets.token_bytes", lambda _: APP_RANDOM)
    async def run():
        peer, trace = Peer(), []
        session = StandardSession(peer, TOKEN, on_transport=lambda **fields: trace.append(fields))
        try:
            await session.authenticate()
            public = json.dumps(trace)
            assert TOKEN.hex() not in public and APP_RANDOM.hex() not in public
            assert all(set(p) <= {"direction", "length", "control", "argument", "parcel"} for p in trace)
            assert any(p.get("control") == "0000000b" for p in trace)
        finally:
            await session.close()
        channel = FramedChannel(peer, AUTH, timeout=.3)
        await channel.start()
        try:
            peer.emit(AUTH, bytes.fromhex("000001050100"))
            with pytest.raises(DirectError, match="狀態 1"):
                await channel.receive()
        finally:
            await channel.close()
    asyncio.run(run())
