"""Live examples with synthetic records and caller-controlled public I/O mocks.

The real decoder runs, but no account, radio, proxy or private file is opened.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import TEST_KEY, object_bytes
from linptech_ble import BluetoothConfig, DeviceConfig, ProxyConfig


EXAMPLES = Path(__file__).parents[1] / "examples"
TOKEN = "00112233445566778899aabb"
MODELS = {
    "ES5BB": "linp.sensor_occupy.es5b", "KS2BB": "linp.sensor_ht.ks2bb",
    "HS3BB": "linp.motion.hs1bb2", "MS1BB": "linp.magnet.m1",
}


def example(name):
    spec = importlib.util.spec_from_file_location("synthetic_" + name, EXAMPLES / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sensor(model="KS2BB", number=1, *, token=TOKEN):
    return DeviceConfig(f"AA:BB:CC:DD:EE:{number:02X}", model, "Synthetic " + model,
                        TEST_KEY, token, MODELS[model])


class Radio:
    """Public scanner stand-in; records may arrive during a GATT read."""

    def __init__(self, records=(), *, hold_open=False):
        self.queue = asyncio.Queue()
        for record in records:
            self.queue.put_nowait(record)
        self.hold_open = hold_open
        self.entered = self.closed = False
        self.exits = self.yielded = self.failures = 0
        self.options = None

    def factory(self, **options):
        self.options = options
        return self

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_):
        self.closed = True
        self.exits += 1

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.queue.empty() and not self.hold_open:
            raise StopAsyncIteration
        record = await self.queue.get()
        await asyncio.sleep(0)
        if isinstance(record, Exception):
            self.failures += 1
            raise record
        self.yielded += 1
        return record

    def emit(self, record):
        self.queue.put_nowait(record)

    async def delivered(self, count):
        async def wait():
            while self.yielded < count:
                await asyncio.sleep(0)
            # Decoding completes synchronously after the iterator returns.
            await asyncio.sleep(0)
        await asyncio.wait_for(wait(), 1)

    def health(self):
        return {"listening": self.entered and not self.closed,
                "counts": {"yielded": self.yielded}}


def setup_sources(module, monkeypatch, tmp_path, devices, radio):
    """Assert explicit caller paths without reading an actual credential file."""
    paths = {"config": tmp_path / "selected-sensors.json",
             "transport": tmp_path / "selected-transport.json"}
    loads = []
    transport = BluetoothConfig(backend="esphome", proxies=(ProxyConfig("hall", "192.0.2.10"),))

    def load_sensors(path):
        loads.append(("sensors", path))
        assert path == paths["config"]
        return devices

    def load_transport(path):
        loads.append(("transport", path))
        assert path == paths["transport"]
        return SimpleNamespace(bluetooth=transport)

    monkeypatch.setattr(module, "load_config", load_sensors)
    monkeypatch.setattr(module, "load_transport_config", load_transport)
    monkeypatch.setattr(module, "AdvertisementScanner", radio.factory)
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(config=paths["config"], transport_config=paths["transport"],
                           seconds=.01, gatt_read=False)
    return args, transport, loads


def run_bounded(module, args):
    return asyncio.run(asyncio.wait_for(module.run(args), 3))


def test_live_scan_preserves_receiver_rssi_without_replaying_button_or_writing_files(
    monkeypatch, tmp_path, capsys, make_record,
):
    module, device = example("library_scan"), sensor()
    packet = make_record("KS2BB", object_bytes(0x560C, b""), address=device.address)
    radio = Radio([replace(packet, receiver="hall", rssi=-45),
                   replace(packet, receiver="bedroom", rssi=-78)])
    args, transport, loads = setup_sources(module, monkeypatch, tmp_path, [device], radio)
    args.seconds = 1
    run_bounded(module, args)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["receiver"] for row in rows[:-1]] == ["hall", "bedroom"]
    assert [row["rssi"] for row in rows[:-1]] == [-45, -78]
    assert [row["status"] for row in rows[:-1]] == ["decoded", "duplicate"]
    assert sum(len(row["events"]) for row in rows[:-1]) == 1
    assert rows[-1]["scanner"]["counts"]["yielded"] == 2
    assert radio.options == {"transport": transport, "addresses": [device.address]}
    assert loads == [("sensors", args.config), ("transport", args.transport_config)]
    assert radio.closed and radio.exits == 1 and list(tmp_path.iterdir()) == []
    assert TEST_KEY not in json.dumps(rows) and TOKEN not in json.dumps(rows)


@pytest.mark.parametrize("name", ["library_scan", "library_validate_proxy"])
def test_quiet_observation_window_finishes_without_hardware_or_output_files(
    name, monkeypatch, tmp_path, capsys,
):
    module, radio = example(name), Radio(hold_open=True)
    args, _, _ = setup_sources(module, monkeypatch, tmp_path, [sensor()], radio)
    run_bounded(module, args)
    output = capsys.readouterr().out
    assert radio.closed and radio.exits == 1 and list(tmp_path.iterdir()) == []
    if name == "library_validate_proxy":
        report = json.loads(output)
        assert report["all_devices_observed"] is False
        assert report["devices"][0]["authenticated_advertisement_observed"] is False
        assert report["devices"][0]["receivers"] == []
        assert report["decoded_status_counts"] == {} and report["gatt"] == []
    else:
        assert json.loads(output)["scanner"]["counts"]["yielded"] == 0


def test_probe_aggregates_valid_rssi_per_receiver_but_keeps_invalid_packet_observations(
    monkeypatch, tmp_path, capsys, make_record,
):
    module, device = example("library_validate_proxy"), sensor()
    packet = make_record("KS2BB", object_bytes(0x560C, b""), address=device.address)
    radio = Radio([replace(packet, receiver=receiver, rssi=rssi) for receiver, rssi in
                   [("hall", -50), ("hall", -70), ("bedroom", -80),
                    ("invalid", -127), ("invalid", 0), ("invalid", 20)]])
    args, _, _ = setup_sources(module, monkeypatch, tmp_path, [device], radio)
    args.seconds = 1
    run_bounded(module, args)
    report = json.loads(capsys.readouterr().out)
    receivers = {row["receiver"]: row for row in report["devices"][0]["receivers"]}
    assert receivers == {
        "hall": {"receiver": "hall", "samples_retained": 2, "rssi_min": -70,
                 "rssi_median": -60, "rssi_max": -50},
        "bedroom": {"receiver": "bedroom", "samples_retained": 1, "rssi_min": -80,
                    "rssi_median": -80, "rssi_max": -80},
    }
    assert report["decoded_status_counts"] == {"decoded": 1, "duplicate": 5}
    assert report["all_devices_observed"] is True and list(tmp_path.iterdir()) == []


def test_probe_retains_only_latest_600_rssi_samples_per_receiver(
    monkeypatch, tmp_path, capsys, make_record,
):
    module, device = example("library_validate_proxy"), sensor()
    packet = make_record("KS2BB", object_bytes(0x560C, b""), address=device.address)
    radio = Radio([replace(packet, receiver="hall", rssi=-126 if index < 5 else -50)
                   for index in range(605)])
    args, _, _ = setup_sources(module, monkeypatch, tmp_path, [device], radio)
    args.seconds = 1
    run_bounded(module, args)
    report = json.loads(capsys.readouterr().out)
    assert report["devices"][0]["receivers"] == [{
        "receiver": "hall", "samples_retained": 600,
        "rssi_min": -50, "rssi_median": -50, "rssi_max": -50,
    }]
    assert sum(report["decoded_status_counts"].values()) == 605


def test_probe_collects_during_read_only_gatt_and_handles_missing_tokens_and_ms1(
    monkeypatch, tmp_path, capsys, make_record,
):
    module = example("library_validate_proxy")
    devices = [sensor("ES5BB", 1), sensor("KS2BB", 2), sensor("HS3BB", 3),
               sensor("MS1BB", 4), sensor("KS2BB", 5, token=None)]
    radio, reads, entries, exits, forbidden = Radio(hold_open=True), [], [], [], []
    args, transport, _ = setup_sources(module, monkeypatch, tmp_path, devices, radio)
    args.gatt_read = True

    class Client:
        def __init__(self, device, *, transport):
            assert transport is radio.options["transport"]
            assert radio.entered and not radio.closed
            self.device = device

        async def __aenter__(self):
            entries.append(self.device.model)
            return self

        async def __aexit__(self, *_):
            exits.append(self.device.model)

        async def read(self, name):
            reads.append((self.device.model, name))
            count = radio.yielded
            packet = make_record("KS2BB", object_bytes(0x560C, b""), address=devices[1].address)
            radio.emit(replace(packet, receiver="hall", rssi=-55))
            radio.emit(replace(packet, receiver="bedroom", rssi=-75))
            await radio.delivered(count + 2)
            return SimpleNamespace(valid=True, name=name, status="read")

        def __getattr__(self, name):
            forbidden.append(name)
            raise AssertionError("The probe may only use context entry/exit and read")

    monkeypatch.setattr(module, "LinptechClient", Client)
    run_bounded(module, args)
    report = json.loads(capsys.readouterr().out)
    rows = {row["address"]: row for row in report["gatt"]}
    assert reads == [("ES5BB", "occupancy_status"), ("KS2BB", "temperature_unit"),
                     ("HS3BB", "detection_interval")]
    assert entries == exits == ["ES5BB", "KS2BB", "HS3BB"] and forbidden == []
    assert rows[devices[4].address]["status"] == "missing_token"
    assert rows[devices[3].address]["status"] == "advertisement_only"
    for device in devices[:3]:
        assert rows[device.address]["status"] == "read"
        assert rows[device.address]["advertisements_during_gatt"] == 2
    assert report["decoded_status_counts"] == {"decoded": 1, "duplicate": 5}
    assert report["devices"][1]["authenticated_advertisement_observed"] is True
    assert report["all_devices_observed"] is False
    assert radio.options["addresses"] == [device.address for device in devices]
    assert radio.closed and radio.options["transport"] is transport and list(tmp_path.iterdir()) == []


def test_probe_reports_gatt_failure_type_without_secret_exception_text(
    monkeypatch, tmp_path, capsys,
):
    module, radio = example("library_validate_proxy"), Radio(hold_open=True)
    args, _, _ = setup_sources(module, monkeypatch, tmp_path, [sensor("ES5BB")], radio)
    args.gatt_read = True

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def read(self, _name):
            raise RuntimeError("SYNTHETIC_PRIVATE_TOKEN https://private.invalid/account")

    monkeypatch.setattr(module, "LinptechClient", Client)
    run_bounded(module, args)
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["gatt"][0]["status"] == "failed"
    assert report["gatt"][0]["error_type"] == "RuntimeError"
    assert "SYNTHETIC_PRIVATE_TOKEN" not in output and "private.invalid" not in output
    assert radio.closed


def test_observer_failure_during_gatt_is_propagated_instead_of_returning_success(
    monkeypatch, tmp_path, capsys,
):
    module, radio = example("library_validate_proxy"), Radio(hold_open=True)
    args, _, _ = setup_sources(module, monkeypatch, tmp_path, [sensor("ES5BB")], radio)
    args.gatt_read = True

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def read(self, name):
            radio.emit(RuntimeError("Synthetic receiver failed during GATT"))
            async def failed():
                while not radio.failures:
                    await asyncio.sleep(0)
            await asyncio.wait_for(failed(), 1)
            return SimpleNamespace(valid=True, name=name, status="read")

    monkeypatch.setattr(module, "LinptechClient", Client)
    with pytest.raises(RuntimeError, match="receiver failed"):
        run_bounded(module, args)
    assert radio.closed and capsys.readouterr().out == ""


@pytest.mark.parametrize("name", ["library_scan", "library_validate_proxy"])
def test_main_redacts_configuration_or_transport_error_text(name, monkeypatch, capsys):
    module = example(name)

    def fail(_path):
        raise RuntimeError("SYNTHETIC_SECRET_KEY https://private.invalid/account")

    monkeypatch.setattr(module, "load_config", fail)
    monkeypatch.setattr("sys.argv", [name, "--config", "explicit-synthetic.json",
                                    "--transport-config", "explicit-transport.json", "--seconds", "1"])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output)["type"] == "RuntimeError"
    assert "SYNTHETIC_SECRET_KEY" not in output and "private.invalid" not in output


@pytest.mark.parametrize("name", ["library_scan", "library_validate_proxy"])
def test_empty_explicit_inventory_fails_before_starting_scanner(name, monkeypatch, tmp_path):
    module, radio = example(name), Radio()
    args, _, _ = setup_sources(module, monkeypatch, tmp_path, [], radio)
    with pytest.raises(ValueError, match="configuration is empty"):
        run_bounded(module, args)
    assert radio.entered is False and list(tmp_path.iterdir()) == []
