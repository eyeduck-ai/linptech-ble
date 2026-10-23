"""Replay and scanner lifecycle coverage; a fake scanner never touches Bluetooth."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import bleak
import pytest

from linptech_ble import cli, runtime
from linptech_ble.config import DeviceConfig, load_config, save_config
from linptech_ble.models import MIBEACON_UUID

from conftest import ADDRESS, TEST_KEY, object_bytes


def records_from(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def scanner_factory(monkeypatch):
    instances = []
    pending_records = []

    class FakeScanner:
        def __init__(self, *, detection_callback):
            self.callback = detection_callback
            self.entered = False
            self.exited = False
            instances.append(self)

        async def __aenter__(self):
            self.entered = True
            for record in pending_records:
                device = SimpleNamespace(address=record.address, name=record.name)
                advertisement = SimpleNamespace(
                    local_name=record.name, rssi=record.rssi, tx_power=record.tx_power,
                    service_data={key: bytes.fromhex(value) for key, value in record.service_data.items()},
                    manufacturer_data={int(key): bytes.fromhex(value) for key, value in record.manufacturer_data.items()},
                    service_uuids=record.service_uuids,
                )
                self.callback(device, advertisement)
            return self

        async def __aexit__(self, *_):
            self.exited = True

    async def immediately(_seconds):
        return None

    monkeypatch.setattr(bleak, "BleakScanner", FakeScanner)
    monkeypatch.setattr(runtime, "_wait_duration", immediately)
    return pending_records, instances


def test_scan_writes_every_callback_and_closes_scanner(tmp_path, make_record, scanner_factory):
    pending, instances = scanner_factory
    packet = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    pending.extend([packet, packet, packet])
    output = tmp_path / "scan.jsonl"
    stats = asyncio.run(runtime.capture(mode="scan", seconds=1, output=output))
    assert len(records_from(output)) == 3
    assert stats["written"] == 3
    assert stats["errors"] == 0
    assert instances[0].entered and instances[0].exited


def test_configured_only_drops_other_linptech_candidates(tmp_path, make_record, scanner_factory):
    pending, _ = scanner_factory
    pending.extend([
        make_record("MS1BB", object_bytes(0x4A12, b"\x01")),
        make_record("KS2BB", object_bytes(0x560C, b"\x01"), address="AA:BB:CC:DD:EE:02"),
    ])
    output = tmp_path / "events.jsonl"
    stats = asyncio.run(runtime.capture(
        mode="monitor", seconds=1, output=output,
        devices=[DeviceConfig(ADDRESS, "MS1BB", "Door", TEST_KEY)],
        configured_only=True,
    ))
    assert stats["received"] == 2
    assert stats["selected"] == 1
    assert [row["address"] for row in records_from(output)] == [ADDRESS]


def test_monitor_retains_raw_repeats_but_deduplicates_events(tmp_path, make_record, scanner_factory):
    pending, instances = scanner_factory
    packet = make_record("KS2BB", object_bytes(0x560C, b"\x01"))
    pending.extend([packet, packet, packet])
    output, raw_output = tmp_path / "events.jsonl", tmp_path / "raw.jsonl"
    devices = [DeviceConfig(ADDRESS, "KS2BB", "Synthetic button", TEST_KEY)]
    stats = asyncio.run(runtime.capture(mode="monitor", seconds=1, output=output, raw_output=raw_output, devices=devices))
    decoded = records_from(output)
    assert [row["status"] for row in decoded] == ["decoded", "duplicate", "duplicate"]
    assert sum(len(row["events"]) for row in decoded) == 1
    assert len(records_from(raw_output)) == stats["raw_written"] == 3
    assert TEST_KEY not in output.read_text(encoding="utf-8")
    assert instances[0].exited


def test_short_mibeacon_does_not_stop_scan(tmp_path, make_record, scanner_factory):
    pending, instances = scanner_factory
    good = make_record("ES5BB", object_bytes(0x484E, b"\x01"))
    pending.extend([replace(good, service_data={MIBEACON_UUID: "5859"}), good])
    output = tmp_path / "short-header.jsonl"
    stats = asyncio.run(runtime.capture(mode="scan", seconds=1, output=output))
    assert stats["statuses"] == {"malformed": 1, "encrypted": 1}
    assert stats["errors"] == 0
    assert len(records_from(output)) == 2
    assert instances[0].exited


def test_monitor_displays_two_distinct_button_events_without_throttling(tmp_path, make_record, scanner_factory, capsys):
    pending, _ = scanner_factory
    pending.extend([
        make_record("KS2BB", object_bytes(0x560C, b"\x01"), counter=1),
        make_record("KS2BB", object_bytes(0x560D, b"\x01"), counter=2),
    ])
    output = tmp_path / "button-events.jsonl"
    asyncio.run(runtime.capture(mode="monitor", seconds=1, output=output, devices=[DeviceConfig(ADDRESS, "KS2BB", "Synthetic button", TEST_KEY)]))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert [row["events"][0]["event_type"] for row in lines] == ["press", "double_press"]


def test_existing_output_is_preserved_before_scanner_starts(tmp_path, scanner_factory):
    _, instances = scanner_factory
    output = tmp_path / "existing.jsonl"
    output.write_text("do not overwrite\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        asyncio.run(runtime.capture(mode="scan", seconds=1, output=output))
    assert output.read_text(encoding="utf-8") == "do not overwrite\n"
    assert instances == []


def test_scan_callback_io_failure_stops_and_closes_scanner(tmp_path, make_record, scanner_factory, monkeypatch):
    pending, instances = scanner_factory
    pending.extend([make_record("MS1BB", object_bytes(0x4A12, b"\x01"))] * 3)

    def fail_write(*_):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(runtime, "_write_json", fail_write)
    with pytest.raises(RuntimeError, match="synthetic disk full"):
        asyncio.run(runtime.capture(mode="scan", seconds=1, output=tmp_path / "scan.jsonl"))
    assert instances[0].exited


def test_replay_synthetic_demo_without_bluetooth(tmp_path, monkeypatch):
    def no_hardware(*args, **kwargs):
        pytest.fail("Offline replay must not create a Bluetooth scanner")

    monkeypatch.setattr(bleak, "BleakScanner", no_hardware)
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "replay.jsonl"
    stats = runtime.replay(root / "examples/demo.raw.jsonl", load_config(root / "examples/demo.secrets.json"), output)
    rows = records_from(output)
    assert stats["records"] == 8
    assert stats["statuses"] == {"decoded": 8}
    assert {row["model"] for row in rows} == {"ES5BB", "MS1BB", "KS2BB"}
    assert {event["event_type"] for row in rows for event in row["events"]} == {"press", "double_press", "long_press"}
    assert TEST_KEY not in output.read_text(encoding="utf-8")


def test_replay_reports_bad_lines_and_continues(tmp_path, make_record):
    input_path, output = tmp_path / "input.jsonl", tmp_path / "output.jsonl"
    packet = make_record("MS1BB", object_bytes(0x4A12, b"\x01"), encrypted=False)
    input_path.write_text("invalid-json\n[]\n" + json.dumps(packet.to_dict()) + "\n", encoding="utf-8")
    stats = runtime.replay(input_path, [], output)
    rows = records_from(output)
    assert [row["status"] for row in rows] == ["malformed", "malformed", "decoded"]
    assert stats["malformed_lines"] == 2


def test_replay_wrongly_supplied_secrets_file_never_echoes_key(tmp_path):
    input_path, output = tmp_path / "secrets.jsonl", tmp_path / "result.jsonl"
    input_path.write_text(json.dumps({"version": 1, "devices": [{"bindkey": TEST_KEY}]}) + "\n", encoding="utf-8")
    stats = runtime.replay(input_path, [], output)
    assert stats["malformed_lines"] == 1
    assert records_from(output)[0]["status"] == "malformed"
    assert TEST_KEY not in output.read_text(encoding="utf-8")


def test_replay_decoded_events_file_is_rejected_as_wrong_format(tmp_path, make_record):
    input_path, output = tmp_path / "events.jsonl", tmp_path / "result.jsonl"
    decoded = runtime.Decoder().decode(make_record("MS1BB", object_bytes(0x4A12, b"\x01"), encrypted=False))
    assert decoded["status"] == "decoded"
    input_path.write_text(json.dumps(decoded) + "\n", encoding="utf-8")
    stats = runtime.replay(input_path, [], output)
    assert stats["malformed_lines"] == 1
    assert records_from(output)[0]["status"] == "malformed"


@pytest.mark.parametrize("seconds", ["-1", "inf", "nan"])
def test_cli_rejects_invalid_duration_before_scanning(seconds):
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["scan", "--seconds", seconds])
    assert error.value.code == 2


def test_set_key_uses_hidden_prompt_and_keeps_key_out_of_output(tmp_path, monkeypatch, capsys):
    path = tmp_path / "secrets.json"
    save_config(path, [DeviceConfig(ADDRESS, "ES5BB", "Synthetic", None)])
    prompts = []

    def hidden_prompt(prompt):
        prompts.append(prompt)
        return TEST_KEY

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "getpass", hidden_prompt)
    assert cli.main(["set-key", "--address", ADDRESS, "--config", str(path)]) == 0
    assert len(prompts) == 1
    assert load_config(path)[0].bindkey == TEST_KEY
    captured = capsys.readouterr()
    assert TEST_KEY not in captured.out + captured.err
