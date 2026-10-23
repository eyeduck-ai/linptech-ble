"""Application GATT recording is bounded, optional and independent of BLE."""
import asyncio
from dataclasses import replace
import json
import threading

import pytest

from linptech_ble.es5_direct import DirectController
from linptech_ble.es5_radar import encode_series
from linptech_ble.recording import AsyncRecordingWriter, RecordingConfig
from linptech_ble.sensor_direct import SensorDirectController
from test_client import KEY, Radio, add, device, until
from test_dashboard_public_client import es5_peer, sensor_peer
from test_standard_auth import TOKEN


def setup_controller(monkeypatch, model, config):
    radio = Radio(monkeypatch, peer_factory=es5_peer if model == "ES5BB" else lambda: sensor_peer(model))
    records = []
    controller_type = DirectController if model == "ES5BB" else SensorDirectController
    control = controller_type(device(model, confirmed=True), recording=config, on_value=records.append)
    return control, radio, records


@pytest.mark.parametrize("model", ["ES5BB", "KS2BB", "HS3BB"])
def test_disabled_recording_keeps_real_public_gatt_updates_and_creates_no_files(monkeypatch, tmp_path, model):
    config = RecordingConfig(enabled=False, directory=tmp_path / "disabled")
    control, radio, records = setup_controller(monkeypatch, model, config)
    async def run():
        await control._connect()
        try:
            assert control.snapshot()["authenticated"] and records
            name = {"ES5BB": "occupancy_status", "KS2BB": "temperature_unit", "HS3BB": "indicator_light"}[model]
            prop = control.client.properties[name]
            from linptech_ble.standard_auth import encode_value
            value = True if prop.format == "bool" else 1
            radio.peer.notify_property(200, prop.siid, prop.piid, *encode_value(value, prop.format))
            await until(lambda: control.snapshot()["properties"][name]["value"] == value)
            control._diagnostic("synthetic_observation")
        finally:
            await control._disconnect()
        state = control.snapshot()
        assert state["recording"] is None and state["recording_stats"]["status"] == "disabled"
        assert state["recording_stats"]["written"] == {"events": 0}
        assert not radio.peer.callbacks
    asyncio.run(asyncio.wait_for(run(), 5))
    assert not config.directory.exists() and not list(tmp_path.iterdir())


def test_gatt_observation_and_diagnostic_parts_rotate_and_apply_retention(monkeypatch, tmp_path):
    config = RecordingConfig(directory=tmp_path, max_file_bytes=512, max_parts=3, queue_size=256)
    control, _, records = setup_controller(monkeypatch, "KS2BB", config)
    async def run():
        await control._connect()
        writer = control.log
        try:
            observed = control.client.get_property("temperature_unit")
            for index in range(40):
                control._accept(replace(observed, value=index % 2, updated_at=f"2099-01-01T00:00:{index:02d}+00:00"))
                control._diagnostic("sample", index=index, detail="x" * 160)
            assert len(records) >= 40
        finally:
            await control._disconnect()
        assert writer.parts["events"] > config.max_parts
        state = control.snapshot()
        assert state["recording_stats"]["status"] == "closed"
        assert state["recording_stats"]["written"]["events"] >= 80
        assert state["recording_stats"]["dropped"] == 0
        assert state["recording"] == state["recording_stats"]["paths"]["events"]
    asyncio.run(asyncio.wait_for(run(), 5))
    parts = list(tmp_path.glob("*.events.*.jsonl"))
    assert 0 < len(parts) <= config.max_parts
    assert all(path.stat().st_size <= config.max_file_bytes for path in parts)
    for path in parts:
        data = path.read_text(encoding="utf-8")
        assert KEY not in data and TOKEN.hex() not in data
        assert all(type(json.loads(line)) is dict for line in data.splitlines())


