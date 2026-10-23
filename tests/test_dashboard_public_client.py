"""Dashboard adapters must exercise the public SDK, not a second wire stack.

Only the transport is replaced. Standard Auth, AES-CCM, typed properties and
read/write verification use the real LinptechClient against a synthetic peer.
"""
from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
import inspect
import json
from pathlib import Path

import pytest

from linptech_ble.client import LinptechClient
from linptech_ble.es5_direct import DirectController
from linptech_ble.es5_radar import EmptyRoomScan, encode_series
from linptech_ble.property_catalog import PROPERTIES
from linptech_ble.sensor_direct import SensorDirectController
from linptech_ble.standard_auth import AUTH, DirectError, encode_value
from test_client import KEY, Radio, add, commands, device, requested, until
from test_standard_auth import Peer, TOKEN


@pytest.mark.parametrize("module", ["es5_direct", "sensor_direct", "es5_worker", "dashboard"])
def test_application_adapters_do_not_own_wire_or_security_implementation(module):
    path = Path(__file__).parents[1] / "linptech_ble" / (module + ".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_imports = {"StandardSession", "SessionCipher", "FramedChannel", "BleakClient",
                         "connected_client", "encode_value", "parse_properties", "uuid16"}
    forbidden_attributes = {"_session", "read_gatt_char", "write_gatt_char", "start_notify", "stop_notify", "authenticate"}
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            violations.extend((node.lineno, item.name) for item in node.names if item.name in forbidden_imports)
        elif isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
            violations.append((node.lineno, node.attr))
    assert not violations, f"{module} bypasses the public client: {violations}"


def es5_peer():
    peer = Peer()
    for prop in PROPERTIES:
        if prop.name in {"motion_energy", "presence_energy", "motion_threshold", "presence_threshold",
                         "default_motion_threshold", "default_presence_threshold"}:
            value = encode_series([5] * 32)
        elif prop.name == "human_sensor_bitmaps":
            value = "0,0,0,0,0,0,0,1,0"
        elif prop.format == "string":
            value = "synthetic"
        elif prop.format == "bool":
            value = False
        elif prop.allowed_values:
            value = prop.allowed_values[0]
        elif prop.minimum is not None:
            value = prop.minimum
        elif prop.format == "float":
            value = 12.5
        else:
            value = 0
        add(peer, prop.siid, prop.piid, value, prop.format)
    return peer


def sensor_peer(model):
    peer = Peer()
    if model == "KS2BB":
        add(peer, 6, 1, 0, "uint8")
        add(peer, 6, 2, 0, "uint8")
    else:
        for siid, piid, value, format in ((2, 2, 30, "uint16"), (2, 1053, 10, "uint16"),
                                        (4, 1, False, "bool"), (5, 1, 0, "uint16"),
                                        (5, 2, 0, "uint16"), (5, 3, True, "bool")):
            add(peer, siid, piid, value, format)
    return peer


def test_es5_controller_real_public_client_updates_streams_and_invalidates_cache(monkeypatch, tmp_path):
    radio = Radio(monkeypatch, peer_factory=es5_peer)
    records = []
    control = DirectController(device(), recording_dir=tmp_path, on_value=records.append)

    async def run():
        await control._connect()
        try:
            assert isinstance(control.client, LinptechClient)
            state = control.snapshot()
            assert state["authenticated"] and state["status"] == "ready"
            assert state["properties"]["occupancy_status"]["value"] == 0
            assert state["properties"]["interest_enable"]["value"] is False
            assert commands(radio.peer)[-1] == 1
            radio.peer.notify_property(200, 2, 1078, *encode_value(1, "uint8"))
            await until(lambda: control.snapshot()["properties"]["occupancy_status"]["value"] == 1)
            await control._set("no_one_determine_time", 31)
            assert control.snapshot()["properties"]["no_one_determine_time"]["value"] == 31
            await control._select_stream("pir")
            assert control.snapshot()["streaming"]["mode"] == "pir"
            radio.peer.notify_property(201, 6, 18, *encode_value(-27, "int32"))
            await until(lambda: control.snapshot()["properties"]["pir_energy"]["value"] == -27)
            assert control.client.diagnostics()["pir_data_received"]
            assert KEY not in json.dumps(control.snapshot())
            assert TOKEN.hex() not in json.dumps(control.snapshot())
        finally:
            await control._disconnect()
        state = control.snapshot()
        assert not state["authenticated"]
        assert state["properties"]["occupancy_status"]["value"] == 1
        assert state["properties"]["occupancy_status"]["status"] == "cached"
        assert commands(radio.peer)[-1] == 0
        assert not radio.peer.callbacks and radio.closed == 1
    asyncio.run(asyncio.wait_for(run(), 5))
    assert any(row["name"] == "occupancy_status" and row["value"] == 1 for row in records)


