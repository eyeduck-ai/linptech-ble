# ES5BB advanced controls

Model: `linp.sensor_occupy.es5b`. The project provides authenticated BLE Spec access through local Windows BLE or optional ESPHome transport, without a Xiaomi gateway. Runtime control uses a separate 12-byte Standard Auth token, not the 16-byte advertisement bindkey. Supply your own credentials using the [credential guide](BINDKEY_WINDOWS.md).

## Connect from the dashboard

1. Start `start-dashboard.cmd`, or run the following from the project folder:

   ```powershell
   .\.venv\Scripts\python.exe -m linptech_ble dashboard --open
   ```

2. Keep ES5BB powered and close to the computer or selected proxy. Release the phone's connection; on iPhone, turn Bluetooth off in Settings rather than Control Center.
3. Request ES5BB connection in the advanced workbench, then short-press its physical button once. Do not long-press or reset it.
4. Wait for authentication, property reads and fresh radar samples. Some firmware fields may remain unavailable. The ordinary advertisement card does not indicate a GATT connection.

The dashboard starts radar reporting after connection. The public `LinptechClient.connect()` only authenticates; library callers explicitly select a stream. See the [library guide](LIBRARY.md).

## Interpret the signals

| Data | Meaning and limits |
| --- | --- |
| `6.4` / `6.5` | Motion/presence radar energy, 32 bins of 0.25 m covering 0-8 m. Raw 0-255 values, not dBm. |
| `6.2` / `6.3` | The corresponding per-bin thresholds. Crossing a threshold alone does not establish fused occupancy. |
| `6.7` / `6.17` | Detection and close-interest region masks. These are regions, not exact person tracking or a person count. |
| `6.8` | Radar result bitmaps and diagnostic fields. Its eighth CSV field contains the overall PIR trigger flag when present. |
| `6.18` | Separate signed PIR energy, specified as int32 notify, range -1000 to 1000. It is not a per-distance infrared measurement; its physical unit is unconfirmed. |
| `6.19` | PIR threshold, uint8. Writing requires an explicit custom PIR sensitivity and a fresh check before the write. |
| `2.1078` | The sensor's fused occupancy decision. It is distinct from radar bins and the PIR trigger. |

The dashboard can show each bin's energy, threshold and enabled region. Energy/bitmap data older than eight seconds is shown as a last reading. Disconnection preserves historical values without claiming they are live.

The vendor noise-quality indicator uses >95 as normal, 92-95 as mildly abnormal and <92 as severely abnormal. This is a vendor score, not dB.

## PIR validation boundary

**The PIR trigger flag works; independent PIR energy remains unconfirmed on the tested device.** The latest test recorded real `6.8` trigger values of both 0 and 1. Radar streaming worked, but 60/180-second tests and several command-order comparisons produced no valid `6.18` energy notifications.

The available PIR stream action uses the plugin-derived command `6.9=4`; returning to radar uses command 1, and stopping uses 0. A command acknowledgement or NULL snapshot does not prove that energy is being produced. Missing PIR data remains unknown, never zero or false. Ordinary occupancy advertisements cannot reconstruct an independent PIR signal.

The public API's `diagnostics()` separates raw notifications, ACKs, NULL replies and model-valid op4 updates. Its `pir_data_received` flag means a valid PIR update has been received in that session, not that it is still fresh. See the [PIR investigation](PIR_INVESTIGATION.md) for the evidence and unresolved native-SDK subscription details. PIR threshold writing and the availability of this development stream across other firmware remain unvalidated.

## Settings and occupancy clearing

Choose regions, sensitivity, detection mode and no-occupancy delay explicitly. The dashboard enables writes only after valid reads in the current connection, then checks a fresh readback. Presets may affect related thresholds; change one setting at a time and test the resulting behavior.

Occupancy clears only when the device itself decides the area is empty for its configured delay. Movement, interference or region/threshold choices can prevent that timer from completing. The dashboard does not infer an empty room from missing packets. Its stale threshold is a display rule, not a substitute for the device's clear delay.

Battery percentage and power-source mode are separate fields. External power does not imply a full battery, and unsupported battery reads remain unavailable rather than becoming 0%.

## Calibration

Fix the sensor in its intended position and prepare an empty detection area. The dashboard requests explicit preparation confirmation and provides a leave-room countdown.

- **Radar recalibration:** sends the fixed vendor calibration command and allows a settling period. There is no verified device completion flag; command acceptance and a finished countdown do not validate calibration effectiveness.
- **Empty-room threshold scan:** waits for explicit empty occupancy and fresh energy, then collects 30 continuous seconds. Motion or interrupted data restarts collection; total waiting is bounded. A preview precedes any write. Each proposed threshold is `max(original, min(255, observed maximum + 6))`.
- **Apply/restore:** originals are recorded before writes, applied values are read back, and failures trigger a restoration attempt. If restoration cannot be confirmed, use the saved backup and verify it after reconnecting. Backups are retained in the private direct-session recording.

Higher thresholds may reduce sensitivity to small movements. The current round did not validate the physical effect of calibration; test entry, stationary presence and clearing after any intentional change. No reset, rebinding or firmware-update command is exposed.

## Troubleshooting and evidence

An acknowledgement without fresh samples is not a working data stream. Check the per-field timestamps and diagnostics before changing settings. Move closer, release other clients and short-press to wake the sensor. Windows may need one discovery pass to populate its device cache; the transport performs one bounded fallback only for a device-not-found error.

Keep `captures/`, credentials and full diagnostic recordings private. For protocol details and validation boundaries, see [direct protocol](DIRECT_PROTOCOL.md), [hardware tests](HARDWARE_VALIDATION_2026-10-23.md) and [device capabilities](DEVICE_CAPABILITIES.md). ESPHome transport has protocol tests, but has not yet been validated with a physical proxy in this project.
