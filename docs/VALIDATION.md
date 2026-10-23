# Validation

Run the synthetic suite with `python -m pytest -q`; see [contributing](../CONTRIBUTING.md) for test dependencies. Tests use generated credentials and peers. Hardware results apply to the tested devices and firmware.

| Layer | Evidence | Limit |
| --- | --- | --- |
| Advertisement decoding | Synthetic AES-CCM fixtures and authenticated packets from all four models | Not every defined object has been received |
| Public GATT client | ES5BB, KS2BB and HS3BB authentication and selected reads/writes | Model/firmware-specific; MS1BB has no released settings profile |
| ES5BB PIR | Trigger flag 0/1 observed; multiple stream sequences tested | Independent energy remains absent |
| Dashboard | Live advertisements and ES5BB public-client radar; synthetic inventory, QR flow and RSSI views | Refactored KS2BB/HS3BB UI paths remain synthetic-only |
| QR credential setup | Integrated `cn` login and key save for all four models; live KS2BB packets decrypted afterwards | Other regions and account variants remain unverified |
| ESPHome | Real `bleak-esphome` backend against a synthetic Native API peer | No physical ESP32 tests |
| Distribution | Wheel/sdist, resource, dependency and documentation-link checks | No cross-platform hardware certification |

## Hardware

All four models produced MIC-authenticated advertisements. KS2BB temperature/humidity and single/double/long press, MS1BB opening/closing and battery, HS3BB motion/illuminance, and ES5BB occupancy/illuminance/near-range/battery were observed across local Windows sessions. A received value proves that report, not a guaranteed reporting interval or sensor accuracy.

Selected KS2BB and HS3BB settings passed independent readback and restoration. ES5BB tests verified radar streams, PIR trigger flags and occupied/clear transitions. Independent PIR energy, calibration effects and threshold application remain unverified. Tested writes were restored; no reset or firmware update was performed. Details: [hardware evidence](HARDWARE_VALIDATION.md), [PIR investigation](PIR_INVESTIGATION.md), [HS3BB wake](HS3BB_WAKE.md).

## Application and transport boundaries

Synthetic encrypted peers exercise authentication, typed reads, guarded writes, readback, stream cleanup and early notifications before the session-start acknowledgement. Missing, invalid or stale observations remain unavailable rather than becoming zero or false.

Dashboard checks cover per-address controls, same-model filters, independent drafts, receiver-aware RSSI history, CSV provenance, drawer behavior and pause/replay restrictions. Isolated browser tests scan, add, rename and remove devices without restarting. Credential transactions preserve unselected devices and reject active or concurrently edited targets. Thread tests prevent replaced decoders or retired controllers from restoring obsolete state.

The integrated QR flow completed a real `cn` login and saved bindkeys plus compatible direct tokens for all four selected models. Reloaded credentials remained valid; subsequent KS2BB packets passed MIC verification. Synthetic tests separately cover cancellation/expiry, encrypted cloud requests, redacted HTTP boundaries, selection persistence, unselected credential preservation and concurrently edited targets. Result displays distinguish retrieved credentials from preserved values. Account access and sensor credentials are never included in public fixtures.

ESPHome tests exercise shared advertisement/GATT ownership, local/remote routing, partial disconnections and the production authentication protocol. Example firmware was reviewed, not compiled or flashed. Actual radio range, proxy latency and long-term reconnection behavior require physical deployment testing.
