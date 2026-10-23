# Windows dashboard

The dashboard is a local test application for ES5BB, MS1BB, KS2BB and HS3BB. The interface remains in Traditional Chinese. It runs without a Xiaomi gateway; monitoring uses credentials already stored locally and does not require a cloud login.

## Start

Run from the project folder. In an already configured checkout, double-click `start-dashboard.cmd`, or use PowerShell:

```powershell
.\.venv\Scripts\python.exe -m linptech_ble dashboard --open
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). Keep the terminal open and press Ctrl+C to stop. No Node.js build is needed. If the port is occupied, use `--port 8767`; starting a second instance may also compete for Bluetooth access.

New users must first create the Python environment and supply their own device credentials: [developer tools](DEVTOOLS.md), [credential setup](BINDKEY_WINDOWS.md). The repository does not include credentials for physical sensors.

Default startup:

- Starts local BLE advertisement reception, decoding and recording under `captures/`.
- Leaves GATT controllers waiting for a connection request. It does not automatically connect or change sensor settings.
- Keeps MQTT and ES5 automatic connection disabled. These require an explicit [bridge configuration](ESPHOME_HOME_ASSISTANT.md).
- Binds HTTP to `127.0.0.1` only. It does not expose the dashboard to other LAN devices.

## Test the sensors

Place the sensors near the receiver. Close their Xiaomi Home control pages; for GATT tests, turn off the phone's Bluetooth in system settings to release the connection.

| Sensor | Test action | Expected observations |
| --- | --- | --- |
| ES5BB | Enter, remain in, then leave the detection area | Occupancy, close-region occupancy and illuminance when reported |
| MS1BB | Move the magnet away and back | Open/closed state and any reported door warnings |
| KS2BB | Single/double/long press; separately wait for environmental updates | Button events, temperature and humidity |
| HS3BB | Move in front of it, then leave; vary ambient light | Motion, reported inactivity and illuminance |

Battery values appear only when received. A press or movement does not guarantee a temperature or battery update.

Each field keeps its own timestamp. Missing values remain unknown; silence does not become zero, closed or unoccupied. The default 180-second stale threshold marks old reports, and differs from the sensor's own occupancy-clear delay. Change it with `--stale-after 600` if needed.

Pausing the display does not stop reception or recording. Export downloads the current display state and recent history; complete raw and decoded recordings remain in `captures/`. These files can contain device identifiers and should stay private.

## Advanced GATT controls

Use the relevant connection button after preparing the sensor and its separate 12-byte Standard Auth token. A visible advertisement is not proof that the device is currently connectable.

- **ES5BB:** a connection authenticates, reads the catalog and requests the radar stream automatically. See [ES5 advanced controls](ES5BB_ADVANCED.md) for regions, sensitivity, PIR and calibration limits.
- **KS2BB:** connect to read button mode and display temperature unit. Setting changes require explicit actions and verified readback.
- **HS3BB:** short-press the physical button and connect promptly. Writes require confirmation of `linp.motion.hs1bb2`. See [wake-up evidence](HS3BB_WAKE.md).
- **MS1BB:** the released profile has no exposed GATT settings; use its advertisement panels.

## Shared library components

Live and replay panels use the public `Decoder`. All advanced GATT operations use the public `LinptechClient`: authentication, typed reads, verified settings, stream selection and fixed calibration/threshold commands. Controller workers adapt those observations and operations to the UI; they do not implement another BLE protocol.

Windows local GATT retains a separate worker process so blocked native cleanup cannot freeze HTTP or scanning. ESPHome keeps the controller in the same process as its shared proxy owner. Worker restarts invalidate current-read permissions and retain any unresolved calibration backup. For an application without HTTP or workers, use the same [async library API](LIBRARY.md).

Cancelling threshold application or restoration can leave a partial device change. The Dashboard retains the backup and marks restoration as required until all original values are independently verified; a closed connection is not evidence that settings were untouched.

The refactored Dashboard route is covered by synthetic encrypted-peer tests. Earlier sensor hardware results still apply to the documented library operations; this UI refactor has not been retested on physical sensors.

## Offline and proxy modes

For a source checkout, the supplied demo is wholly synthetic and requires no Bluetooth:

```powershell
.\.venv\Scripts\python.exe -m linptech_ble dashboard --config examples/demo.secrets.json --replay examples/demo.raw.jsonl --port 8766 --open
```

Replay preserves recorded timestamps, labels the display as historical, and disables GATT and bridge services. It cannot be combined with `--bridge-config`.

For a configured ESPHome installation:

```powershell
.\.venv\Scripts\python.exe -m linptech_ble dashboard --bridge-config bridge.local.json --open
```

Proxy hardware and Home Assistant still need deployment-specific validation. See [ESPHome and Home Assistant](ESPHOME_HOME_ASSISTANT.md).
