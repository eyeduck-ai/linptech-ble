"""Four-model token opt-in and model identity, using public fake credentials."""
import json

import pytest

from linptech_ble.config import MIOT_MODELS, DeviceConfig, load_config, save_config
from linptech_ble.xiaomi_import import import_keys

KEY = "00112233445566778899aabbccddeeff"
TOKEN = "1234567890abcdef12345678"


def exported(tmp_path, rows):
    path = tmp_path / "export.private.json"
    path.write_text(json.dumps([{"homes": [{"devices": rows}]}]), encoding="utf-8")
    return path


@pytest.mark.parametrize("model,miot_model", MIOT_MODELS.items())
def test_each_model_can_opt_in_to_exact_12_byte_standard_auth_token(tmp_path, model, miot_model):
    source = exported(tmp_path, [{"model": miot_model, "mac": "AA:BB:CC:DD:EE:01",
                                 "BLE_DATA": {"beaconkey": KEY}, "token": TOKEN}])
    config = tmp_path / "sensors.local.json"
    import_keys(source, config)
    first = load_config(config)[0]
    assert first.model == model and first.miot_model == miot_model and first.auth_token is None
    summary = import_keys(source, config, include_tokens=True)
    actual = load_config(config)[0]
    assert actual.auth_token == TOKEN and actual.miot_model == miot_model
    assert TOKEN not in json.dumps(summary) and KEY not in json.dumps(summary)
    assert TOKEN not in repr(actual) and KEY not in repr(actual)
    import_keys(source, config)
    assert load_config(config)[0].auth_token == TOKEN  # Opt-out refresh preserves existing consent.


def test_mixed_cloud_and_ble_tokens_do_not_pollute_other_devices_or_public_summary(tmp_path):
    rows = []
    for number, (model, miot) in enumerate(MIOT_MODELS.items(), 1):
        rows.append({"model": miot, "mac": f"AA:BB:CC:DD:EE:0{number}", "BLE_DATA": {"beaconkey": KEY},
                     "token": TOKEN if model in {"ES5BB", "HS3BB"} else "fedcba9876543210fedcba9876543210"})
    source = exported(tmp_path, rows)
    config = tmp_path / "sensors.local.json"
    summary = import_keys(source, config, include_tokens=True)
    actual = {device.model: device for device in load_config(config)}
    assert actual["ES5BB"].auth_token == actual["HS3BB"].auth_token == TOKEN
    assert actual["MS1BB"].auth_token is None and actual["KS2BB"].auth_token is None
    assert all(device.miot_model == MIOT_MODELS[device.model] for device in actual.values())
    assert TOKEN not in json.dumps(summary) and KEY not in json.dumps(summary)
    assert "fedcba9876543210fedcba9876543210" not in config.read_text()


def test_invalid_hex_12_byte_token_is_atomic_and_error_redacts_credentials(tmp_path):
    secret = "z" * 24
    source = exported(tmp_path, [{"model": MIOT_MODELS["KS2BB"], "mac": "AA:BB:CC:DD:EE:01",
                                 "BLE_DATA": {"beaconkey": KEY}, "token": secret}])
    config = tmp_path / "sensors.local.json"
    save_config(config, [DeviceConfig("AA:BB:CC:DD:EE:01", "KS2BB", "Existing", KEY, TOKEN)])
    before = config.read_bytes()
    with pytest.raises(ValueError) as error:
        import_keys(source, config, include_tokens=True)
    assert config.read_bytes() == before
    assert all(value not in str(error.value) for value in (secret, TOKEN, KEY))


def test_miot_model_cannot_claim_another_sensor_protocol():
    with pytest.raises(ValueError):
        DeviceConfig("AA:BB:CC:DD:EE:01", "HS3BB", "Synthetic", KEY, TOKEN, MIOT_MODELS["MS1BB"])
