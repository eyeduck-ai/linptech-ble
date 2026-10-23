# Third-party notices

The original project code is MIT licensed; see [LICENSE](LICENSE). The project
does not claim ownership of third-party dependencies or published device
specifications. No vendor Mi Home JavaScript, token extractor executable,
private firmware, AGPL integration source, captures, keys, or account exports
are included in the distribution.

## Dependencies and protocol sources

The distributions below retain their own license notices; installing this
project does not relicense them. They are dependencies, not vendored copies.

| Component | Verified version | License | Use |
| --- | --- | --- | --- |
| [Bleak](https://github.com/hbldh/bleak) | 3.0.2 | MIT | Local Bluetooth GATT/scanning |
| [xiaomi-ble](https://github.com/Bluetooth-Devices/xiaomi-ble) | 1.17.0 | Apache-2.0 | Existing MiBeacon conversion helpers |
| [cryptography](https://github.com/pyca/cryptography) | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Standard Auth and AES-CCM |
| [bleak-esphome](https://github.com/Bluetooth-Devices/bleak-esphome) | 4.1.0 | MIT | Optional ESPHome transport |
| [aioesphomeapi](https://github.com/esphome/aioesphomeapi) | 46.21.0 | MIT | Optional encrypted ESPHome API |
| [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) | 2.1.0 | EPL-2.0 OR BSD-3-Clause | Optional MQTT bridge |

Xiaomi MIoT instance JSON files in `linptech_ble/specs/` are public protocol
reference data from [miot-spec.org](https://miot-spec.org/). Their vendor
descriptions and names retain their original rights and are not relicensed by
the project's MIT notice. They contain device specifications, not private
device/account data. Model and protocol mappings are documented in
[DEVICE_CAPABILITIES.md](docs/DEVICE_CAPABILITIES.md).

## Standard Auth research attribution

The independent Xiaomi Standard Auth / BLE Spec implementation was informed by the protocol research in [opravdin/hass-yunmi-kettle-ble](https://github.com/opravdin/hass-yunmi-kettle-ble). Its MIT notice is retained below. Vendor Mi Home JavaScript is not included. Python dependencies retain their own licenses.

MIT License

Copyright (c) 2026 Pravdin Oleg

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
