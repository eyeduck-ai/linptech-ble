# Developer tools

The package contains reusable MiBeacon decoding and authenticated async clients, plus a local dashboard for testing. No Xiaomi gateway is required. Scanning and decoding do not log in to Xiaomi; credentials are obtained and stored separately.

## Windows setup

The tested setup uses Windows, Python 3.11 and a BLE-capable adapter. Run from the project folder in ordinary PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dashboard]"
.\.venv\Scripts\python.exe -m linptech_ble doctor
```

Skip environment creation when the checkout is already configured. `doctor` checks software/configuration only; it does not scan, connect or verify hardware. Windows pairing is not required. Turn Bluetooth on and place the receiver near the sensor.

## Scan before adding credentials

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
.\.venv\Scripts\python.exe -m linptech_ble scan --seconds 60 --output "captures/scan-$stamp.jsonl"
```

This identifies candidate MiBeacon devices and records raw advertisements. Add `--all` only when needed to investigate an unidentified device; it also records nearby devices unrelated to the test. Product IDs are stronger evidence than advertised names.

A received encrypted packet proves radio reception, not successful sensor decoding. There is no guaranteed account-free bindkey extraction method for these four models. See [credential setup](BINDKEY_WINDOWS.md).

## Configure one sensor

Use the real address you observed and the correct model. Keys are entered through non-echoing prompts, rather than command-line arguments:

```powershell
$address = Read-Host 'Sensor Bluetooth MAC address'
.\.venv\Scripts\python.exe -m linptech_ble add-device --address $address --model ES5BB --name 'Living room'
.\.venv\Scripts\python.exe -m linptech_ble set-key --address $address
```

The other model choices are `MS1BB`, `KS2BB` and `HS3BB`. Default credentials live in `sensors.local.json`; pass `--config` to select another private file. For authenticated GATT, use `set-token` or import an authorized local export with `--include-tokens`; details are in the credential guide.

## Add, rename or remove sensors

There is no four-device inventory limit. Each physical sensor needs its own Bluetooth address and credentials; several entries may use the same model. Give each instance a useful name, such as `Bedroom temperature` or `Kitchen temperature`. Names do not have to be unique, but distinct names make filtering easier.

To add another sensor, repeat `add-device` and `set-key` with its address. Rename an existing sensor by running `add-device` again with the same address and model and a new `--name`; its bindkey, direct token and confirmed MIoT model are preserved.

```powershell
$address = Read-Host 'Existing sensor Bluetooth MAC address'
.\.venv\Scripts\python.exe -m linptech_ble add-device --address $address --model KS2BB --name 'Bedroom temperature'
```

Remove an entry from the selected local configuration:

```powershell
.\.venv\Scripts\python.exe -m linptech_ble remove-device --address $address
```

Removal does not reset, unbind or contact the sensor, and preserves all other entries. An unknown address fails without rewriting the configuration. Keep a private backup if you may need the removed credentials again. Stop and restart the dashboard or monitor after any inventory or credential change; configuration is loaded at startup.

## Monitor, replay and open the dashboard

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
.\.venv\Scripts\python.exe -m linptech_ble monitor --configured-only --seconds 180 --output "captures/events-$stamp.jsonl" --raw-output "captures/raw-$stamp.jsonl"
```

`monitor` without a duration runs until Ctrl+C. Raw and decoded output paths must be different and must not already exist. Decoded records contain only that packet's fields; missing fields do not clear previous application state. Repeated packets within the decoder's deduplication window do not produce repeated button events.

Replay a deliberately selected raw recording without Bluetooth:

```powershell
$inputFile = Read-Host 'Path to a private raw JSONL recording'
.\.venv\Scripts\python.exe -m linptech_ble replay $inputFile
```

For live sensor rows, signal quality and advanced controls:

```powershell
.\.venv\Scripts\python.exe -m linptech_ble dashboard --open
```

Dashboard startup receives advertisements; GATT connection and setting changes require explicit actions by default. An ES5 connection starts radar reporting. Advanced controls call the same public `LinptechClient` used by standalone applications; workers only manage UI state and Windows process isolation. See [dashboard behavior and architecture](DASHBOARD_WINDOWS.md) and [ES5 controls](ES5BB_ADVANCED.md).

## Validate behavior

| Sensor | Useful checks |
| --- | --- |
| ES5BB | Enter/remain/leave; compare fused occupancy with the configured clearing delay and fresh radar data. PIR trigger is verified; independent PIR energy is still unconfirmed. |
| MS1BB | Open and close repeatedly. The released profile exposes no GATT settings. |
| KS2BB | Single/double/long press; wait separately for temperature/humidity. Display unit and button-mode writes have been read back and restored on hardware. |
| HS3BB | Motion/illuminance, then a short-button wake for GATT. Indicator and detection-interval writes have been read back and restored; unsupported firmware values remain invalid. |

Radio reception, authenticated decoding, valid interpretation and agreement with physical actions are separate checks. Battery reporting and quiet periods follow device behavior. Never infer absence or a zero value solely from silence. Detailed evidence is in [device capabilities](DEVICE_CAPABILITIES.md).

Common decoder statuses: `encrypted_no_key` requires the correct bindkey; `decryption_failed` requires checking address/key/binding; `model_mismatch` requires checking the configured model; `no_measurement` is a valid packet without usable sensor data. Preserve raw input when investigating `malformed`, `parse_error` or unsupported protocols.

## Library, proxy and tests

- [Library API](LIBRARY.md): caller-owned `Decoder`, `LinptechClient`, metadata and payload-free diagnostics; no dashboard required.
- [Examples](../examples/README.md): offline synthetic demo and explicit library clients.
- [ESPHome / Home Assistant](ESPHOME_HOME_ASSISTANT.md): optional proxy transport and MQTT bridge. They require separate configuration and hardware validation.

Run synthetic tests without sensors or account credentials:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

Keep real exports, local credential files, recordings, vendor packages and private hardware scripts out of public source and distributions. Unit tests use public synthetic keys; passing them does not establish physical-device support. Use `python -m linptech_ble --help` for the complete command list.
