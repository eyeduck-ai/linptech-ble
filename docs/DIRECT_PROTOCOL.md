# Authenticated BLE protocol

Applies to the tested ES5BB (`linp.sensor_occupy.es5b`) and compatible Standard Auth / BLE Spec profiles. No Xiaomi gateway is used. Runtime connections do not call Xiaomi cloud services.

## Authentication and framing

- MiBeacon bindkey: **16 bytes**, for advertisements. Standard Auth token: **12 bytes**, for GATT. They are not interchangeable.
- FE95 characteristics 0010 / 0019 / 001A / 001B provide login, authentication, write and notify channels.
- Login uses random challenges, HKDF-SHA256 (`mible-login-info`), HMAC proofs and the `0x24 -> 0x21` acknowledgement.
- Session data uses AES-CCM with a four-byte MIC. Replayed or obsolete packets cannot overwrite new state. Counters are not reused after exhaustion.
- BLE Spec uses a 12-bit packet/property length and a transaction ID. Parsed operations are write ACK (1), snapshot (3) and update (4). NULL is unavailable, not zero.
- One reader decrypts and routes responses. Writes require independent readback, not just a transport ACK.

Op2 is a snapshot probe in this implementation. The public Xiaomi JavaScript SDK does not expose its native subscription wire protocol; this project does not claim op2 fully reproduces it.

## ES5BB fields

| Field | Meaning |
| --- | --- |
| `2.1078` / `2.5` | Occupancy / no-occupancy delay in seconds |
| `6.2` / `6.3` | Motion / presence thresholds: 32 hex bytes in CSV |
| `6.4` / `6.5` | Motion / presence energy: 32 hex bytes in CSV |
| `6.7` / `6.17` | Detection / interest-region bit masks |
| `6.8` | Radar masks, noise quality and flags; CSV field 8 is the overall PIR flag |
| `6.18` | Independent signed PIR energy; no valid samples from the tested flows |
| `6.19` | PIR threshold; reading verified, writing not hardware-verified |
| `6.63` | Power mode, separate from battery percentage |
| `7.1003` | Battery percentage; direct availability is firmware-dependent |

Radar has 32 bins of 0.25 m. Bins describe echoes, not precise tracking or people counts. Raw energy is not dBm. Normal PIR is an overall trigger flag, not per-distance infrared energy.

Fixed commands on `6.9`: 0 stops streams, 1 starts radar, 4 requests PIR debug energy, 256 requests radar calibration, 512 applies the supported threshold workflow. An ACK does not prove data production or calibration completion. No arbitrary commands, factory reset or firmware update are exposed. See [PIR findings](PIR_INVESTIGATION.md) and [calibration limits](ES5BB_ADVANCED.md).

## Transport and ownership

`LinptechClient` owns the authenticated protocol path and uses `StandardSession` internally. Dashboard controllers call this public API for all GATT operations; they do not construct sessions, access raw BLE characteristics or use client internals. The library needs no Dashboard or worker process.

Local Windows Dashboard connections retain process isolation around the public client. ESPHome uses one shared API owner for advertisements and GATT. Physical proxy operation remains unverified. Fixed threshold commit is exposed through `commit_radar_thresholds(empty_room=True)`; callers must still verify the final threshold group after sensitivity changes. Calibration or commit ACKs are not completion measurements.

## Sources

- [Official ES5 MIoT specification](https://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:occupancy-sensor:0000A0BF:linp-es5b:1).
- [Xiaomi BLE Spec SDK](https://github.com/MiEcosystem/miot-plugin-sdk/blob/master/miot-sdk/device/bluetooth/blespec.js) and [BluetoothDevice API](https://github.com/MiEcosystem/miot-plugin-sdk/blob/master/miot-sdk/device/bluetooth/BluetoothDevice.js).
- Authorized static inspection of official ES5 plugin v113, project 1.1.10: field meanings, 0.25 m bins, command constants and UI behavior. Vendor code and signed download URLs are excluded from this repository.
- Android plugin SHA-256: `a362cea1703c48d60b868db889098537e14598d5789435e2c8faaa21950569ff`; iOS: `6bdfbddceb7dba2be6009ae8888a9967a49160a5d465149faf4fd159fb7fc24a`.
- [MIT Standard Auth research](https://github.com/opravdin/hass-yunmi-kettle-ble); attribution is retained in [third-party notices](../THIRD_PARTY_NOTICES.md).
