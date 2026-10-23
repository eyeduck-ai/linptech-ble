"""Import scope and credential handling use public synthetic keys only."""
import json

import pytest

from linptech_ble.config import DeviceConfig, load_config, save_config
from linptech_ble.xiaomi_import import import_keys

KEY = "00112233445566778899aabbccddeeff"


def export_file(tmp_path, rows):
    source = tmp_path / "export.secrets.json"
    source.write_text(json.dumps([{"server": "cn", "homes": [{"devices": rows}]}]), encoding="utf-8")
    return source


def test_only_target_sensors_are_imported_and_summary_has_no_credentials(tmp_path):
    source = export_file(tmp_path, [
        {"model": "linp.magnet.m1", "mac": "AA:BB:CC:DD:EE:01", "name": "Door", "BLE_DATA": {"beaconkey": KEY}, "token": "do-not-import"},
        {"model": "other.device", "mac": "AA:BB:CC:DD:EE:02", "BLE_DATA": {"beaconkey": KEY}},
    ])
    config = tmp_path / "sensors.local.json"
    save_config(config, [DeviceConfig("AA:BB:CC:DD:EE:03", "ES5BB", "Existing", KEY)])
    summary = import_keys(source, config)
    assert KEY not in json.dumps(summary)
    assert "do-not-import" not in config.read_text()
    assert {item.model for item in load_config(config)} == {"MS1BB", "ES5BB"}
    assert len(summary["imported"]) == 1


def test_hs3_cloud_model_maps_to_hs3bb(tmp_path):
    source = export_file(tmp_path, [{"model": "linp.motion.hs1bb2", "mac": "AA:BB:CC:DD:EE:01", "BLE_DATA": {"beaconkey": KEY}}])
    config = tmp_path / "sensors.local.json"
    import_keys(source, config)
    assert load_config(config)[0].model == "HS3BB"


@pytest.mark.parametrize("rows", [
    [{"model": "linp.magnet.m1", "mac": "AA:BB:CC:DD:EE:01"}],
    [{"model": "linp.magnet.m1", "mac": "AA:BB:CC:DD:EE:01", "BLE_DATA": {"beaconkey": "invalid"}}],
    [{"model": "other.device", "mac": "AA:BB:CC:DD:EE:01", "BLE_DATA": {"beaconkey": KEY}}],
])
def test_no_valid_target_never_replaces_existing_config(tmp_path, rows):
    config = tmp_path / "sensors.local.json"
    save_config(config, [DeviceConfig("AA:BB:CC:DD:EE:03", "ES5BB", "Existing", KEY)])
    before = config.read_bytes()
    with pytest.raises(ValueError) as error:
        import_keys(export_file(tmp_path, rows), config)
    assert KEY not in str(error.value)
    assert config.read_bytes() == before


def test_conflicting_duplicate_export_does_not_write_config(tmp_path):
    rows = [
        {"model": "linp.magnet.m1", "mac": "AA:BB:CC:DD:EE:01", "BLE_DATA": {"beaconkey": KEY}},
        {"model": "linp.magnet.m1", "mac": "AA:BB:CC:DD:EE:01", "BLE_DATA": {"beaconkey": "f" * 32}},
    ]
    config = tmp_path / "sensors.local.json"
    with pytest.raises(ValueError):
        import_keys(export_file(tmp_path, rows), config)
    assert not config.exists()


def test_direct_token_is_opt_in_scoped_to_es5_and_preserved_on_later_import(tmp_path):
    token = "1234567890abcdef12345678"
    source = export_file(tmp_path, [
        {"model": "linp.sensor_occupy.es5b", "mac": "AA:BB:CC:DD:EE:01", "BLE_DATA": {"beaconkey": KEY}, "token": token},
        {"model": "linp.magnet.m1", "mac": "AA:BB:CC:DD:EE:02", "BLE_DATA": {"beaconkey": KEY}, "token": "not-a-ble-token"},
    ])
    config = tmp_path / "sensors.local.json"
    import_keys(source, config)
    assert all(item.auth_token is None for item in load_config(config))
    summary = import_keys(source, config, include_tokens=True)
    assert token not in json.dumps(summary) and KEY not in json.dumps(summary)
    devices = {d.model: d for d in load_config(config)}
    assert devices["ES5BB"].auth_token == token and devices["MS1BB"].auth_token is None
    import_keys(source, config)
    assert next(d for d in load_config(config) if d.model == "ES5BB").auth_token == token


def test_invalid_direct_token_does_not_overwrite_config(tmp_path):
    token = "bad-secret-token"
    source = export_file(tmp_path, [{"model": "linp.sensor_occupy.es5b", "mac": "AA:BB:CC:DD:EE:01", "BLE_DATA": {"beaconkey": KEY}, "token": token}])
    config = tmp_path / "sensors.local.json"
    save_config(config, [DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Original", KEY)])
    before = config.read_bytes()
    with pytest.raises(ValueError) as error:
        import_keys(source, config, include_tokens=True)
    assert token not in str(error.value) and KEY not in str(error.value)
    assert config.read_bytes() == before
