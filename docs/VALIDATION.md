# Validation overview

Version **0.4.0 passed 505 tests** on Windows on 2026-10-23 (51.53 seconds). This includes the Dashboard-to-public-client migration, encrypted synthetic peers, threshold commit guards, cancellation/backup recovery and distribution checks. The earlier 0.3.0 baseline had 479 tests. See [development tools](DEVTOOLS.md) to rerun the source suite.

| Layer | Evidence | Limit |
| --- | --- | --- |
| Advertisement decoding | Synthetic AES-CCM fixtures and authenticated captures from all four models | Not every protocol-defined object has been received |
| Public GATT client | ES5BB, KS2BB and HS3BB authentication and selected reads/writes | Device/firmware-specific; MS1BB has no released settings profile |
| ES5BB PIR | Trigger flag 0/1 observed; matrix of stream sequences tested | Independent energy still absent; native SDK subscription details unknown |
| Windows Dashboard | Earlier real broadcasts; new UI started with live reception, and public-client GATT routing checked with synthetic encrypted peers and proxy loopback | Refactored GATT path not yet retested on sensors; replay is not live state |
| ESPHome / HA MQTT | Protocol peers, lifecycle tests and synthetic UI | No physical ESP32 or full HA installation yet |
| Distribution | Core-only wheel upgrade, offline sdist rebuild, source/resource/link audits | Not a cross-platform hardware certification |

Writes under test were followed by independent readback and restoration. The latest ES5BB matrix verified all 24 valid original settings at exit; HS3BB's wake experiment was read-only. No reset or firmware update was performed.

Details: [hardware](HARDWARE_VALIDATION_2026-10-23.md), [ES5BB earlier session](ES5BB_DIRECT_VALIDATION_2026-10-22.md), [PIR](PIR_INVESTIGATION.md), [HS3BB wake](HS3BB_WAKE.md), [proxy simulation](PROXY_HA_VALIDATION_2026-10-22.md).