def test_ks2_controller_uses_verified_public_write_and_ignores_old_client_callback(monkeypatch, tmp_path):
    radio = Radio(monkeypatch, peer_factory=lambda: sensor_peer("KS2BB"))
    control = SensorDirectController(device("KS2BB"), recording_dir=tmp_path)

    async def run():
        await control._connect()
        old_client = control.client
        assert isinstance(old_client, LinptechClient)
        old_callback = old_client.on_update
        assert old_callback is not None
        await control._set("temperature_unit", 1)
        assert control.snapshot()["properties"]["temperature_unit"]["value"] == 1
        operations = [op for op, siid, piid in requested(radio.peer) if (siid, piid) == (6, 2)]
        assert 0 in operations and len(operations) >= 4
        await control._disconnect()
        assert control.snapshot()["properties"]["temperature_unit"]["status"] == "cached"
        await control._connect()
        try:
            before = control.snapshot()["properties"]["temperature_unit"]
            stale = replace(old_client.get_property("temperature_unit"), value=1, status="read",
                            updated_at="2099-01-01T00:00:00+00:00", source="gatt")
            result = old_callback(stale)
            if inspect.isawaitable(result):
                await result
            await asyncio.sleep(0)
            assert control.snapshot()["properties"]["temperature_unit"] == before
        finally:
            await control._disconnect()
        assert radio.closed == 2
        assert all(not commands(peer) for peer in radio.peers)
    asyncio.run(asyncio.wait_for(run(), 5))


def test_hs3_controller_keeps_invalid_firmware_values_and_rechecks_before_write(monkeypatch, tmp_path):
    radio = Radio(monkeypatch, peer_factory=lambda: sensor_peer("HS3BB"))
    records = []
    control = SensorDirectController(device("HS3BB", confirmed=True), recording_dir=tmp_path,
                                     on_value=records.append)

    async def run():
        await control._connect()
        try:
            state = control.snapshot()
            assert state["status"] == "ready" and state["model_confirmed"]
            assert state["properties"]["indicator_light"]["value"] is False
            assert state["properties"]["no_motion_timeout_1"]["status"] == "invalid_value"
            assert state["properties"]["no_motion_timeout_1"]["value"] == 0
            assert not any(row["name"] == "no_motion_timeout_1" for row in records)
            add(radio.peer, 2, 2, 25, "uint16")
            before = len(radio.peer.requests)
            with pytest.raises(DirectError):
                await control._set("detection_interval", 20)
            assert not any(op == 0 for op, _, _ in requested_from(radio.peer.requests[before:]))
            assert control.snapshot()["properties"]["detection_interval"]["status"] == "invalid_value"
        finally:
            await control._disconnect()
        assert not commands(radio.peer)
    asyncio.run(asyncio.wait_for(run(), 5))


def requested_from(messages):
    return [(p[4], p[6], int.from_bytes(p[7:9], "little")) for p in messages if p[4] != 0xF0]


