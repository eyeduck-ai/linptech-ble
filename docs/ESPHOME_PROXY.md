# ESPHome Bluetooth Proxy

An ESP32 near the sensors forwards BLE advertisements and active GATT over Wi-Fi or Ethernet to the Python host. Decoding, authentication and ES5BB advanced features use the same library API as local BLE. No Xiaomi gateway is required.

```text
Sensors --BLE--> ESPHome proxy --Native API--> Python application
```

Proxy transport has synthetic Native API and encrypted-peer tests. **Physical ESP32 reception, latency and long-term stability remain unverified.** These sensors use MiBeacon/GATT, not BLE Mesh relaying. See [validation](VALIDATION.md).

## Prepare the proxy

Use a BLE-capable ESP32 with ESPHome Bluetooth Proxy firmware:

| Example | Hardware |
| --- | --- |
| [Wi-Fi](../examples/esphome-proxy-wifi.yaml) | Original ESP32-WROOM-32 / `esp32dev` |
| [Ethernet](../examples/esphome-proxy-ethernet.yaml) | Olimex ESP32-PoE-ISO / ESP32-PoE-ISO-EA |

Pin mappings are specific to each board. ESP32-S2 has no BLE. Use the [official installer](https://esphome.io/projects/?type=bluetooth) or ESPHome tooling, and configure network access plus Native API encryption. Keep keys in a private `secrets.yaml`.

For active connections:

```yaml
bluetooth_proxy:
  active: true
  connection_slots: 3
```

Advertisements do not consume GATT slots; each active connection does. Place the proxy near its sensors. See [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy/).

Use one advertisement subscription owner per proxy. Another standalone client or Home Assistant's native ESPHome Bluetooth integration can replace the subscription. Give this application's proxy its own owner; ESPHome tooling can still manage firmware and OTA. [Subscription implementation](https://github.com/esphome/esphome/blob/2026.10.0/esphome/components/bluetooth_proxy/bluetooth_proxy.cpp#L684-L702)

## Configure the host

For a Python application, install `.[proxy]` and explicitly pass `BluetoothConfig`/`ProxyConfig` to `LinptechClient`. See the [runnable example](../examples/library_proxy.py) and [library API](LIBRARY.md#optional-esphome-transport).

For the Dashboard or CLI, run from the checkout:

```powershell
python -m pip install -e ".[proxy]"
# Copy only when transport.local.json does not already exist.
Copy-Item -LiteralPath examples\transport.example.json -Destination transport.local.json
```

Edit [transport.local.json's example](../examples/transport.example.json):

| Setting | Value |
| --- | --- |
| `bluetooth.backend` | `esphome` |
| `bluetooth.proxies[].name` | Unique local name |
| `bluetooth.proxies[].host` | Reachable proxy IP or hostname |
| `bluetooth.proxies[].port` | Usually `6053` |
| `bluetooth.proxies[].noise_psk` | The proxy's 32-byte base64 `api.encryption.key` |
| `bluetooth.gatt_proxy` | Configured proxy name used for GATT |
| `es5.auto_connect` | Start with `false` for manual testing |

The example IP and `REPLACE_...` key are placeholders. Additional proxies receive advertisements concurrently; active GATT uses `gatt_proxy`. The host does not need a local BLE adapter in `esphome` mode. The `local` and `hybrid` backends remain available through the same transport configuration.

Keep sensor bindkeys and Standard Auth tokens in `sensors.local.json`. Proxy API keys are separate credentials and do not replace device keys.

```powershell
# Configuration/dependency check; no hardware connection.
python -m linptech_ble doctor --transport-config transport.local.json
python -m linptech_ble dashboard --transport-config transport.local.json --open
```

`scan` and `monitor` also accept `--transport-config`. Replay cannot be combined with transport configuration. The Dashboard remains local at [127.0.0.1:8765](http://127.0.0.1:8765/).

### Migrate an older checkout

Replace `--bridge-config bridge.local.json` with `--transport-config transport.local.json`. Start from the new example or retain only `version`, `bluetooth` and `es5` in the old file; legacy `mqtt` fields are rejected. Replace the former `ha`/`bridge` installation extras with `proxy` when using ESPHome. Existing sensor credentials stay in `sensors.local.json`.

Applications choose their own Home Assistant, MQTT or other downstream integration and consume this library's decoded observations and client API. The repository's transport configuration covers BLE reception and direct access.

## Connection checks

| Symptom | Check |
| --- | --- |
| Proxy unavailable | IP/hostname, TCP 6053, matching API encryption key |
| Advertisements stop | Competing subscription owner |
| Advertising works but GATT fails | Active proxy mode, free slot, sensor wake, correct token, competing phone connection |
| Radar/PIR remains unchanged | Authenticated session and individual field timestamps |

Confirm advertisements before attempting GATT. Short-press wake was observed for ES5BB and HS3BB; no equivalent procedure is established for MS1BB or KS2BB. Do not hold buttons to reset. Independent ES5BB PIR energy remains unconfirmed with local BLE; changing receivers cannot establish an unreported channel. See [device capabilities](DEVICE_CAPABILITIES.md).
