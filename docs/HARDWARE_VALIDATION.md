# Hardware validation

Observed on Windows, Python 3.11 and local BLE on 2026-10-21 through 2026-10-23, using each sensor's own credentials. Public `LinptechClient` tests preceded an ES5BB Dashboard smoke. No Xiaomi gateway was used. Results apply to the tested units and firmware.

## Advertisements and settings

All four models produced MIC-authenticated advertisements; [device capabilities](DEVICE_CAPABILITIES.md) identifies observed and protocol-only objects. Historical battery reports remain historical until another packet updates them. KS2BB battery is defined but unobserved.

| Model | Verified | Restoration / limits |
| --- | --- | --- |
| KS2BB | Authentication; unit `0 → 1 → 0`; button mode `0 → 1 → 0` | Both settings freshly reread and restored; quick-click behavior not exhaustively tested |
| HS3BB | Authentication; indicator `true → false → true`; interval `30 → 40 → 30 s` | Four valid baseline settings matched at exit |
| HS3BB | No-motion time 10 min; PIR single-report false | Read only; A/B timeouts returned out-of-spec zero and were never written |
| ES5BB | Authentication, radar arrays, PIR trigger 0/1; detection mode `2 → 1 → 2` | All 24 valid original settings matched at exit; stream stopped |
| MS1BB | Opening/closing advertisements | Released profile exposes no GATT settings; no writes attempted |

An earlier ES5BB session on firmware `2.1.2_0156` read 19 valid properties and streamed both 32-bin radar arrays for 181 seconds without a recorded disconnect. No-occupancy delay `30 → 31 → 30 s` and PIR sensitivity `3 → 2 → 3` passed independent readback/restoration. A 30-second empty-room preview matched the original thresholds, so nothing was applied. Calibration effects, threshold application and long-term stability were not established.

Independent PIR energy `6.18` remained absent in 60/180-second trials and later sequence/mode comparisons, despite valid trigger flags and radar. See [PIR investigation](PIR_INVESTIGATION.md). HS3BB short-button wake followed by connectable advertising, authentication and reads was observed in a separate read-only trial; see [wake procedure](HS3BB_WAKE.md).

## Dashboard through the public client

The Dashboard received both 32-bin radar arrays and 16 distinct fresh timestamps over 32 seconds. Of 17 baseline values, 15 were freshly reread unchanged; optional `pir_threshold` and `indicator_light` were excluded from this readback claim. Diagnostics recorded 113 requests, 186 verified messages and 225 operation-4 properties, with no parse/callback errors.

Writes were temporary stream controls only. Streaming stopped, the notification reader ended and the session disconnected with no restoration pending. This smoke did not validate PIR energy, settings writes or calibration. Refactored KS2BB/HS3BB Dashboard paths remain synthetic-only; their standalone-client hardware results above are separate evidence.

## ES5BB session-start ordering and clearance

A valid operation-4 property notification arrived before the BLE Spec `0xF0` session-start echo. The former first-message-only check rejected this ordering. The client now validates and buffers early properties until the exact echo/transaction ID arrives, bounded to eight seconds and 64 messages. Invalid MICs, malformed messages, counter rollback and wrong acknowledgements still fail; exact retransmissions are not decoded twice.

A physical probe reproduced the early notification and authenticated after the fix. Other rapid connection attempts still timed out, at approximately -81 to -89 dBm; the fix does not establish that all connection failures are resolved. The current firmware version was not freshly obtained.

Seventeen valid writable settings matched earlier readings. Detection covered 0–4.5 m, vacancy delay was 30 seconds and near-range interest was disabled. Radar threshold crossings in enabled 3.25–4.25 m bins coincided with occupied status; this alone does not prove whether the room was empty or calibration suited the environment.

A later bounded trial performed 54 occupancy reads while streaming radar. After the user left the full range for at least 60 seconds and returned, occupancy changed `occupied → clear` at 17:31:32 local time and back to `occupied` at 17:32:37. The clear sample had a zero occupied-region bitmap; the return had PIR activity and occupied regions. All five selected settings were freshly reread unchanged, then streaming stopped and the client disconnected. No persistent setting writes or calibration were issued.

Physical ESPHome transport remains untested. Private captures and original-value backups are not distributed. See [validation scope](VALIDATION.md).