def test_hs3_controller_model_confirmation_and_ms1_profile_never_write(monkeypatch, tmp_path):
    radio = Radio(monkeypatch, peer_factory=lambda: sensor_peer("HS3BB"))
    control = SensorDirectController(device("HS3BB"), recording_dir=tmp_path)

    async def run():
        await control._connect()
        try:
            before = len(radio.peer.requests)
            with pytest.raises(DirectError):
                await control._set("indicator_light", True)
            assert len(radio.peer.requests) == before
        finally:
            await control._disconnect()
        before = len(radio.calls)
        ms1 = SensorDirectController(device("MS1BB"), recording_dir=tmp_path)
        with pytest.raises(DirectError):
            await ms1._connect()
        assert len(radio.calls) == before
    asyncio.run(asyncio.wait_for(run(), 5))


def test_cancel_controller_auth_releases_public_client_transport(monkeypatch, tmp_path):
    entered = asyncio.Event()

    class SlowPeer(Peer):
        async def write_gatt_char(self, uuid, value, **kwargs):
            if uuid == AUTH:
                entered.set()
                await asyncio.Event().wait()
            return await super().write_gatt_char(uuid, value, **kwargs)

    radio = Radio(monkeypatch, peer_factory=SlowPeer)
    control = DirectController(device(), recording_dir=tmp_path)

    async def run():
        task = asyncio.create_task(control._connect())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await control._disconnect()
        assert not control.snapshot()["authenticated"]
        assert radio.closed == 1 and not radio.peer.callbacks
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("action", ["calibration_apply", "calibration_restore"])
def test_cancel_partial_threshold_operation_keeps_unresolved_backup_after_sdk_closes(monkeypatch, tmp_path, action):
    second_write = asyncio.Event()

    class HoldSecondThresholdPeer(Peer):
        def __init__(self):
            super().__init__()
            self.props = es5_peer().props

        async def write_gatt_char(self, uuid, value, **kwargs):
            before = len(self.requests)
            result = await super().write_gatt_char(uuid, value, **kwargs)
            if len(self.requests) > before:
                request = self.requests[-1]
                if request[4] == 0 and (request[6], int.from_bytes(request[7:9], "little")) == (6, 3):
                    # The first setting and this setting reached the device;
                    # cancellation must not claim that nothing was applied.
                    second_write.set()
                    await asyncio.Event().wait()
            return result

    radio = Radio(monkeypatch, peer_factory=HoldSecondThresholdPeer)
    backups = []
    control = DirectController(device(), recording_dir=tmp_path, on_backup=backups.append)

    async def run():
        await control._connect()
        names = ("motion_threshold", "presence_threshold", "radar_motion_sens", "radar_presence_sens")
        original = {name: (await control.client.read(name)).value for name in names}
        proposed = {"motion_threshold": encode_series([6] * 32), "presence_threshold": encode_series([7] * 32)}
        if action == "calibration_apply":
            control.scan = EmptyRoomScan(original, asyncio.get_running_loop().time())
            control._cal_state(phase="preview", proposed=proposed)
        else:
            control.cal_backup = original.copy()
            for name, value in proposed.items():
                prop = control.client.properties[name]
                add(radio.peer, prop.siid, prop.piid, value, prop.format)
            control._cal_state(phase="error", backup_available=True, restore_required=True)
        task = asyncio.create_task(control._calibration_action(action))
        await asyncio.wait_for(second_write.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backups == ([original] if action == "calibration_apply" else [])
        assert control.cal_backup == original
        state = control.snapshot()
        assert state["calibration"]["restore_required"] is True
        assert state["calibration"]["phase"] == "error"
        assert not control.client.connected
        expected = proposed["motion_threshold"] if action == "calibration_apply" else original["motion_threshold"]
        assert radio.peer.props[(6, 2)] == encode_value(expected, "string")
        await control._disconnect()
        state = control.snapshot()
        assert state["calibration"]["restore_required"] is True
        assert state["calibration"]["phase"] == "error"
        assert not state["authenticated"] and radio.closed == 1
        assert not radio.peer.callbacks
    asyncio.run(asyncio.wait_for(run(), 5))
