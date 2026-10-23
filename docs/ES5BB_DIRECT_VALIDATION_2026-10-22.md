# ES5BB hardware validation: 2026-10-22

Historical Windows local-BLE tests, firmware **2.1.2_0156**. No Xiaomi gateway.

- Standard Auth with the device's 12-byte token and encrypted BLE Spec succeeded.
- 19 valid properties were read. NULL fields remained unavailable.
- Two 32-bin radar-energy streams and bitmaps ran continuously for 181 seconds without recorded disconnect or worker restart. This is not a long-term stability guarantee.
- No-occupancy delay **30 -> 31 -> 30 s** and PIR sensitivity **3 -> 2 -> 3** were independently read back and restored.
- A 30-second empty-room threshold preview completed. Suggested thresholds matched the originals, so nothing was applied.
- Occupancy 0 and 1 were seen in different sessions; no same-session clearance-timing claim is made.
- Existing regions and threshold arrays were not changed. Calibration command effects and threshold application remain unverified.
- Historical advertisement decoding confirmed ES5 battery reporting. KS2 battery remained protocol-only.

Later PIR energy and HS3 tests are recorded in [2026-10-23 hardware validation](HARDWARE_VALIDATION_2026-10-23.md). For current behavior use [device capabilities](DEVICE_CAPABILITIES.md), [PIR findings](PIR_INVESTIGATION.md) and the [ES5 guide](ES5BB_ADVANCED.md).
