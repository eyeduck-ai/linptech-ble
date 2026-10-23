# Hardware validation: 2026-10-23

Windows, Python 3.11, local BLE and each device's own credentials. Public `LinptechClient` calls were tested independently of the Dashboard. No Xiaomi gateway or MQTT was involved. Results apply to the tested units and firmware.

| Model | Verified | Restoration / limits |
| --- | --- | --- |
| KS2BB | Standard Auth; temperature unit 0 -> 1 -> 0; button mode 0 -> 1 -> 0 | Both settings freshly read back and restored; all fast-mode button behavior not exhaustively tested |
| HS3BB | Standard Auth; indicator true -> false -> true; interval 30 -> 40 -> 30 s | Four valid baseline settings matched at final readback |
| HS3BB | No-motion time 10 min; PIR single-report false | Read only; A/B timeouts returned out-of-spec 0, never written |
| ES5BB | Standard Auth, configuration reads, 32-bin radar and PIR trigger 0/1 | 24 valid original settings matched after mode 2 -> 1 -> 2; stream stopped |
| MS1BB | Authenticated opening/closing advertisements | Released profile exposes no GATT settings; no writes attempted |

ES5BB's independent `6.18` PIR energy remained absent in 60/180-second trials and the later sequence/mode matrix. ACKs are not data. See [PIR investigation](PIR_INVESTIGATION.md).

A later HS3BB read-only trial received connectable advertising and authenticated after a short button press; no writes. See [wake procedure](HS3BB_WAKE.md).

All four advertisement keys were previously verified with MIC-authenticated packets. A session without a new battery report does not refresh the historical battery value. Physical ESP32 transport and complete Home Assistant deployment have not been tested.

The public inventory separates observed advertisements, protocol-only fields, readable settings and restored writes. See [capabilities](DEVICE_CAPABILITIES.md) and the [validation overview](VALIDATION.md). Private captures and original-value backups are not distributed.
