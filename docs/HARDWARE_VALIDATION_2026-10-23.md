# Hardware validation: 2026-10-23

Windows, Python 3.11, local BLE and each device's own credentials. Public `LinptechClient` calls were first tested independently of the Dashboard, followed by an ES5BB Dashboard smoke. No Xiaomi gateway or MQTT was involved. Results apply to the tested units and firmware.

| Model | Verified | Restoration / limits |
| --- | --- | --- |
| KS2BB | Standard Auth; temperature unit 0 -> 1 -> 0; button mode 0 -> 1 -> 0 | Both settings freshly read back and restored; all fast-mode button behavior not exhaustively tested |
| HS3BB | Standard Auth; indicator true -> false -> true; interval 30 -> 40 -> 30 s | Four valid baseline settings matched at final readback |
| HS3BB | No-motion time 10 min; PIR single-report false | Read only; A/B timeouts returned out-of-spec 0, never written |
| ES5BB | Standard Auth, configuration reads, 32-bin radar and PIR trigger 0/1 | 24 valid original settings matched after mode 2 -> 1 -> 2; stream stopped |
| MS1BB | Authenticated opening/closing advertisements | Released profile exposes no GATT settings; no writes attempted |

ES5BB's independent `6.18` PIR energy remained absent in 60/180-second trials and the later sequence/mode matrix. ACKs are not data. See [PIR investigation](PIR_INVESTIGATION.md).

A later HS3BB read-only trial received connectable advertising and authenticated after a short button press; no writes. See [wake procedure](HS3BB_WAKE.md).

## ES5BB Dashboard public-client smoke

The refactored Dashboard authenticated through public `LinptechClient` and received both 32-bin radar energy arrays. Observation over 32 seconds confirmed 16 distinct fresh radar timestamps. Of 17 baseline setting values, 15 were freshly reread unchanged; optional `pir_threshold` and `indicator_light` were not freshly reread and are excluded from that readback claim.

Device writes were limited to temporary stream controls: no persistent setting changes or calibration. The session ended disconnected, with streaming off, its notification reader stopped and no restoration pending. Diagnostics recorded 113 requests sent, 186 verified messages and 225 operation-4 properties, with zero parse or callback errors. All three write acknowledgements were stream-command acknowledgements.

This radar-mode smoke did not validate independent PIR energy. KS2BB/HS3BB refactored Dashboard paths remain covered by synthetic peers only; their earlier standalone-client hardware results above are separate evidence.

All four advertisement keys were previously verified with MIC-authenticated packets. A session without a new battery report does not refresh the historical battery value. Physical ESP32 transport and complete Home Assistant deployment have not been tested.

## ES5BB session-start ordering

A later read-only diagnosis observed an authenticated operation-4 property notification before the BLE Spec `0xF0` session-start echo. The former first-message-only check rejected this valid ordering. The library now strictly validates and buffers initial properties, publishing them only after an exact echo with the expected transaction ID. Startup is bounded to eight seconds and 64 messages; malformed data, invalid MICs, counter rollback and wrong acknowledgements still fail. Exact retransmissions are not decoded twice.

A physical authentication probe reproduced the early operation-4 ordering after the fix and completed authentication, then disconnected. Separate rapid connection attempts still timed out; this is not evidence that all radio/connection failures are resolved. The receiver observed roughly -81 to -89 dBm during this diagnosis, and the current firmware version was not freshly obtained.

The same diagnosis received occupancy, close-range, illuminance and battery broadcasts with existing credentials. Seventeen valid writable settings matched earlier successful readings: no persistent setting drift was found. The configured detection region covered 0-4.5 m, vacancy delay was 30 seconds and close-range interest reporting was disabled. Radar data had threshold crossings in enabled 3.25-4.25 m bins while the device reported occupied; this alone does not establish whether the room was physically empty or whether calibration fits the environment. No calibration or persistent setting write was performed.

A subsequent bounded public-client trial polled occupancy every three seconds while streaming radar. With the user leaving the full detection area for at least 60 seconds and returning, it observed occupied -> clear at 17:31:32 local time -> occupied at 17:32:37. The clear sample had an occupied-region bitmap of zero; the return sample included a PIR trigger and nonzero occupied regions. This validates the tested transition, not all household conditions or a universal clearing time.

That trial completed 54 occupancy reads. All five selected writable settings were freshly reread unchanged at exit; the stream context stopped and the client disconnected. No calibration or persistent setting writes were issued.

The public inventory separates observed advertisements, protocol-only fields, readable settings and restored writes. See [capabilities](DEVICE_CAPABILITIES.md) and the [validation overview](VALIDATION.md). Private captures and original-value backups are not distributed.
