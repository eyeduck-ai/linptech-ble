# Device capabilities

This guide separates authenticated advertisements, published protocol definitions, and hardware-tested GATT features. No Xiaomi gateway is required. See the [library guide](LIBRARY.md) for the SDK and the [capability API](CAPABILITY_API.md) for machine-readable evidence.

## Supported devices

| Device | PID | Confirmed MIoT model | Observed advertisements | Additional GATT features |
| --- | --- | --- | --- | --- |
| ES5BB | `0x6A0F` | `linp.sensor_occupy.es5b` | Illuminance, occupancy, close-range occupancy, battery | Radar bins and thresholds, region masks, PIR trigger flag, settings and calibration commands |
| MS1BB | `0x1889` | `linp.magnet.m1` | Opening/closing state, door events, battery | No exposed sensor settings in released v1 |
| KS2BB | `0x520B` | `linp.sensor_ht.ks2bb` | Temperature, humidity, single/double/long press | Button mode and display temperature unit |
| HS3BB | `0x50D4` | `linp.motion.hs1bb2` | Motion with illuminance, no-motion duration, custom timeout, battery | Six setting definitions; tested support varies by field |

Model confirmation applies to the tested devices. HS3BB callers must still supply their own confirmed `miot_model`; the library does not infer safe writes from a product name alone. Published specifications are included in [the package](../linptech_ble/specs/); the [official MIoT index](https://miot-spec.org/miot-spec-v2/instances?status=all) lists released and debug variants.

## ES5BB

**Observed advertising objects:** `0x4805` illuminance, `0x484E` occupancy, `0x5413` close-range occupancy, `0x5C03` battery. Duration reports `0x4851/0x4852` and special events `0x5661` have protocol definitions but have not been captured. No independent PIR signal or radar energy has been observed in advertisements.

**Direct connection:** the catalog contains 56 properties and 27 writable definitions. Radar notifications provide two sets of 32 energy bins, thresholds, occupied-region information, noise quality, and the global PIR trigger flag in `6.8`. These are not precise person tracking or per-distance PIR measurements.

Hardware evidence includes valid reads and final matching readbacks for **24 settings**. No-one delay, PIR sensitivity, and detection mode have write/readback/restore evidence; the investigation changed detection mode `2 → 1 → 2`. Reading the remaining settings does not verify their writes. Occupancy and power-mode notifications are verified separately from GET support.

The PIR trigger flag works. Independent raw PIR energy `6.18` remains **unconfirmed**: several command/subscription sequences and modes produced no energy samples, despite working radar controls and valid PIR flags. Firmware not exposing this debug channel is likely, but unpublished native SDK details remain a possible difference. Official v113 hides that debug chart for ES5 while using the `6.8` trigger in its normal UI. Firmware `2.1.2_0156` is a historical observation, not a successful new version query.

See [ES5 advanced features](ES5BB_ADVANCED.md), the [PIR investigation](PIR_INVESTIGATION.md), and the [released specification](../linptech_ble/specs/linp.sensor_occupy.es5b.json).

## MS1BB

**Observed objects:** `0x4804` door state, `0x4A12` door event carrying open/closed values, `0x4C03` battery. Separate close `0x4A13`, tamper `0x4A0F`, and door-left-open `0x4A1A` events are defined but unobserved. The upstream interpretation of `0x4A13` conflicts with the official close event; this library does not invent a button press from it.

Released v1 exposes **no GATT sensor settings**. The debug v2 not-closed-duration setting is not applied to these devices. No model-specific connection wake procedure is established. See the [released specification](../linptech_ble/specs/linp.magnet.m1.json).

## KS2BB

**Observed objects:** `0x4801` temperature, `0x4802` humidity, and zero-length `0x560C/0x560D/0x560E` button events. Battery `0x5003` and reset `0x561C` are defined but **not observed**. Older `0x4C01/0x4C08` forms are compatibility handlers, not evidence of current-device reporting.

GATT `6.1 button_mode` and `6.2 temperature_unit` both passed authenticated read, write, independent readback, and restoration. Quick-click behavior was not exhaustively tested. Changing the display to Fahrenheit does not change the Celsius broadcast temperature definition. No specific button wake procedure is established. See the [released specification](../linptech_ble/specs/linp.sensor_ht.ks2bb.json).

## HS3BB

**Observed objects:** `0x4805` illuminance, `0x4818` no-motion duration in seconds, `0x4835` custom timeout in minutes, `0x4A08` motion with illuminance, and `0x4C03` battery. Reset `0x4A1C` is defined but unobserved. A captured zero for `0x4835` is outside the official 1–60 minute range and remains diagnostic data.

| GATT setting | Hardware evidence |
| --- | --- |
| `2.2 detection_interval` | Write/readback/restore verified |
| `4.1 indicator_light` | Write/readback/restore verified |
| `2.1053 no_motion_time`, `5.3 pir_single_report` | Valid reads; writes untested |
| `5.1/5.2 no_motion_timeout_1/2` | Returned invalid zero, outside 60–3600 seconds; **not written** |

A **single short press** was followed by fresh connectable advertising, successful Standard Auth, and valid setting reads on the tested device. Do not long-press or reset. The before/after observations used separate scan windows, so a universal requirement or sole cause of earlier failures is not established (`wake.required=null`). See [wake evidence](HS3BB_WAKE.md) and the [released specification](../linptech_ble/specs/linp.motion.hs1bb2.json).

## Shared limits

Advertising uses a 16-byte bindkey; ES5BB, KS2BB, and HS3BB direct authentication has been tested with their own 12-byte Standard Auth tokens. An ESPHome API key is a separate credential. Once credentials are available, runtime decoding and GATT do not need a Xiaomi account session.

A received advertisement does not guarantee current connectability. Writes require an authenticated session, a valid current read, model and value checks, and independent readback. Unknown, stale, or invalid values never become fabricated zero/false states.

These profiles implement MiBeacon and GATT, not BLE Mesh provisioning or relaying. Local Windows BLE is hardware-tested; [ESPHome transport](ESPHOME_PROXY.md) has synthetic protocol tests and awaits physical deployment validation. Detailed evidence is in the [hardware report](HARDWARE_VALIDATION.md).
