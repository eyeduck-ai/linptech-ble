# Windows dashboard

The dashboard is a local test application for ES5BB, MS1BB, KS2BB and HS3BB. The interface remains in Traditional Chinese. It runs without a Xiaomi gateway; monitoring uses credentials already stored locally and does not require a cloud login.

## Start

Run from the project folder. In an already configured checkout, double-click `start-dashboard.cmd`, or use PowerShell:

```powershell
.\.venv\Scripts\python.exe -m linptech_ble dashboard --open
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). Keep the terminal open and press Ctrl+C to stop. No Node.js build is needed. If the port is occupied, use `--port 8767`; starting a second instance may also compete for Bluetooth access.

New users must first create the Python environment: [developer tools](DEVTOOLS.md). Add sensors and enter credentials through the dashboard; see [credential setup](BINDKEY_WINDOWS.md) for obtaining your own keys. The repository does not include credentials for physical sensors.

Default startup:

- Starts local BLE advertisement reception, decoding and recording under `captures/`.
- Leaves GATT controllers waiting for a connection request. It does not automatically connect or change sensor settings.
- Keeps MQTT and ES5 automatic connection disabled. These require an explicit [bridge configuration](ESPHOME_HOME_ASSISTANT.md).
- Binds HTTP to `127.0.0.1` only. It does not expose the dashboard to other LAN devices.

## Test the sensors

The sensor list uses one row per device with its name, model, latest state, advertisement age and RSSI. Filter by model, or search by name/address to find a specific instance when several sensors share the same model. Filtering changes the display only; reception, recording and device settings are unchanged. Advanced controls open in a right-side dialog without expanding the device list. Close it with the close button or Escape.

KS2BB's temperature history remains inside its own expanded panel. It plots up to 180 actual decoded readings with their original timestamps; it does not fill silent periods with invented readings.

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

## Compare signal quality

Open the signal-quality view for a compact reception table and RSSI history. It shows observed signal levels, sample counts and reception gaps for placement checks. Filter by model/name/address and compare the same receiver at each location. The practical bands are project guidance; missed controlled events matter more than one RSSI cutoff. See [receiver placement and measurement limits](SIGNAL_QUALITY.md).

## Advanced GATT controls

Open **advanced settings** in the relevant sensor row for its connection, settings and advertisement diagnostics. ES5BB's dialog also contains radar, PIR, regions and calibration. Opening or closing the dialog sends no device command and does not disconnect an active session. Prepare the sensor and its separate 12-byte Standard Auth token before connecting; a visible advertisement is not proof that the device is currently connectable.

- **ES5BB:** a connection authenticates, reads the catalog and requests the radar stream automatically. See [ES5 advanced controls](ES5BB_ADVANCED.md) for regions, sensitivity, PIR and calibration limits.
- **KS2BB:** connect to read button mode and display temperature unit. Setting changes require explicit actions and verified readback.
- **HS3BB:** short-press the physical button and connect promptly. Writes require confirmation of `linp.motion.hs1bb2`. See [wake-up evidence](HS3BB_WAKE.md).
- **MS1BB:** the released profile has no exposed GATT settings; use its advertisement panels.

## Manage the device inventory

The four supported models are not a four-device limit. Multiple sensors of the same model are supported; give each a room name and use the model/name/address filters.

1. Select **Add sensor**, then start the 60-second nearby scan. Short-press a sensor's button if needed; do not hold it to reset.
2. Select a supported result, or enter its model and MAC manually. Set a name and optionally enter its 32-hex-character bindkey and 24-hex-character direct token.
3. Save. The local configuration, decoder and device list update immediately; no dashboard restart is needed.
4. To rename or update credentials, open that device's advanced dialog and select **Name and credentials**. Blank credential fields preserve saved values; stored secrets are never sent back to the page.
5. To remove a device, use the same form, tick the removal confirmation and select **Remove sensor**. This removes its local configuration and credentials without resetting or unbinding the sensor.

Scanning discovers advertisements, not credentials. RSSI is available without a bindkey; encrypted values require a correct bindkey, and supported GATT controls require the separate token. Discovery and editing are disabled in replay mode. Disconnect before replacing credentials or removing a connected device, and complete any pending calibration restoration first. Existing recordings remain on disk.

GUI changes are saved to the configuration selected at startup (`sensors.local.json` by default). Command-line tools remain available: [inventory commands](DEVTOOLS.md#add-rename-or-remove-sensors). Changes made by CLI or a text editor still require stopping and restarting the dashboard; external file edits are not watched.

## Shared library components

Live and replay panels use the public `Decoder`. All advanced GATT operations use the public `LinptechClient`: authentication, typed reads, verified settings, stream selection and fixed calibration/threshold commands. Controller workers adapt those observations and operations to the UI; they do not implement another BLE protocol.

Windows local GATT retains a separate worker process so blocked native cleanup cannot freeze HTTP or scanning. ESPHome keeps the controller in the same process as its shared proxy owner. Worker restarts invalidate current-read permissions and retain any unresolved calibration backup. For an application without HTTP or workers, use the same [async library API](LIBRARY.md).

Cancelling threshold application or restoration can leave a partial device change. The Dashboard retains the backup and marks restoration as required until all original values are independently verified; a closed connection is not evidence that settings were untouched.

The refactored route has synthetic encrypted-peer coverage and a physical ES5BB smoke through `LinptechClient`: 32-bin radar and 15 freshly reread, unchanged settings. No persistent settings or calibration were changed; streaming stopped and the session disconnected. KS2BB/HS3BB refactored UI paths and independent PIR energy remain outside this hardware smoke. See [hardware evidence and readback limits](HARDWARE_VALIDATION_2026-10-23.md).

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
