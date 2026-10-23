# ESPHome Bluetooth Proxy and Home Assistant

An ESP32 near the sensors can forward BLE advertisements and active GATT over Wi-Fi or Ethernet to the host. Decoding, authentication, ES5BB radar features, and guarded settings remain in Python. No Xiaomi gateway is required.

```text
Sensors --BLE--> ESPHome proxy --Native API--> Python host
                                               |-- SDK / optional dashboard
                                               `-- optional MQTT broker --> Home Assistant
```

This extends reception over IP; the sensors do not become BLE Mesh relays. Local BLE has hardware evidence. Proxy routing, MQTT Discovery, and command handling have software/simulation tests, but **physical ESP32 and Home Assistant end-to-end operation remain unverified**. See the [validation report](PROXY_HA_VALIDATION_2026-10-22.md).

## Prepare a proxy

Use a supported BLE-capable ESP32 with ESPHome Bluetooth Proxy firmware. Starting configurations:

| Example | Hardware |
| --- | --- |
| [Wi-Fi](../examples/esphome-proxy-wifi.yaml) | Original ESP32-WROOM-32 / `esp32dev` |
| [Ethernet](../examples/esphome-proxy-ethernet.yaml) | Olimex ESP32-PoE-ISO / ESP32-PoE-ISO-EA |

Board pin mappings are specific to each example. ESP32-S2 has no BLE. Install through the [official installer](https://esphome.io/projects/?type=bluetooth) or your ESPHome tooling; configure API encryption and network access. Put secrets in a separate `secrets.yaml` as shown by the examples.

Active GATT requires:

```yaml
bluetooth_proxy:
  active: true
  connection_slots: 3
```

Advertising from four sensors does not consume four GATT slots. Each active sensor connection consumes a slot. Keep the proxy near the sensors, and use a distinct ESPHome name for each proxy. [ESPHome proxy documentation](https://esphome.io/components/bluetooth_proxy/)

**Give this standalone program sole ownership of the proxy's advertisement subscription.** Do not simultaneously enable HA's native ESPHome Bluetooth subscription for the same proxy; it can replace this program's subscriber. ESPHome Device Builder may still manage compilation and OTA. This architecture sends data to HA through optional MQTT. A custom HA integration using HA's shared Bluetooth manager would be a different implementation and is not supplied here. [Subscription implementation](https://github.com/esphome/esphome/blob/2026.10.0/esphome/components/bluetooth_proxy/bluetooth_proxy.cpp#L684-L702)

## Configure the host

For direct SDK use, install the `proxy` extra and pass a `BluetoothConfig` containing `ProxyConfig` objects. See the [proxy example](../examples/library_proxy.py) and [library guide](LIBRARY.md). MQTT and the dashboard are unnecessary for core SDK operation.

For the optional dashboard bridge, run these commands in your Python environment from the checkout:

```powershell
python -m pip install -e ".[proxy]"
# Copy only when bridge.local.json does not already exist.
Copy-Item -LiteralPath examples\bridge.example.json -Destination bridge.local.json
```

Edit [the example configuration](../examples/bridge.example.json):

| Setting | Value |
| --- | --- |
| `bluetooth.backend` | `esphome` |
| `bluetooth.proxies[].name` | A unique local proxy name |
| `bluetooth.proxies[].host` | The proxy's reachable IP or hostname |
| `bluetooth.proxies[].port` | Usually `6053` |
| `bluetooth.proxies[].noise_psk` | The same 32-byte base64 key as `api.encryption.key` |
| `bluetooth.gatt_proxy` | One configured proxy name, used for active GATT |
| `mqtt.enabled` | Keep `false` unless using the optional HA bridge |
| `es5.auto_connect` | Start with `false` for manual testing |

The example's `192.0.2.x` addresses and `REPLACE_...` values are placeholders. Add proxies for more rooms: advertisements arrive from all configured proxies, while active GATT uses the selected `gatt_proxy`. The host need not have its own BLE adapter.

Keep sensor credentials in `sensors.local.json`, not ESPHome YAML. Advertising bindkeys, direct authentication tokens, and proxy API keys are distinct; replacing the receiver does not replace sensor credentials. See [credential setup](BINDKEY_WINDOWS.md).

```powershell
# Format/dependency checks only; this does not connect to hardware or MQTT.
python -m linptech_ble doctor --bridge-config bridge.local.json

python -m linptech_ble dashboard --config sensors.local.json --bridge-config bridge.local.json --open
```

The optional dashboard remains local at [127.0.0.1:8765](http://127.0.0.1:8765/). For advertising-only tools, `scan` and `monitor` also accept `--bridge-config`; keep MQTT and ES5 auto-connect disabled. Replay cannot be combined with a bridge configuration.

## Optional Home Assistant MQTT bridge

MQTT is this project's implemented HA integration path, **not a requirement of ESPHome, the sensors, or the Python library**. HA's native ESPHome integration uses the Native API directly; combining its Bluetooth ownership with this standalone bridge is not supported. [HA ESPHome integration](https://www.home-assistant.io/integrations/esphome/)

1. Install the host extras: `python -m pip install -e ".[bridge]"` for proxy plus MQTT, or `".[ha]"` for local BLE plus MQTT.
2. Configure a broker and HA's [MQTT integration](https://www.home-assistant.io/integrations/mqtt/).
3. Set `mqtt.host`, `port`, dedicated `username/password`, and `enabled: true` in `bridge.local.json`. Use the broker's TLS settings when applicable.
4. Match `discovery_prefix` with HA, normally `homeassistant`, and give each running bridge a unique `client_id`. Restart the program.

HA Discovery exposes received sensor states, battery and button events; ES5BB advanced values include PIR trigger, radar arrays, and supported settings. Controls retain authentication, current valid-read checks, range validation, and independent readback. Full radar charts and calibration remain in the dashboard; calibration is not exposed as an unrestricted MQTT write. Unknown or stale data stays unavailable rather than becoming zero or vacant, and historical button events are not replayed as new presses.

## First checks

Confirm live advertisements before attempting GATT. Place the sensor near the selected proxy; another phone or client may occupy its connection. ES5BB and the tested HS3BB workflow used one short press, never a long press/reset. No equivalent wake procedure is established for MS1BB or KS2BB. See [device evidence](DEVICE_CAPABILITIES.md).

| Symptom | Check |
| --- | --- |
| Proxy cannot connect | Hostname/IP, TCP 6053, matching API encryption key |
| Advertising stops unexpectedly | Another client, especially HA's native Bluetooth subscription, owns the proxy |
| Advertising works but GATT fails | `active: true`, free connection slot, sensor connectability, correct sensor token, competing phone connection |
| HA entities are missing | Broker connection, credentials, discovery prefix, HA MQTT integration |
| Radar/PIR appears unchanged | GATT state and each field's timestamp; advertisements do not prove streaming is active |

Independent ES5BB raw PIR energy is still unconfirmed; a working proxy cannot manufacture missing firmware data. Follow [ES5 advanced guidance](ES5BB_ADVANCED.md) for settings and calibration, and verify actual range and streaming stability on your hardware before relying on automation.
