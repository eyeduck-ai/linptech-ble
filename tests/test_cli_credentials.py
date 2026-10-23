"""Interactive token updates do not expose credentials or alter other sensors."""
from types import SimpleNamespace

import pytest

from linptech_ble import cli
from linptech_ble.config import MIOT_MODELS, DeviceConfig, load_config, save_config

KEY = "00112233445566778899aabbccddeeff"
TOKEN = "1234567890abcdef12345678"
ADDRESS = "AA:BB:CC:DD:EE:01"


@pytest.mark.parametrize("model", MIOT_MODELS)
def test_set_token_uses_hidden_prompt_and_preserves_model_and_other_credentials(tmp_path, monkeypatch, capsys, model):
    config = tmp_path / "sensors.local.json"
    save_config(config, [DeviceConfig(ADDRESS, model, "Synthetic", KEY),
                         DeviceConfig("AA:BB:CC:DD:EE:02", "ES5BB", "Other", KEY, "f" * 24)])
    prompts = []
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "getpass", lambda prompt: prompts.append(prompt) or TOKEN)
    result = cli.main(["set-token", "--config", str(config), "--address", ADDRESS, "--miot-model", MIOT_MODELS[model]])
    assert result == 0 and len(prompts) == 1
    actual = {item.address: item for item in load_config(config)}
    assert actual[ADDRESS].auth_token == TOKEN and actual[ADDRESS].bindkey == KEY
    assert actual[ADDRESS].miot_model == MIOT_MODELS[model]
    assert actual["AA:BB:CC:DD:EE:02"].auth_token == "f" * 24
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err and KEY not in output.out + output.err


def test_mismatched_model_is_rejected_before_prompt_or_file_change(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sensors.local.json"
    save_config(config, [DeviceConfig(ADDRESS, "HS3BB", "Synthetic", KEY)])
    before = config.read_bytes()
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    def forbidden_prompt(_prompt):
        raise AssertionError("Must reject model before asking for a token")
    monkeypatch.setattr(cli, "getpass", forbidden_prompt)
    assert cli.main(["set-token", "--config", str(config), "--address", ADDRESS, "--miot-model", MIOT_MODELS["MS1BB"]]) != 0
    assert config.read_bytes() == before
    output = capsys.readouterr()
    assert KEY not in output.out + output.err


def test_bad_token_does_not_echo_prompt_value_or_write_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sensors.local.json"
    save_config(config, [DeviceConfig(ADDRESS, "ES5BB", "Synthetic", KEY, TOKEN)])
    before = config.read_bytes()
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "getpass", lambda _prompt: "bad-secret-value")
    assert cli.main(["set-token", "--config", str(config), "--address", ADDRESS]) != 0
    assert config.read_bytes() == before
    output = capsys.readouterr()
    assert all(secret not in output.out + output.err for secret in ("bad-secret-value", KEY, TOKEN))


def test_noninteractive_token_prompt_is_rejected_before_getpass_fallback(tmp_path, monkeypatch):
    config = tmp_path / "sensors.local.json"
    save_config(config, [DeviceConfig(ADDRESS, "ES5BB", "Synthetic", KEY)])
    before = config.read_bytes()
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    prompted = []
    monkeypatch.setattr(cli, "getpass", lambda prompt: prompted.append(prompt) or TOKEN)
    assert cli.main(["set-token", "--config", str(config), "--address", ADDRESS]) != 0
    assert prompted == [] and config.read_bytes() == before
