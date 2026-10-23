import base64
import json

import pytest

from linptech_ble.bridge_config import load_bridge_config, public_transport
from linptech_ble import cli


API_KEY = base64.b64encode(b"a" * 32).decode()
PASSWORD = "private-placeholder-do-not-echo"


def network_config():
    return {"version": 1, "bluetooth": {"backend": "esphome", "proxies": [
        {"name": "bedroom", "host": "192.0.2.10", "noise_psk": API_KEY}], "gatt_proxy": "bedroom"},
        "mqtt": {"enabled": True, "host": "192.0.2.20", "username": "private-user", "password": PASSWORD},
        "es5": {"auto_connect": True, "retry_seconds": 30}}


def write(tmp_path, value):
    path = tmp_path / "bridge.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_default_keeps_local_ble_and_has_no_mqtt_side_effects():
    value = load_bridge_config()
    assert value.bluetooth.backend == "local" and not value.bluetooth.proxies
    assert not value.mqtt.enabled and not value.es5_auto_connect


def test_remote_credentials_are_private_and_devices_config_is_separate(tmp_path):
    value = load_bridge_config(write(tmp_path, network_config()))
    assert value.bluetooth.direct_proxy().name == "bedroom"
    assert value.mqtt.enabled and value.es5_auto_connect
    public = json.dumps(public_transport(value.bluetooth))
    for secret in (API_KEY, PASSWORD, "private-user"):
        assert secret not in repr(value) and secret not in public
    assert "192.0.2.10" not in public


@pytest.mark.parametrize("edit", [
    lambda c: c.update(version=True),
    lambda c: c.update(devices=[{"bindkey": PASSWORD}]),
    lambda c: c["bluetooth"].update(backend="other"),
    lambda c: c["bluetooth"].update(gatt_proxy="missing"),
    lambda c: c["bluetooth"]["proxies"][0].update(noise_psk=PASSWORD),
    lambda c: c["bluetooth"]["proxies"][0].update(host="http://user:password@host"),
    lambda c: c["bluetooth"]["proxies"][0].update(port=True),
    lambda c: c["mqtt"].update(enabled="yes"),
    lambda c: c["mqtt"].update(topic_prefix="some/#"),
    lambda c: c["es5"].update(auto_connect="yes"),
    lambda c: c["es5"].update(retry_seconds=0),
])
def test_invalid_configuration_fails_before_network_and_without_secrets(tmp_path, edit):
    document = network_config()
    edit(document)
    with pytest.raises(ValueError) as failure:
        load_bridge_config(write(tmp_path, document))
    assert PASSWORD not in str(failure.value) and API_KEY not in str(failure.value)


def test_multiple_proxy_autoconnect_requires_explicit_gatt_route(tmp_path):
    document = network_config()
    document["bluetooth"]["proxies"].append({"name": "living", "host": "192.0.2.11"})
    del document["bluetooth"]["gatt_proxy"]
    with pytest.raises(ValueError):
        load_bridge_config(write(tmp_path, document))
    document["es5"]["auto_connect"] = False
    value = load_bridge_config(write(tmp_path, document))
    assert public_transport(value.bluetooth)["gatt_proxy"] is None


def test_explicit_missing_file_never_silently_falls_back_to_local(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_bridge_config(tmp_path / "missing.json")


def test_doctor_validates_network_config_without_connecting(tmp_path, monkeypatch, capsys):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kw: pytest.fail("doctor must be offline"))
    assert cli.main(["doctor", "--bridge-config", str(write(tmp_path, network_config()))]) == 0
    output = capsys.readouterr().out
    assert "esphome" in output and "bedroom" in output
    assert API_KEY not in output and PASSWORD not in output


def test_replay_cannot_publish_historical_state_to_ha(tmp_path):
    from linptech_ble.dashboard import serve_dashboard
    with pytest.raises(ValueError, match="離線重播"):
        serve_dashboard(config="not-read.json", replay_path="not-read.jsonl", bridge_path="not-read.json")


def test_capture_cli_requires_dashboard_for_service_features(tmp_path, capsys):
    path = write(tmp_path, network_config())
    assert cli.main(["scan", "--bridge-config", str(path)]) == 1
    assert "dashboard" in capsys.readouterr().err