def test_full_gatt_log_queue_does_not_block_observations_or_event_loop(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    original_write, original_close = AsyncRecordingWriter._write, AsyncRecordingWriter.close
    close_threads = []
    def stalled_write(writer, job):
        entered.set()
        assert release.wait(3)
        original_write(writer, job)
    def track_close(writer, *args, **kwargs):
        close_threads.append(threading.get_ident())
        return original_close(writer, *args, **kwargs)
    monkeypatch.setattr(AsyncRecordingWriter, "_write", stalled_write)
    monkeypatch.setattr(AsyncRecordingWriter, "close", track_close)
    config = RecordingConfig(directory=tmp_path, queue_size=2)
    control, _, records = setup_controller(monkeypatch, "KS2BB", config)
    async def run():
        loop_thread = threading.get_ident()
        try:
            await control._connect()
            assert await asyncio.to_thread(entered.wait, 1)
            observed = control.client.get_property("temperature_unit")
            for index in range(20):
                control._accept(replace(observed, value=index % 2, updated_at=f"2099-01-01T00:00:{index:02d}+00:00"))
            await asyncio.sleep(0)
            state = control.snapshot()
            assert state["status"] == "ready" and state["properties"]["temperature_unit"]["value"] == 1
            assert len(records) >= 20 and state["recording_stats"]["dropped"] > 0
            assert state["recording_stats"]["queued"] <= config.queue_size
        finally:
            release.set()
            await control._disconnect()
        assert close_threads and all(thread != loop_thread for thread in close_threads)
        assert control.snapshot()["recording_stats"]["pending"] == 0
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("stage", ["start", "write", "close"])
def test_gatt_log_io_errors_cannot_break_verified_settings_or_disconnect(monkeypatch, tmp_path, stage):
    original = getattr(AsyncRecordingWriter, "_write" if stage == "write" else stage)
    def failing(writer, *args, **kwargs):
        if stage == "close":
            original(writer, *args, **kwargs)
        raise OSError("synthetic-private-filesystem-details")
    monkeypatch.setattr(AsyncRecordingWriter, "_write" if stage == "write" else stage, failing)
    control, radio, _ = setup_controller(monkeypatch, "KS2BB", RecordingConfig(directory=tmp_path))
    async def run():
        await control._connect()
        try:
            if stage == "write":
                await until(lambda: control.snapshot()["recording_stats"]["status"] == "error")
            await control._set("temperature_unit", 1)
            await control._set("temperature_unit", 0)
            assert control.snapshot()["status"] == "ready"
            assert control.snapshot()["properties"]["temperature_unit"]["value"] == 0
            assert control.client.callback_errors == 0
        finally:
            await control._disconnect()
        state = control.snapshot()
        assert state["status"] == "disconnected" and not radio.peer.callbacks
        assert state["recording_stats"]["status"] == "error"
        assert state["recording_stats"]["error_type"] == "OSError"
        assert "synthetic-private" not in json.dumps(state)
    asyncio.run(asyncio.wait_for(run(), 5))


def test_recording_disk_error_cannot_abort_es5_calibration_restore(monkeypatch, tmp_path):
    def failing_write(*_):
        raise OSError("synthetic-private-filesystem-details")
    monkeypatch.setattr(AsyncRecordingWriter, "_write", failing_write)
    control, radio, _ = setup_controller(monkeypatch, "ES5BB", RecordingConfig(directory=tmp_path))
    names = ("motion_threshold", "presence_threshold", "radar_motion_sens", "radar_presence_sens")
    async def run():
        await control._connect()
        try:
            await until(lambda: control.snapshot()["recording_stats"]["status"] == "error")
            original = {name: control.client.get_property(name).value for name in names}
            control.cal_backup = original
            control._cal_state(phase="error", restore_required=True)
            for name in names:
                prop = control.client.properties[name]
                add(radio.peer, prop.siid, prop.piid, encode_series([9] * 32) if prop.format == "string" else 2, prop.format)
            await control._restore_calibration()
            state = control.snapshot()
            assert state["status"] == "ready" and state["calibration"]["restore_required"] is False
            assert all(state["properties"][name]["value"] == value for name, value in original.items())
            assert state["recording_stats"]["status"] == "error"
            assert control.client.callback_errors == 0
        finally:
            await control._disconnect()
        assert not radio.peer.callbacks
    asyncio.run(asyncio.wait_for(run(), 5))
