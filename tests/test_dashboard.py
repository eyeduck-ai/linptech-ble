"""Dashboard data integrity, HTTP boundaries and live scanner integration."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import bleak
import pytest

from linptech_ble import cli, runtime
from linptech_ble.config import DeviceConfig
from linptech_ble.dashboard import DashboardServer, DashboardState, MonitorWorker
from linptech_ble.decoder import Decoder
from linptech_ble.es5_direct import DirectController

from conftest import ADDRESS, TEST_KEY, object_bytes


def device(model="KS2BB"):
    return DeviceConfig(ADDRESS, model, "Test sensor", TEST_KEY)


def packet(*, timestamp="2026-10-22T01:00:00+00:00", status="decoded", **fields):
    return {"address": ADDRESS, "timestamp": timestamp, "status": status, "rssi": -53,
            "bindkey_verified": True, "measurements": [], "binary_sensors": [], "events": [], **fields}


def test_partial_packets_keep_per_field_timestamps_and_preserve_false_and_zero():
    state = DashboardState([device("ES5BB")])
    first = "2026-10-22T01:00:00+00:00"
    later = "2026-10-22T01:01:00+00:00"
    state.ingest(packet(timestamp=first, binary_sensors=[{"key": "occupancy", "value": False}],
                        measurements=[{"key": "illuminance", "value": 0, "unit": "lx"}]))
    state.ingest(packet(timestamp=later, measurements=[{"key": "battery", "value": 100, "unit": "%"}]))
    result = state.snapshot()["devices"][0]
    assert result["binary_sensors"]["occupancy"]["value"] is False
    assert result["binary_sensors"]["occupancy"]["updated_at"] == first
    assert result["measurements"]["illuminance"]["value"] == 0
    assert result["measurements"]["battery"]["updated_at"] == later
    assert "temperature" not in result["measurements"]


def test_duplicate_and_battery_packets_cannot_replay_a_button_event(make_record):
    config = device()
    state, decoder = DashboardState([config]), Decoder([config])
    record = make_record("KS2BB", object_bytes(0x560C, b"\x01"))
    state.ingest(decoder.decode(record))
    state.ingest(decoder.decode(record))
    state.ingest(decoder.decode(make_record("KS2BB", object_bytes(0x5003, b"\x64"), counter=2)))
    result = state.snapshot()
    buttons = [event for event in result["events"] if event["kind"] == "button"]
    assert len(buttons) == 1
    assert buttons[0]["text"] == "按鍵 · 單擊"
    assert result["devices"][0]["duplicates"] == 1
    assert result["devices"][0]["last_event"]["event_type"] == "press"


def test_close_range_is_separate_from_overall_occupancy(make_record):
    config = device("ES5BB")
    decoder, state = Decoder([config]), DashboardState([config])
    state.ingest(decoder.decode(make_record("ES5BB", object_bytes(0x484E, b"\x01"))))
    state.ingest(decoder.decode(make_record("ES5BB", object_bytes(0x5413, b"\x00"), counter=2)))
    binary = state.snapshot()["devices"][0]["binary_sensors"]
    assert binary["occupancy"]["value"] is True
    assert binary["occupancy_close_range"]["value"] is False


def test_error_does_not_replace_previous_measurements_or_repeat_error_events():
    state = DashboardState([device()])
    state.ingest(packet(measurements=[{"key": "temperature", "value": 28.9, "unit": "°C"}]))
    state.ingest(packet(status="decryption_failed", measurements=[{"key": "temperature", "value": 99}]))
    state.ingest(packet(status="decryption_failed"))
    result = state.snapshot()
    assert result["devices"][0]["measurements"]["temperature"]["value"] == 28.9
    assert result["devices"][0]["last_error"]
    assert len([event for event in result["events"] if event["kind"] == "error"]) == 1


def test_stale_radio_state_is_not_a_motion_clear_and_duplicates_do_not_refresh_values():
    state = DashboardState([device("ES5BB")], stale_after=180)
    before = state.snapshot()
    assert before["devices"][0]["freshness"] == "waiting"
    assert before["devices"][0]["binary_sensors"] == {}
    state.ingest(packet(binary_sensors=[{"key": "occupancy", "value": True}]))
    now = datetime(2026, 10, 22, 1, 4, tzinfo=timezone.utc)
    assert state.snapshot(now=now)["devices"][0]["freshness"] == "stale"
    state.ingest(packet(status="duplicate", timestamp=now.isoformat()))
    result = state.snapshot(now=now)["devices"][0]
    assert result["freshness"] == "recent"
    assert result["binary_sensors"]["occupancy"]["value"] is True
    assert result["binary_sensors"]["occupancy"]["updated_at"] == "2026-10-22T01:00:00+00:00"
    assert result["binary_sensors"]["occupancy"]["freshness"] == "stale"
    assert result["binary_sensors"]["occupancy"]["age_seconds"] == 240


def test_gatt_occupancy_updates_do_not_invent_advertisements_or_key_verification():
    state = DashboardState([device("ES5BB")])
    state.ingest_direct({"name": "occupancy_status", "value": 0, "updated_at": "2026-10-22T01:00:00+00:00"})
    result = state.snapshot(now=datetime(2026, 10, 22, 1, 0, 1, tzinfo=timezone.utc))
    sensor = result["devices"][0]
    assert sensor["binary_sensors"]["occupancy"]["value"] is False
    assert sensor["binary_sensors"]["occupancy"]["source"] == "gatt"
    assert sensor["packets"] == result["totals"]["packets"] == 0
    assert sensor["last_seen"] is None and sensor["verified"] is False
    assert result["events"][0]["text"] == "人體存在 · 無人（直連）"


def test_gatt_battery_zero_is_a_measurement_and_does_not_refresh_occupancy():
    state = DashboardState([device("ES5BB")])
    before = "2026-10-22T01:00:00+00:00"
    later = "2026-10-22T01:05:00+00:00"
    state.ingest_direct({"name": "occupancy_status", "value": 1, "updated_at": before})
    state.ingest_direct({"name": "battery_level", "value": 0, "updated_at": later})
    result = state.snapshot(now=datetime(2026, 10, 22, 1, 5, 1, tzinfo=timezone.utc))
    sensor = result["devices"][0]
    assert sensor["measurements"]["battery"] == {"key": "battery", "value": 0, "unit": "%", "device_id": None, "updated_at": later, "source": "gatt"}
    assert sensor["binary_sensors"]["occupancy"]["updated_at"] == before
    assert sensor["binary_sensors"]["occupancy"]["freshness"] == "stale"
    assert sensor["packets"] == result["totals"]["packets"] == 0
    assert sensor["verified"] is False and sensor["last_seen"] is None


def test_history_and_event_memory_are_bounded_and_outsiders_ignored():
    state = DashboardState([device()])
    state.ingest(packet(address="AA:BB:CC:DD:EE:02"))
    start = datetime(2026, 10, 22, tzinfo=timezone.utc)
    for i in range(350):
        state.ingest(packet(timestamp=(start + timedelta(seconds=i)).isoformat(),
                            measurements=[{"key": "temperature", "value": i, "unit": "°C"}]))
    result = state.snapshot()
    assert result["totals"]["packets"] == result["totals"]["events"] == 350
    assert len(result["events"]) == 300
    assert len(result["devices"][0]["history"]["temperature"]) == 180
    assert TEST_KEY not in json.dumps(result)


@contextmanager
def http_server(state):
    server = DashboardServer(state, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.closing.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(port, path, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_http_export_and_assets_never_serve_secrets_or_arbitrary_paths():
    state = DashboardState([device()])
    with http_server(state) as port:
        status, headers, data = request(port, "/api/export")
        assert status == 200 and TEST_KEY.encode() not in data
        assert "attachment" in headers["Content-Disposition"]
        assert headers["Cache-Control"] == "no-store"
        assert json.loads(data)["devices"][0]["key_configured"] is True
        for path in ("/sensors.local.json", "/../sensors.local.json", "/.private/es5bb-miot-spec.json"):
            assert request(port, path)[0] == 404
        for path in ("/", "/styles.css", "/app.js", "/icon.svg"):
            assert request(port, path)[0] == 200
        assert request(port, "/api/state", {"Host": "attacker.example"})[0] == 403
        assert request(port, "/api/state", {"Origin": "https://attacker.example"})[0] == 403


def test_sse_stream_sends_current_snapshot_on_reconnect():
    state = DashboardState([device()])
    with http_server(state) as port:
        def read_snapshot():
            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/api/stream")
            response = connection.getresponse()
            assert response.status == 200
            assert response.readline().strip() == b"event: snapshot"
            result = json.loads(response.readline().decode().removeprefix("data: "))
            response.close()
            connection.close()
            return result
        assert read_snapshot()["totals"]["packets"] == 0
        state.ingest(packet(measurements=[{"key": "temperature", "value": 30.6, "unit": "°C"}]))
        assert read_snapshot()["devices"][0]["measurements"]["temperature"]["value"] == 30.6


def post(port, payload, headers=None, path="/api/es5/direct"):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    body = json.dumps(payload).encode()
    try:
        connection.request("POST", path, body, headers={"Content-Type": "application/json", "X-Linptech-Control": "1", **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_control_api_blocks_cross_origin_forms_arbitrary_writes_and_secrets(tmp_path):
    token = "1234567890abcdef12345678"
    state = DashboardState([device("ES5BB")])
    c = DirectController(DeviceConfig(ADDRESS, "ES5BB", "Test", TEST_KEY, token), recording_dir=tmp_path)
    c.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))
    c.commands = asyncio.Queue()
    state.direct = c
    with http_server(state) as port:
        assert post(port, {"action": "connect"}, {"Origin": "https://attacker.example"})[0] == 403
        assert post(port, {"action": "connect"}, {"Host": "attacker.example"})[0] == 403
        assert post(port, {"action": "connect"}, {"X-Linptech-Control": ""})[0] == 403
        assert post(port, {"action": "connect"}, {"Content-Type": "text/plain"})[0] == 403
        assert post(port, {"action": "connect", "auth_token": token})[0] == 400
        assert post(port, {"action": "set", "property": "cmd_to_device", "value": 1})[0] == 409
        assert post(port, {"action": "set", "property": "sensitivity_level", "value": 4})[0] == 409
        assert post(port, {"action": "set", "property": "sensitivity_level", "value": 2})[0] == 409
        status, content = post(port, {"action": "connect"})
        assert status == 202 and c.commands.get_nowait()["action"] == "connect"
        assert token.encode() not in content
        public = request(port, "/api/export")[2]
        assert token.encode() not in public and TEST_KEY.encode() not in public
    replay = DashboardState([device("ES5BB")], mode="replay")
    with http_server(replay) as port:
        assert post(port, {"action": "connect"})[0] == 409


def test_live_callback_publishes_to_dashboard_and_records_before_clean_shutdown(tmp_path, monkeypatch, make_record):
    config = device("ES5BB")
    state = DashboardState([config])
    record = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    lifecycle = []

    class Scanner:
        def __init__(self, *, detection_callback):
            self.callback = detection_callback

        async def __aenter__(self):
            advertisement = SimpleNamespace(
                local_name=record.name, rssi=-55, tx_power=None,
                service_data={k: bytes.fromhex(v) for k, v in record.service_data.items()},
                manufacturer_data={}, service_uuids=record.service_uuids,
            )
            self.callback(SimpleNamespace(address=ADDRESS, name=record.name), advertisement)
            lifecycle.append("start")

        async def __aexit__(self, *_):
            lifecycle.append("stop")

    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    async def immediate(_):
        return None
    monkeypatch.setattr(runtime, "_wait_duration", immediate)
    output = tmp_path / "events.jsonl"
    asyncio.run(runtime.capture(mode="monitor", seconds=1, output=output, devices=[config],
                                quiet=True, on_record=state.ingest, on_started=lambda: state.set_scanner("listening")))
    assert lifecycle == ["start", "stop"]
    result = state.snapshot()
    assert result["scanner"]["status"] == "listening"
    assert result["devices"][0]["binary_sensors"]["occupancy"]["value"] is True
    assert len(output.read_text(encoding="utf-8").splitlines()) == 1


def test_replay_worker_uses_original_times_and_does_not_start_bluetooth(tmp_path, monkeypatch, make_record):
    record = make_record("KS2BB", object_bytes(0x560C, b"\x01"))
    path = tmp_path / "raw.jsonl"
    path.write_text(json.dumps(record.to_dict()) + "\n", encoding="utf-8")
    state = DashboardState([device()], mode="replay")
    def forbidden(**_):
        pytest.fail("Replay must not access BLE hardware")
    monkeypatch.setattr(bleak, "BleakScanner", forbidden)
    worker = MonitorWorker(state, [device()], replay_path=path)
    worker.start()
    worker.thread.join(timeout=4)
    assert not worker.thread.is_alive()
    result = state.snapshot()
    assert result["mode"] == "replay"
    assert result["scanner"]["status"] == "replay_complete"
    assert result["devices"][0]["last_seen"] == record.timestamp


def test_dashboard_cli_rejects_invalid_port_and_empty_config(tmp_path, capsys):
    assert cli.main(["dashboard", "--port", "0"]) == 1
    assert cli.main(["dashboard", "--config", str(tmp_path / "missing.json")]) == 1
    assert "設定檔內沒有裝置" in capsys.readouterr().err
