import base64
import json

import pytest

from linptech_ble.transport_config import TransportConfig, load_transport_config, public_transport
from linptech_ble import cli


API_KEY = base64.b64encode(b"a" * 32).decode()
PASSWORD = "private-placeholder-do-not-echo"


def network_config():
    return {"version": 1, "bluetooth": {"backend": "esphome", "proxies": [
        {"name": "bedroom", "host": "192.0.2.10", "noise_psk": API_KEY}], "gatt_proxy": "bedroom"},
        "es5": {"auto_connect": True, "retry_seconds": 30}}


def write(tmp_path, value):
    path = tmp_path / "transport.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_default_keeps_local_ble_and_disables_automatic_connections():
    value = load_transport_config()
    assert value.bluetooth.backend == "local" and not value.bluetooth.proxies
    assert not value.es5_auto_connect


def test_remote_credentials_are_private_and_devices_config_is_separate(tmp_path):
    value = load_transport_config(write(tmp_path, network_config()))
    assert value.bluetooth.direct_proxy().name == "bedroom"
    assert value.es5_auto_connect
    public = json.dumps(public_transport(value.bluetooth))
    for secret in (API_KEY, PASSWORD):
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
    lambda c: c.update(mqtt={"enabled": True, "password": PASSWORD}),
    lambda c: c["es5"].update(auto_connect="yes"),
    lambda c: c["es5"].update(retry_seconds=0),
])
def test_invalid_configuration_fails_before_network_and_without_secrets(tmp_path, edit):
    document = network_config()
    edit(document)
    with pytest.raises(ValueError) as failure:
        load_transport_config(write(tmp_path, document))
    assert PASSWORD not in str(failure.value) and API_KEY not in str(failure.value)


def test_multiple_proxy_autoconnect_requires_explicit_gatt_route(tmp_path):
    document = network_config()
    document["bluetooth"]["proxies"].append({"name": "living", "host": "192.0.2.11"})
    del document["bluetooth"]["gatt_proxy"]
    with pytest.raises(ValueError):
        load_transport_config(write(tmp_path, document))
    document["es5"]["auto_connect"] = False
    value = load_transport_config(write(tmp_path, document))
    assert public_transport(value.bluetooth)["gatt_proxy"] is None


def test_explicit_missing_file_never_silently_falls_back_to_local(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_transport_config(tmp_path / "missing.json")


def test_doctor_validates_network_config_without_connecting(tmp_path, monkeypatch, capsys):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kw: pytest.fail("doctor must be offline"))
    assert cli.main(["doctor", "--transport-config", str(write(tmp_path, network_config()))]) == 0
    output = capsys.readouterr().out
    assert "esphome" in output and "bedroom" in output
    assert API_KEY not in output and PASSWORD not in output


def test_replay_rejects_unnecessary_transport_configuration_before_reading_files(tmp_path):
    from linptech_ble.dashboard import serve_dashboard
    with pytest.raises(ValueError, match="離線重播"):
        serve_dashboard(config="not-read.json", replay_path="not-read.jsonl", transport_path="not-read.json")


def test_capture_cli_requires_dashboard_for_automatic_direct_connections(tmp_path, capsys):
    path = write(tmp_path, network_config())
    assert cli.main(["scan", "--transport-config", str(path)]) == 1
    assert "dashboard" in capsys.readouterr().err


@pytest.mark.parametrize("options", [{"es5_auto_connect": "yes"}, {"es5_retry_seconds": True},
    {"es5_retry_seconds": float("nan")}, {"es5_retry_seconds": 10 ** 400}])
def test_programmatic_transport_options_validate_before_any_connection(options):
    with pytest.raises(ValueError):
        TransportConfig(**options)


def test_json_per_address_routes_support_auto_connect_without_a_global_proxy(tmp_path):
    document = network_config()
    document["bluetooth"]["proxies"].append({"name": "living", "host": "192.0.2.11"})
    del document["bluetooth"]["gatt_proxy"]
    document["bluetooth"]["gatt_routes"] = {"aa:bb:cc:dd:ee:01": "living", "AA:BB:CC:DD:EE:02": "bedroom"}
    settings = load_transport_config(write(tmp_path, document))
    assert settings.es5_auto_connect
    assert settings.bluetooth.direct_proxy("AA:BB:CC:DD:EE:01").name == "living"
    assert settings.bluetooth.direct_proxy("AA:BB:CC:DD:EE:02").name == "bedroom"
    public = public_transport(settings.bluetooth)
    assert public["gatt_proxy"] is None
    assert public["gatt_routes"] == {"AA:BB:CC:DD:EE:01": "living", "AA:BB:CC:DD:EE:02": "bedroom"}
    assert API_KEY not in json.dumps(public) and "192.0.2" not in json.dumps(public)
