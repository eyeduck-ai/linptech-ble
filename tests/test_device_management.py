"""Local inventory changes preserve other devices and never use Bluetooth."""

import pytest

from linptech_ble import cli
from linptech_ble.config import DeviceConfig, MIOT_MODELS, load_config, save_config


ADDRESS = "AA:BB:CC:DD:EE:01"
OTHER = "AA:BB:CC:DD:EE:02"
KEY = "00112233445566778899aabbccddeeff"
TOKEN = "1234567890abcdef12345678"


@pytest.mark.parametrize("address", [ADDRESS.lower(), "aa-bb-cc-dd-ee-01", "aabbccddee01"])
def test_remove_normalizes_address_and_preserves_other_credentials(tmp_path, capsys, address):
    path = tmp_path / "sensors.local.json"
    other = DeviceConfig(OTHER, "HS3BB", "Hallway", KEY, TOKEN, MIOT_MODELS["HS3BB"])
    save_config(path, [DeviceConfig(ADDRESS, "KS2BB", "Bedroom", KEY, TOKEN), other])

    assert cli.main(["remove-device", "--config", str(path), "--address", address]) == 0
    assert load_config(path) == [other]
    output = capsys.readouterr()
    assert "Restart the dashboard" in output.out
    assert KEY not in output.out + output.err and TOKEN not in output.out + output.err


def test_remove_last_device_keeps_valid_empty_configuration(tmp_path):
    path = tmp_path / "sensors.local.json"
    save_config(path, [DeviceConfig(ADDRESS, "MS1BB", "Front door", KEY)])
    assert cli.main(["remove-device", "--config", str(path), "--address", ADDRESS]) == 0
    assert path.exists() and load_config(path) == []


def test_remove_unknown_address_does_not_rewrite_configuration(tmp_path, capsys):
    path = tmp_path / "sensors.local.json"
    save_config(path, [DeviceConfig(ADDRESS, "KS2BB", "Bedroom", KEY, TOKEN)])
    before = path.read_bytes()
    assert cli.main(["remove-device", "--config", str(path), "--address", OTHER]) == 1
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert "No configured device" in output.err
    assert KEY not in output.out + output.err and TOKEN not in output.out + output.err


def test_remove_missing_configuration_does_not_create_file(tmp_path, capsys):
    path = tmp_path / "missing.json"
    assert cli.main(["remove-device", "--config", str(path), "--address", ADDRESS]) == 1
    assert not path.exists()
    assert "No configured device" in capsys.readouterr().err


def test_remove_invalid_address_does_not_rewrite_configuration(tmp_path):
    path = tmp_path / "sensors.local.json"
    save_config(path, [DeviceConfig(ADDRESS, "ES5BB", "Living room", KEY, TOKEN)])
    before = path.read_bytes()
    assert cli.main(["remove-device", "--config", str(path), "--address", "invalid"]) == 1
    assert path.read_bytes() == before


def test_remove_rejects_broken_configuration_without_overwriting_it(tmp_path):
    path = tmp_path / "sensors.local.json"
    path.write_text('{"version":1,"devices":', encoding="utf-8")
    before = path.read_bytes()
    assert cli.main(["remove-device", "--config", str(path), "--address", ADDRESS]) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("model", tuple(MIOT_MODELS))
def test_rename_with_add_device_preserves_existing_credentials_and_other_sensors(tmp_path, capsys, model):
    path = tmp_path / "sensors.local.json"
    original = DeviceConfig(ADDRESS, model, "Old name", KEY, TOKEN, MIOT_MODELS[model])
    other = DeviceConfig(OTHER, model, "Other room", "f" * 32, "e" * 24, MIOT_MODELS[model])
    save_config(path, [original, other])

    assert cli.main(["add-device", "--config", str(path), "--address", ADDRESS.lower(),
                     "--model", model, "--name", "New room"]) == 0
    actual = {item.address: item for item in load_config(path)}
    assert actual[ADDRESS] == DeviceConfig(ADDRESS, model, "New room", KEY, TOKEN, MIOT_MODELS[model])
    assert actual[OTHER] == other
    output = capsys.readouterr()
    assert all(secret not in output.out + output.err for secret in (KEY, TOKEN, "f" * 32, "e" * 24))


def test_add_same_model_at_another_address_creates_separate_instance(tmp_path):
    path = tmp_path / "sensors.local.json"
    existing = DeviceConfig(ADDRESS, "KS2BB", "Bedroom", KEY, TOKEN)
    save_config(path, [existing])
    assert cli.main(["add-device", "--config", str(path), "--address", OTHER,
                     "--model", "KS2BB", "--name", "Office"]) == 0
    assert load_config(path) == [existing, DeviceConfig(OTHER, "KS2BB", "Office")]


def test_add_existing_device_without_name_preserves_name_and_credentials(tmp_path):
    path = tmp_path / "sensors.local.json"
    existing = DeviceConfig(ADDRESS, "HS3BB", "Hallway", KEY, TOKEN, MIOT_MODELS["HS3BB"])
    save_config(path, [existing])
    assert cli.main(["add-device", "--config", str(path), "--address", ADDRESS,
                     "--model", "HS3BB"]) == 0
    assert load_config(path) == [existing]
