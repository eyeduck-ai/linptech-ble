"""Real paho client against a private loopback MQTT 3.1.1 protocol peer.

This is intentionally not a production broker. It implements the few packets
needed to exercise the bridge's network boundary without HA, hardware, external
network access, household data or an additional test dependency.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import socket
import struct
import threading
import time

import pytest

from linptech_ble.ha_mqtt import HomeAssistantBridge, MqttConfig


def mqtt_string(value: str) -> bytes:
    encoded = value.encode()
    return struct.pack(">H", len(encoded)) + encoded


def packet(header: int, body: bytes) -> bytes:
    remaining = len(body)
    encoded = bytearray([header])
    while True:
        digit, remaining = remaining % 128, remaining // 128
        encoded.append(digit | (128 if remaining else 0))
        if not remaining:
            break
    return bytes(encoded) + body


def read_string(body: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from(">H", body, offset)[0]
    start = offset + 2
    return body[start:start + length].decode(), start + length


class LoopbackPeer:
    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.connection = None
        self.stopping = threading.Event()
        self.condition = threading.Condition()
        self.send_lock = threading.Lock()
        self.messages = []
        self.subscriptions = []
        self.will = None
        self.protocol = None
        self.disconnected = False
        self.error = None
        self.thread = threading.Thread(target=self._run, name="test-mqtt-peer", daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        for connection in (self.connection, self.listener):
            if connection:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()

    def wait(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not predicate():
                if self.error:
                    raise AssertionError("Loopback MQTT protocol peer failed") from self.error
                remaining = deadline - time.monotonic()
                assert remaining > 0, "Timed out waiting for loopback MQTT protocol activity"
                self.condition.wait(min(.1, remaining))

    def publish(self, topic, payload, *, retain=False):
        self._send(packet(0x31 if retain else 0x30, mqtt_string(topic) + payload.encode()))

    def _send(self, encoded):
        with self.send_lock:
            self.connection.sendall(encoded)

    def _run(self):
        buffered = bytearray()
        try:
            while not self.stopping.is_set() and self.connection is None:
                try:
                    self.connection, _ = self.listener.accept()
                    self.connection.settimeout(.1)
                except socket.timeout:
                    continue
            while not self.stopping.is_set() and self.connection:
                try:
                    chunk = self.connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buffered.extend(chunk)
                while len(buffered) >= 2:
                    remaining, multiplier, index = 0, 1, 1
                    while index < len(buffered):
                        digit = buffered[index]
                        remaining += (digit & 127) * multiplier
                        multiplier *= 128
                        index += 1
                        if digit < 128:
                            break
                    else:
                        break
                    if len(buffered) < index + remaining:
                        break
                    header, body = buffered[0], bytes(buffered[index:index + remaining])
                    del buffered[:index + remaining]
                    self._receive(header, body)
                    with self.condition:
                        self.condition.notify_all()
        except (OSError, ValueError, struct.error) as exc:
            if not self.stopping.is_set():
                self.error = exc
                with self.condition:
                    self.condition.notify_all()

    def _receive(self, header, body):
        kind = header >> 4
        if kind == 1:  # CONNECT
            protocol, index = read_string(body, 0)
            level, flags = body[index], body[index + 1]
            self.protocol = (protocol, level, bool(flags & 2))
            index += 4  # protocol level, flags, keepalive
            _, index = read_string(body, index)  # client identifier
            if flags & 4:
                topic, index = read_string(body, index)
                payload, index = read_string(body, index)
                self.will = {"topic": topic, "payload": payload, "retain": bool(flags & 32), "qos": (flags >> 3) & 3}
            self._send(packet(0x20, b"\x00\x00"))
        elif kind == 8:  # SUBSCRIBE
            identifier, index, grants = body[:2], 2, bytearray()
            while index < len(body):
                topic, index = read_string(body, index)
                qos = body[index]
                index += 1
                self.subscriptions.append(topic)
                grants.append(qos)
            self._send(packet(0x90, identifier + bytes(grants)))
        elif kind == 3:  # PUBLISH
            topic, index = read_string(body, 0)
            qos = (header >> 1) & 3
            identifier = body[index:index + 2] if qos else None
            index += 2 if qos else 0
            self.messages.append({"topic": topic, "payload": body[index:].decode(),
                                  "retain": bool(header & 1), "qos": qos})
            if qos == 1:
                self._send(packet(0x40, identifier))
        elif kind == 12:  # PINGREQ
            self._send(packet(0xD0, b""))
        elif kind == 14:  # DISCONNECT
            self.disconnected = True


def test_actual_paho_discovery_lwt_commands_events_and_graceful_shutdown():
    pytest.importorskip("paho.mqtt.client")
    peer = LoopbackPeer()
    lock = threading.RLock()
    now = datetime.now(timezone.utc).isoformat()
    es5, ks2 = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
    def button(identity, kind):
        return {"id": identity, "timestamp": datetime.now(timezone.utc).isoformat(), "address": ks2,
                "model": "KS2BB", "kind": "button", "key": kind}
    state = {"mode": "live", "started_at": "synthetic-loopback-session", "scanner": {"status": "listening"},
             "events": [button(1, "press")], "devices": [
                 {"model": "ES5BB", "address": es5, "name": "Protocol test ES5BB", "measurements": {},
                  "binary_sensors": {"occupancy": {"value": False, "updated_at": now, "source": "advertisement"}}},
                 {"model": "KS2BB", "address": ks2, "name": "Protocol test KS2BB", "measurements": {}, "binary_sensors": {}},
             ], "direct": {"authenticated": True, "status": "ready", "token_configured": True, "busy": False,
                            "properties": {"no_one_determine_time": {"status": "read", "value": 30, "updated_at": now}}}}
    submitted = []
    def snapshot():
        with lock:
            return deepcopy(state)
    def submit(command):
        with lock:
            submitted.append(command)
        with peer.condition:
            peer.condition.notify_all()
        return "test-command-id"
    bridge = HomeAssistantBridge(MqttConfig(enabled=True, host="127.0.0.1", port=peer.port,
                                            poll_interval=.02, client_id="linptech-protocol-test"), snapshot, submit)
    try:
        bridge.start()
        peer.wait(lambda: any(m["topic"].endswith("/bridge/availability") and m["payload"] == "online" for m in peer.messages)
                  and "linptech_ble/+/command/+" in peer.subscriptions)
        assert peer.protocol == ("MQTT", 4, True)
        assert peer.will == {"topic": "linptech_ble/bridge/availability", "payload": "offline", "retain": True, "qos": 1}
        configs = [m for m in peer.messages if m["topic"].endswith("/config")]
        assert configs and all(m["retain"] and m["qos"] == 1 for m in configs)
        occupancy = next(json.loads(m["payload"]) for m in configs if m["topic"].endswith("/occupancy/config"))
        assert occupancy["unique_id"] == "linptech_aabbccddee01_occupancy"
        assert occupancy["availability_mode"] == "all"
        assert any(m["topic"].endswith("/occupancy/state") and m["payload"] == "OFF" for m in peer.messages)
        assert not any(m["topic"].endswith("/button/state") for m in peer.messages)

        command_topic = "linptech_ble/aabbccddee01/command/no_one_determine_time"
        peer.publish(command_topic, "31", retain=True)
        peer.wait(lambda: bridge.status()["commands_rejected"] == 1)
        assert submitted == []
        peer.publish(command_topic, "31")
        peer.wait(lambda: len(submitted) == 1)
        assert submitted == [{"action": "set", "property": "no_one_determine_time", "value": 31}]
        with lock:
            state["events"].insert(0, button(2, "long_press"))
        peer.wait(lambda: any(m["topic"].endswith("/button/state") for m in peer.messages))
        events = [m for m in peer.messages if m["topic"].endswith("/button/state")]
        assert len(events) == 1 and events[0]["retain"] is False and events[0]["qos"] == 0
        assert json.loads(events[0]["payload"])["event_type"] == "long_press"

        initial_configs = len(configs)
        peer.publish("homeassistant/status", "online")
        peer.wait(lambda: len([m for m in peer.messages if m["topic"].endswith("/config")]) > initial_configs)
        # Wait until the entire resync finishes rather than merely its first config.
        peer.wait(lambda: len([m for m in peer.messages if m["topic"].endswith("/bridge/availability") and m["payload"] == "online"]) >= 2)
        assert len([m for m in peer.messages if m["topic"].endswith("/button/state")]) == 1
        bridge.stop()
        peer.wait(lambda: peer.disconnected)
        availability = [m for m in peer.messages if m["topic"] == "linptech_ble/bridge/availability"]
        assert availability[-1]["payload"] == "offline" and availability[-1]["retain"] and availability[-1]["qos"] == 1
        assert bridge.status()["status"] == "stopped"
    finally:
        bridge.stop()
        peer.close()
