"""Xiaomi Standard Auth login and authenticated BLE Spec transport.

Login only: this module never provisions, registers, resets, or updates firmware.
Protocol references and third-party notices are in docs/DIRECT_PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import hmac
import math
import secrets
import struct
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def uuid16(value: int) -> str:
    return f"0000{value:04x}-0000-1000-8000-00805f9b34fb"


UPNP, AUTH, WRITE, NOTIFY = map(uuid16, (0x10, 0x19, 0x1A, 0x1B))
READY, OK, ACK = bytes.fromhex("00000101"), bytes.fromhex("00000100"), bytes.fromhex("00000300")
_COUNT_LIMIT = (1 << 63) - 1


def _increment(counts: dict[str, int], name: str, amount: int = 1) -> None:
    """Saturating diagnostics; no unbounded counters or data-bearing map keys."""
    counts[name] = min(_COUNT_LIMIT, counts.get(name, 0) + amount)


class DirectError(Exception):
    """A public error must describe a phase, never credentials or auth payloads."""


class DirectTimeout(DirectError):
    """A quiet notification channel, distinct from a malformed/auth-failed one."""


class PropertyUnavailable(DirectError):
    """The device acknowledged a property but returned an empty value."""


@dataclass(repr=False)
class SessionCipher:
    device_key: bytes = field(repr=False)
    app_key: bytes = field(repr=False)
    device_iv: bytes = field(repr=False)
    app_iv: bytes = field(repr=False)
    tx_counter: int = -1
    rx_counter: int = -1

    @classmethod
    def derive(cls, token: bytes, app_random: bytes, device_random: bytes,
               device_proof: bytes) -> tuple[SessionCipher, bytes]:
        if len(token) != 12 or len(app_random) != 16 or len(device_random) != 16 or len(device_proof) != 32:
            raise DirectError("認證資料長度不符 Standard Auth。")
        material = HKDF(algorithm=hashes.SHA256(), length=64, salt=app_random + device_random,
                        info=b"mible-login-info").derive(token)
        expected = hmac.digest(material[:16], device_random + app_random, hashlib.sha256)
        if not hmac.compare_digest(expected, device_proof):
            raise DirectError("裝置認證驗證失敗；請核對直連 token，不能使用廣播 bindkey 代替。")
        cipher = cls(material[:16], material[16:32], material[32:36], material[36:40])
        proof = hmac.digest(material[16:32], app_random + device_random, hashlib.sha256)
        return cipher, proof

    @staticmethod
    def nonce(iv: bytes, counter: int) -> bytes:
        if len(iv) != 4 or not 0 <= counter <= 65535:
            raise DirectError("會話計數器已耗盡，必須重新認證。")
        return iv + b"\0" * 4 + struct.pack("<I", counter)

    def encrypt(self, plaintext: bytes) -> bytes:
        if self.tx_counter >= 65535:
            raise DirectError("會話計數器已耗盡，請重新連線。")
        self.tx_counter += 1
        encrypted = AESCCM(self.app_key, tag_length=4).encrypt(
            self.nonce(self.app_iv, self.tx_counter), plaintext, None)
        return struct.pack("<H", self.tx_counter) + encrypted

    def decrypt(self, message: bytes) -> bytes:
        if len(message) < 6:
            raise DirectError("加密回覆不完整。")
        counter = int.from_bytes(message[:2], "little")
        if counter <= self.rx_counter:
            raise DirectError("拒絕重複或倒退的直連會話封包。")
        try:
            plain = AESCCM(self.device_key, tag_length=4).decrypt(
                self.nonce(self.device_iv, counter), message[2:], None)
        except InvalidTag:
            raise DirectError("直連回覆的 AES-CCM 驗證失敗。") from None
        self.rx_counter = counter
        return plain

    def verify_obsolete(self, message: bytes) -> bool:
        """Authenticate but never apply a delayed retransmission of old data."""
        if len(message) < 6 or int.from_bytes(message[:2], "little") > self.rx_counter:
            return False
        counter = int.from_bytes(message[:2], "little")
        try:
            AESCCM(self.device_key, tag_length=4).decrypt(
                self.nonce(self.device_iv, counter), message[2:], None)
        except InvalidTag:
            raise DirectError("過期直連封包的 AES-CCM 驗證失敗。") from None
        return True


@dataclass(repr=False)
class Message:
    kind: int
    payload: bytes = field(repr=False)


class FramedChannel:
    """Bounded, ordered framing; notifications are handled outside callbacks."""

    def __init__(self, client: Any, uuid: str, *, timeout: float = 5, trace: Any = None) -> None:
        self.client, self.uuid, self.timeout = client, uuid, timeout
        self.trace = trace
        self.incoming: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.controls: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.messages: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.pending: list[Message] = []
        self.error: DirectError | None = None
        self._counts = {name: 0 for name in ("raw_notifications", "raw_bytes", "outgoing_frames",
            "control_frames", "direct_messages", "fragmented_messages", "protocol_errors", "queue_overflows")}
        self._kinds: dict[str, int] = {}

    def diagnostics(self) -> dict[str, Any]:
        """Return routing/queue counts only, never parcels or message payloads."""
        pending: dict[str, int] = {}
        for message in self.pending:
            _increment(pending, str(message.kind))
        return {"counts": dict(self._counts), "message_kinds": dict(self._kinds),
                "pending_kinds": pending, "pending_count": len(self.pending),
                "queues": {"incoming": self.incoming.qsize(), "controls": self.controls.qsize(),
                           "messages": self.messages.qsize()},
                "reader_running": bool(self.task and not self.task.done()),
                "error_type": type(self.error).__name__ if self.error else None}

    async def start(self) -> None:
        self.task = asyncio.create_task(self._pump())
        await self.client.start_notify(self.uuid, self._notify)

    def _notify(self, _sender: Any, value: bytearray) -> None:
        _increment(self._counts, "raw_notifications")
        _increment(self._counts, "raw_bytes", len(value))
        self._trace("rx", bytes(value))
        try:
            self.incoming.put_nowait(bytes(value))
        except asyncio.QueueFull:
            _increment(self._counts, "queue_overflows")
            self.error = DirectError("直連接收佇列已滿；請重新連線。")

    async def _write(self, value: bytes) -> None:
        _increment(self._counts, "outgoing_frames")
        self._trace("tx", value)
        characteristic = self.client.services.get_characteristic(self.uuid)
        if characteristic is None:
            raise DirectError("實機缺少必要的藍牙特徵值。")
        properties = characteristic.properties
        if not any(p in properties for p in ("write", "write-without-response")):
            raise DirectError("實機特徵值不允許傳輸控制回覆。")
        response = "write-without-response" not in properties
        await self.client.write_gatt_char(self.uuid, value, response=response)
        if not response:
            # WinRT completion only confirms local buffering for Write Commands.
            # Pace parcels so the peripheral can process them before the next.
            await asyncio.sleep(.015)

    def _trace(self, direction: str, frame: bytes) -> None:
        if self.trace:
            # Routing metadata only. Never include challenge/proof parcel data.
            metadata = {"direction": direction, "length": len(frame)}
            if frame[:2] == b"\0\0":
                metadata["control"] = frame[:4].hex()
                if len(frame) == 6:
                    metadata["argument"] = int.from_bytes(frame[4:], "little")
            else:
                metadata["parcel"] = int.from_bytes(frame[:2], "little")
            self.trace(**metadata)

    async def _pump(self) -> None:
        parts: list[bytes] | None = None
        count = kind = 0
        try:
            while True:
                frame = await self.incoming.get()
                if frame in (READY, OK, ACK):
                    _increment(self._counts, "control_frames")
                    self.controls.put_nowait(frame)
                elif len(frame) == 6 and frame[:4] == b"\0\0\x01\x05":
                    code = int.from_bytes(frame[4:], "little")
                    raise DirectError(f"裝置中止藍牙傳輸（特徵 {self.uuid[4:8]}、狀態 {code}）；請單擊按鍵後重新連線。")
                elif len(frame) >= 4 and frame[:3] == b"\0\0\x02":
                    # Direct (unfragmented) auth or encrypted-data message.
                    if parts is not None:
                        raise DirectError("直連分段傳輸被另一個訊息打斷。")
                    _increment(self._counts, "direct_messages")
                    _increment(self._kinds, str(frame[3]))
                    await self._write(ACK)
                    await self.messages.put(Message(frame[3], frame[4:]))
                elif len(frame) == 6 and frame[:3] == b"\0\0\0":
                    if parts is not None:
                        raise DirectError("收到重疊的分段標頭。")
                    kind = frame[3]
                    count = int.from_bytes(frame[4:6], "little")
                    if not 1 <= count <= 256:
                        raise DirectError("直連分段數量超出支援範圍。")
                    parts = []
                    await self._write(READY)
                elif len(frame) >= 3 and int.from_bytes(frame[:2], "little") > 0:
                    if parts is None or int.from_bytes(frame[:2], "little") != len(parts) + 1:
                        raise DirectError("直連分段順序不符。")
                    parts.append(frame[2:])
                    if sum(map(len, parts)) > 4096:
                        raise DirectError("直連訊息超過 4096 bytes。")
                    if len(parts) == count:
                        _increment(self._counts, "fragmented_messages")
                        _increment(self._kinds, str(kind))
                        await self._write(OK)
                        await self.messages.put(Message(kind, b"".join(parts)))
                        parts = None
                else:
                    header = frame[:4].hex() if frame[:2] == b"\0\0" else "non-header"
                    raise DirectError(f"收到未支援的直連傳輸格式（特徵 {self.uuid[4:8]}、長度 {len(frame)}、控制標頭 {header}）。")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _increment(self._counts, "protocol_errors")
            if isinstance(error, asyncio.QueueFull):
                _increment(self._counts, "queue_overflows")
            self.error = error if isinstance(error, DirectError) else DirectError("藍牙直連傳輸失敗。")

    async def _get(self, queue: asyncio.Queue, *, timeout: float | None = None) -> Any:
        end = asyncio.get_running_loop().time() + (self.timeout if timeout is None else timeout)
        while True:
            if self.error:
                raise self.error
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise DirectTimeout("等待藍牙直連回覆逾時。")
            try:
                return await asyncio.wait_for(queue.get(), min(.2, remaining))
            except asyncio.TimeoutError:
                pass

    async def send(self, payload: bytes, kind: int = 0) -> None:
        if not payload or len(payload) > 4096:
            raise DirectError("直連傳輸長度不符。")
        async with self.lock:
            count = math.ceil(len(payload) / 18)
            await self._write(b"\0\0\0" + bytes([kind]) + struct.pack("<H", count))
            if await self._get(self.controls) != READY:
                raise DirectError("裝置未允許開始分段傳輸。")
            for index in range(count):
                await self._write(struct.pack("<H", index + 1) + payload[index * 18:(index + 1) * 18])
            if await self._get(self.controls) != OK:
                raise DirectError("裝置未確認收到完整傳輸。")

    async def receive(self, kind: int | None = None, *, timeout: float | None = None) -> Message:
        for index, message in enumerate(self.pending):
            if kind is None or message.kind == kind:
                return self.pending.pop(index)
        end = asyncio.get_running_loop().time() + (self.timeout if timeout is None else timeout)
        while True:
            message = await self._get(self.messages, timeout=max(0, end - asyncio.get_running_loop().time()))
            if kind is None or message.kind == kind:
                return message
            if len(self.pending) >= 16:
                raise DirectError("收到過多不符預期的認證訊息。")
            self.pending.append(message)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        with suppress(Exception):
            await self.client.stop_notify(self.uuid)


FORMATS = {
    "bool": (0, "<?"), "uint8": (0x10, "<B"), "int8": (0x20, "<b"),
    "uint16": (0x30, "<H"), "int16": (0x40, "<h"),
    "uint32": (0x50, "<I"), "int32": (0x60, "<i"),
    "uint64": (0x70, "<Q"), "int64": (0x80, "<q"), "float": (0x90, "<f"),
}


def encode_value(value: Any, format: str) -> tuple[int, bytes]:
    if format == "string":
        if not isinstance(value, str):
            raise DirectError("屬性需要字串。")
        return 0xA0, value.encode("utf-8")
    if format not in FORMATS:
        raise DirectError("尚未支援此屬性資料型別。")
    code, pattern = FORMATS[format]
    if format == "bool" and type(value) is not bool:
        raise DirectError("屬性需要布林值。")
    if format not in {"bool", "float"} and type(value) is not int:
        raise DirectError("屬性需要整數。")
    if format == "float" and (type(value) not in (int, float) or not math.isfinite(value)):
        raise DirectError("屬性需要有限數值。")
    try:
        return code, struct.pack(pattern, value)
    except (struct.error, OverflowError):
        raise DirectError("屬性數值超出資料型別範圍。") from None


def decode_value(type_code: int, raw: bytes) -> Any:
    if type_code == 0xA0:
        try:
            return raw.decode("utf-8").rstrip("\0")
        except UnicodeDecodeError:
            raise DirectError("字串屬性不是有效的 UTF-8。") from None
    for code, pattern in FORMATS.values():
        if type_code == code:
            if len(raw) != struct.calcsize(pattern):
                raise DirectError("屬性值長度與資料型別不符。")
            if type_code == 0 and raw not in (b"\0", b"\x01"):
                raise DirectError("裝置回覆不是有效的布林值。")
            value = struct.unpack(pattern, raw)[0]
            if isinstance(value, float) and not math.isfinite(value):
                raise DirectError("屬性回覆不是有限數值。")
            return value
    raise DirectError("裝置回覆未支援的屬性值型別。")


def build_request(tid: int, op: int, siid: int | None = None, piid: int | None = None,
                  *, value: Any = None, format: str | None = None) -> bytes:
    if op == 0xF0:
        body = b"\xf0"
    else:
        if not isinstance(siid, int) or not isinstance(piid, int) or not 1 <= siid <= 255 or not 1 <= piid <= 65535:
            raise DirectError("無效的 siid/piid。")
        body = bytes([op, 1]) + struct.pack("<BH", siid, piid)
        if op == 0:
            code, raw = encode_value(value, format or "")
            if len(raw) > 4000:
                raise DirectError("屬性字串超過目前支援的長度。")
            body += struct.pack("<H", len(raw) | (code << 8)) + raw
        elif op == 1:
            body += b"\0\0"
        elif op != 2:
            raise DirectError("尚未支援此 BLE Spec 操作。")
    if len(body) + 4 > 4095:
        raise DirectError("BLE Spec 訊息過長。")
    return struct.pack("<HH", (len(body) + 4) | 0x2000, tid & 65535) + body


def parse_properties(payload: bytes) -> list[dict[str, Any]]:
    header = int.from_bytes(payload[:2], "little")
    if len(payload) < 5 or header & 0xF000 != 0x2000 or header & 0xFFF != len(payload):
        raise DirectError("BLE Spec 回覆標頭不符。")
    if payload[4] == 0xF0 and len(payload) == 5:
        return []
    if len(payload) < 6 or payload[4] not in (1, 3, 4):
        raise DirectError("BLE Spec 回覆的操作型別尚未支援。")
    result = []
    cursor = 6
    for _ in range(payload[5]):
        if payload[4] == 1:
            if cursor + 5 > len(payload):
                raise DirectError("BLE Spec 寫入確認被截斷。")
            siid, piid, status = struct.unpack_from("<BHH", payload, cursor)
            result.append({"siid": siid, "piid": piid, "status": status,
                "value": None, "available": False, "type_code": None,
                "tid": int.from_bytes(payload[2:4], "little"), "write_ack": True})
            cursor += 5
            continue
        prefix = 7 if payload[4] == 3 else 5
        if cursor + prefix > len(payload):
            raise DirectError("BLE Spec 屬性回覆被截斷。")
        siid, piid = struct.unpack_from("<BH", payload, cursor)
        status = int.from_bytes(payload[cursor + 3:cursor + 5], "little") if payload[4] == 3 else 0
        value_header = int.from_bytes(payload[cursor + prefix - 2:cursor + prefix], "little")
        length, code = value_header & 0xFFF, (value_header >> 8) & 0xF0
        start, end = cursor + prefix, cursor + prefix + length
        if end > len(payload):
            raise DirectError("BLE Spec 屬性值被截斷。")
        raw = payload[start:end]
        empty = code == 0xF0 and length == 0
        result.append({"siid": siid, "piid": piid, "status": status,
                       "value": decode_value(code, raw) if status == 0 and not empty else None,
                       "available": not empty and status == 0,
                       "type_code": code, "tid": int.from_bytes(payload[2:4], "little")})
        cursor = end
    if cursor != len(payload):
        raise DirectError("BLE Spec 回覆含有未解析資料；保留原設定。")
    return result


class StandardSession:
    def __init__(self, client: Any, token: bytes, *, on_property: Any = None, on_frame: Any = None, on_transport: Any = None) -> None:
        if len(token) != 12:
            raise DirectError("缺少 12-byte 直連 token。")
        self.client, self._token = client, token
        self.auth = FramedChannel(client, AUTH, trace=on_transport)
        self.write = FramedChannel(client, WRITE)
        self.notify = FramedChannel(client, NOTIFY)
        self.results: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.cipher: SessionCipher | None = None
        self.tid = 0
        self.lock = asyncio.Lock()
        self.on_property = on_property
        self.on_frame = on_frame
        self.read_op: int | None = None
        self.read_ops: dict[tuple[int, int], int] = {}
        self.reader: asyncio.Task | None = None
        self.pending_reads: dict[tuple[int, int, int], asyncio.Future] = {}
        self.error: DirectError | None = None
        self.verified_packets: deque[bytes] = deque(maxlen=64)
        self.obsolete_count = 0
        self._counts = {name: 0 for name in ("upnp_notifications", "requests_attempted", "requests_sent",
            "verified_messages", "exact_retransmissions", "obsolete_messages", "parse_errors",
            "properties_received", "available_values", "null_values", "nonzero_status",
            "op4_properties", "write_acknowledgements", "command_acknowledgements", "command_value_replies")}
        self._operations: dict[str, int] = {}

    def diagnostics(self) -> dict[str, Any]:
        """Inspect bounded session metadata without I/O, addresses or secrets.

        Counts distinguish raw notifications, verified messages, NULL/status
        replies and write acknowledgements. An acknowledged vendor command is
        never evidence that its requested sensor data has actually arrived.
        """
        return deepcopy({"authenticated": self.cipher is not None,
            "reader_running": bool(self.reader and not self.reader.done()),
            "error_type": type(self.error).__name__ if self.error else None,
            "counts": self._counts, "plaintext_operations": self._operations,
            "pending_requests": len(self.pending_reads),
            "channels": {"auth": self.auth.diagnostics(), "write": self.write.diagnostics(),
                         "notify": self.notify.diagnostics()}})

    def _upnp(self, _sender: Any, data: bytearray) -> None:
        _increment(self._counts, "upnp_notifications")
        if not self.results.full():
            self.results.put_nowait(bytes(data))

    async def authenticate(self) -> None:
        for uuid in (UPNP, AUTH, WRITE, NOTIFY):
            characteristic = self.client.services.get_characteristic(uuid)
            if characteristic is None or "notify" not in characteristic.properties:
                raise DirectError("實機未提供目前實作所需的 Standard Auth / BLE Spec 特徵值。")
        await self.client.start_notify(UPNP, self._upnp)
        for channel in (self.auth, self.write, self.notify):
            await channel.start()
        characteristic = self.client.services.get_characteristic(UPNP)
        await self.client.write_gatt_char(UPNP, b"\x24\0\0\0", response="write-without-response" not in characteristic.properties)
        await asyncio.sleep(.3)
        random = secrets.token_bytes(16)
        await self.auth.send(random, 0x0B)
        device_random = (await self.auth.receive(0x0D)).payload
        device_proof = (await self.auth.receive(0x0C)).payload
        self.cipher, proof = SessionCipher.derive(self._token, random, device_random, device_proof)
        await self.auth.send(proof, 0x0A)
        try:
            reply = await asyncio.wait_for(self.results.get(), 8)
        except asyncio.TimeoutError:
            raise DirectError("等待裝置登入確認逾時。") from None
        if reply != b"\x21\0\0\0":
            raise DirectError("裝置拒絕直連登入；未改動綁定或設定。")
        tid = await self._send(0xF0)
        message = await self.notify.receive(0)
        if self.cipher.decrypt(message.payload) != build_request(tid, 0xF0):
            raise DirectError("裝置未確認 BLE Spec 會話；此韌體格式仍待驗證。")
        _increment(self._counts, "verified_messages")
        _increment(self._operations, str(0xF0))
        self.reader = asyncio.create_task(self._read_notifications())

    async def _send(self, op: int, siid: int | None = None, piid: int | None = None, **values: Any) -> int:
        if self.error:
            raise self.error
        if self.cipher is None:
            raise DirectError("尚未完成直連認證。")
        self.tid = (self.tid + 1) & 65535
        request = build_request(self.tid, op, siid, piid, **values)
        _increment(self._counts, "requests_attempted")
        await self.write.send(self.cipher.encrypt(request))
        _increment(self._counts, "requests_sent")
        return self.tid

    async def receive_properties(self, timeout: float = 4, *, report: bool = True) -> list[dict[str, Any]]:
        if self.cipher is None:
            raise DirectError("尚未完成直連認證。")
        message = await self.notify.receive(0, timeout=timeout)
        # A device can retransmit while its transport ACK is in flight. Drop an
        # exact, already authenticated ciphertext without applying it twice.
        # An old counter with different bytes still fails the cipher's check.
        if message.payload in self.verified_packets:
            _increment(self._counts, "exact_retransmissions")
            return []
        if self.cipher.verify_obsolete(message.payload):
            self.obsolete_count = min(_COUNT_LIMIT, self.obsolete_count + 1)
            _increment(self._counts, "obsolete_messages")
            return []
        plaintext = self.cipher.decrypt(message.payload)
        _increment(self._counts, "verified_messages")
        self.verified_packets.append(message.payload)
        if self.on_frame:
            self.on_frame(plaintext)
        try:
            properties = parse_properties(plaintext)
        except DirectError:
            _increment(self._counts, "parse_errors")
            raise
        operation = plaintext[4]
        _increment(self._operations, str(operation))
        for prop in properties:
            # Added by this stateful session only: parse_properties remains a
            # pure wire decoder with its existing return contract unchanged.
            prop["operation"] = operation
            _increment(self._counts, "properties_received")
            if prop.get("write_ack", False):
                _increment(self._counts, "write_acknowledgements")
                if prop["status"] == 0 and (prop["siid"], prop["piid"]) == (6, 9):
                    _increment(self._counts, "command_acknowledgements")
            elif prop["status"] == 0 and not prop["available"]:
                _increment(self._counts, "null_values")
            if prop["status"] != 0:
                _increment(self._counts, "nonzero_status")
            if prop["status"] == 0 and prop["available"]:
                _increment(self._counts, "available_values")
                if (prop["siid"], prop["piid"]) == (6, 9):
                    _increment(self._counts, "command_value_replies")
            if operation == 4:
                _increment(self._counts, "op4_properties")
        if report and self.on_property:
            for prop in properties:
                if prop["status"] == 0 and prop["available"]:
                    self.on_property(prop)
        return properties

    async def _read_notifications(self) -> None:
        """One reader owns the cipher counter, even during writes and live reports."""
        try:
            while True:
                try:
                    properties = await self.receive_properties(.25, report=False)
                except DirectTimeout:
                    continue
                for prop in properties:
                    future = self.pending_reads.get((prop["tid"], prop["siid"], prop["piid"]))
                    if future is not None and not future.done():
                        future.set_result(prop)
                    elif self.on_property and prop["status"] == 0 and prop["available"]:
                        self.on_property(prop)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error if isinstance(error, DirectError) else DirectError("直連回報處理失敗。")
            for future in self.pending_reads.values():
                if not future.done():
                    future.set_exception(self.error)

    async def _exchange(self, op: int, siid: int, piid: int, **values: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        # Install the waiter before sending: a fast device may answer while the
        # final write is being acknowledged. The caller owns self.lock.
        key = ((self.tid + 1) & 65535, siid, piid)
        future = asyncio.get_running_loop().create_future()
        self.pending_reads[key] = future
        try:
            await self._send(op, siid, piid, **values)
            try:
                return await asyncio.wait_for(future, 3)
            except asyncio.TimeoutError:
                raise DirectTimeout("等待 BLE Spec 屬性回覆逾時。") from None
        finally:
            self.pending_reads.pop(key, None)
            if not future.done():
                future.cancel()

    async def read(self, siid: int, piid: int, *, subscribe: bool = False) -> dict[str, Any]:
        async with self.lock:
            # Both are read-only operations. Some BLE Spec firmware only answers
            # SUBSCRIBE snapshots; do not try any SET as a discovery fallback.
            preferred = self.read_ops.get((siid, piid), 2 if subscribe else 1)
            modes = [preferred, 3 - preferred]
            empty_reply = False
            for op in modes:
                try:
                    prop = await self._exchange(op, siid, piid)
                except DirectTimeout:
                    continue
                if prop["status"] != 0:
                    raise DirectError(f"裝置拒絕讀取屬性 {siid}.{piid}，狀態碼 {prop['status']}。")
                if not prop["available"]:
                    empty_reply = True
                    continue
                self.read_op = op
                self.read_ops[(siid, piid)] = op
                return prop
            if empty_reply:
                raise PropertyUnavailable(f"裝置已回覆屬性 {siid}.{piid}，但沒有提供有效值。")
            raise DirectError(f"尚未收到屬性 {siid}.{piid}。")

    async def subscribe(self, siid: int, piid: int) -> dict[str, Any] | None:
        """Probe one op2 snapshot without op1 fallback, accepting NULL/silence.

        The legacy method name does not establish that this wire operation is
        the native Mi Home SDK's push-subscription registration. In particular,
        a NULL snapshot does not confirm that ES5 PIR data will be produced.
        Actual updates still use the authenticated notification reader and the
        selected fixed vendor stream command. No value is invented here.
        """
        async with self.lock:
            try:
                prop = await self._exchange(2, siid, piid)
            except DirectTimeout:
                # An op2 snapshot may stay silent until a separate stream
                # command. Do not fall back to op1 or invent a value.
                self.read_ops[(siid, piid)] = 2
                return None
            if prop["status"] != 0:
                raise DirectError(f"裝置拒絕訂閱屬性 {siid}.{piid}，狀態碼 {prop['status']}。")
            self.read_ops[(siid, piid)] = 2
            return prop if prop["available"] else None

    async def set(self, siid: int, piid: int, value: Any, format: str) -> dict[str, Any]:
        async with self.lock:
            await self._send(0, siid, piid, value=value, format=format)
        # A write is not called successful until a fresh explicit read agrees.
        observed = await self.read(siid, piid)
        if observed["value"] != value:
            raise DirectError("設定已送出，但回讀不一致；請重新讀取確認。")
        return observed

    async def command(self, value: int) -> bool:
        """Fixed vendor operations only; no factory reset, reboot or pairing."""
        if type(value) is not int or value not in {0, 1, 4, 256, 512}:
            raise DirectError("未開放此裝置命令。")
        async with self.lock:
            try:
                reply = await self._exchange(0, 6, 9, value=value, format="uint32")
            except DirectTimeout:
                # Transport ACK alone does not confirm a calibration result.
                return False
            if reply["status"] != 0:
                raise DirectError("裝置未接受雷達命令。")
            return True

    async def close(self) -> None:
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        for future in self.pending_reads.values():
            if not future.done():
                future.cancel()
        self.pending_reads.clear()
        for channel in (self.auth, self.write, self.notify):
            await channel.close()
        with suppress(Exception):
            await self.client.stop_notify(UPNP)
        self.cipher = None
        self._token = b""
