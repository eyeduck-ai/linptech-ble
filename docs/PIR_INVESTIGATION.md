# ES5BB PIR: trigger works, debug energy unconfirmed

Tested on Windows local BLE on 2026-10-23, without a Xiaomi gateway. The phone's Bluetooth was off and the user alternated movement and stillness in front of the sensor.

| Data | Result |
| --- | --- |
| Overall PIR trigger, `6.8` CSV field 8 | 33 valid updates: flag 0 appeared 31 times; flag 1 appeared twice |
| Independent PIR energy, `6.18` | No valid op4 samples; two op3 snapshots returned NULL |
| Radar energy, `6.4/6.5` | Valid 32-bin data; this is not infrared energy |

Use `bitmap_data(value)["pir"]` for the actual PIR flag. Missing data is unknown, not zero. No independent PIR advertising object has been observed.

## Compared flows

| Flow | Observation | Result |
| --- | --- | --- |
| Radar command 1, positive control | 12 s | 10 updates each for occupancy and `6.4/6.5/6.8` |
| 0 -> op2 probe -> 4 | 20 s | Power-mode updates only |
| Probe again after command 4 | 20 s | NULL snapshot; power-mode updates only |
| 0 -> 4 without an op2 probe | 20 s | Power-mode updates only |
| 1 -> 4 without stopping first | 20 s | Radar continued; 21 updates per radar field, no PIR energy |
| Detection mode 2 -> 1, then 0 -> 4 | 20 s | Power-mode updates only |

The whole trace, including setup/cleanup, contained 33 radar groups and 42 power-mode updates. All 162 message-start headers used kind 0. No MIC/session errors or obsolete packets were observed. Keepalive reads worked. Synthetic encrypted-peer tests found no ACK/NULL routing bug that swallowed PIR data.

## Official UI and remaining uncertainty

Official plugin v113 uses `6.8` for normal ES5 PIR status. Its shared developer graph subscribes to `6.18` and writes command 4, but the ES5 developer entry is hidden. No extra enable command, sensitivity-zero prerequisite or required mode-1 step was found.

The tested device likely does not expose this debug stream. However, the [official native subscription wire implementation](https://github.com/MiEcosystem/miot-plugin-sdk/blob/master/miot-sdk/device/bluetooth/BluetoothDevice.js) is not public. A subscription-detail difference is not fully excluded; permanent incompatibility across all firmware is **not established**. Historical firmware `2.1.2_0156` was not successfully reread in this session.

## Restoration and library behavior

24 of 27 allowlisted settings had valid original values. Only `detect_mode` was temporarily changed: **2 -> 1 -> 2**. All 24 values matched at final readback; none remained unconfirmed. Stop command 0 was acknowledged and the connection closed. No calibration, reset or firmware update was performed.

`capabilities()` records `pir_energy` as `no_samples/unconfirmed`. `client.diagnostics()["pir_data_received"]` requires a real, model-valid `6.18` op4; ACKs and NULL do not count. The existing supported stream sequence is retained without speculative commands. See the [library API](LIBRARY.md) and [protocol sources](DIRECT_PROTOCOL.md).
