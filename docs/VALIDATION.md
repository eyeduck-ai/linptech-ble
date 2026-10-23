# Validation overview

The 0.3.0 implementation passed **479 tests** on Windows on 2026-10-23 before the English documentation cleanup. See [development tools](DEVTOOLS.md) to rerun the source suite; test counts may change.

| Layer | Evidence | Limit |
| --- | --- | --- |
| Advertisement decoding | Synthetic AES-CCM fixtures and authenticated captures from all four models | Not every protocol-defined object has been received |
| Public GATT client | ES5BB, KS2BB and HS3BB authentication and selected reads/writes | Device/firmware-specific; MS1BB has no released settings profile |
| ES5BB PIR | Trigger flag 0/1 observed; matrix of stream sequences tested | Independent energy still absent; native SDK subscription details unknown |
| Windows Dashboard | Real broadcasts, UI rendering and offline replay | Old values/replay are not current state |
| ESPHome / HA MQTT | Protocol peers, lifecycle tests and synthetic UI | No physical ESP32 or full HA installation yet |
| Distribution | Core-only wheel upgrade, offline sdist rebuild, source/resource/link audits | Not a cross-platform hardware certification |

Writes under test were followed by independent readback and restoration. The latest ES5BB matrix verified all 24 valid original settings at exit; HS3BB's wake experiment was read-only. No reset or firmware update was performed.

Details: [hardware](HARDWARE_VALIDATION_2026-10-23.md), [ES5BB earlier session](ES5BB_DIRECT_VALIDATION_2026-10-22.md), [PIR](PIR_INVESTIGATION.md), [HS3BB wake](HS3BB_WAKE.md), [proxy simulation](PROXY_HA_VALIDATION_2026-10-22.md).
