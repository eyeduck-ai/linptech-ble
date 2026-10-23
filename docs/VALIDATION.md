# Validation overview

Version **0.4.0 passed 591 tests** on Windows on 2026-10-23 (56.50 seconds). This includes public-client integration, encrypted peers, threshold commit guards, cancellation/backup recovery, distribution, per-address controls, receiver-aware RSSI history, inventory commands and UI filtering. The earlier 0.3.0 baseline had 479 tests. See [development tools](DEVTOOLS.md) to rerun the source suite.

| Layer | Evidence | Limit |
| --- | --- | --- |
| Advertisement decoding | Synthetic AES-CCM fixtures and authenticated captures from all four models | Not every protocol-defined object has been received |
| Public GATT client | ES5BB, KS2BB and HS3BB authentication and selected reads/writes | Device/firmware-specific; MS1BB has no released settings profile |
| ES5BB PIR | Trigger flag 0/1 observed; matrix of stream sequences tested | Independent energy still absent; native SDK subscription details unknown |
| Windows Dashboard | Live advertisements; ES5BB public-client GATT smoke confirmed 32-bin radar, 16 fresh timestamps in 32 seconds and 15 unchanged settings by fresh readback | KS2BB/HS3BB refactored UI paths remain synthetic-only; radar-mode smoke did not validate PIR or settings writes |
| ESPHome / HA MQTT | Protocol peers, lifecycle tests and synthetic UI | No physical ESP32 or full HA installation yet |
| Distribution | Core-only wheel upgrade, offline sdist rebuild, source/resource/link audits | Not a cross-platform hardware certification |

Writes under test were followed by independent readback and restoration. The latest ES5BB matrix verified all 24 valid original settings at exit; HS3BB's wake experiment was read-only. No reset or firmware update was performed.

The later ES5BB Dashboard smoke used only temporary stream controls, with no persistent setting changes or calibration. Of 17 baseline setting values, 15 were freshly reread unchanged; optional `pir_threshold` and `indicator_light` were not freshly reread. It ended disconnected, with streaming off and no restoration pending. See the [hardware evidence](HARDWARE_VALIDATION_2026-10-23.md).

The row and signal-survey views were checked in the live browser and an isolated seven-device/two-receiver server. Same-model filters and per-address drafts remain separate; writes to the second ES5BB/KS2BB leave the first unchanged. Receiver selection and CSV exports retain sample provenance. Menu toggles send no command, pause/replay disable editing, and device-list changes preserve controls. These UI checks made no physical setting changes. Live Windows reception supplies the new RSSI histories; physical ESPHome reception remains untested.

Details: [hardware](HARDWARE_VALIDATION_2026-10-23.md), [ES5BB earlier session](ES5BB_DIRECT_VALIDATION_2026-10-22.md), [PIR](PIR_INVESTIGATION.md), [HS3BB wake](HS3BB_WAKE.md), [proxy simulation](PROXY_HA_VALIDATION_2026-10-22.md).
