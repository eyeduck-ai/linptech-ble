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
| `bluetooth.gatt_proxy` | Default proxy name for GATT |
| `bluetooth.gatt_routes` | Optional sensor MAC-to-proxy map |
| `es5.auto_connect` | Start with `false` for manual testing |

The example IP and `REPLACE_...` key are placeholders. Additional proxies receive advertisements concurrently; active GATT uses the selected proxy. The two supported backends are `local` and `esphome`. The host does not need a local BLE adapter in `esphome` mode.

Keep sensor bindkeys and Standard Auth tokens in `sensors.local.json`. Proxy API keys are separate credentials and do not replace device keys.

```powershell
# Configuration/dependency check; no hardware connection.
python -m linptech_ble doctor --transport-config transport.local.json
python -m linptech_ble dashboard --transport-config transport.local.json --open
```

`scan` and `monitor` also accept `--transport-config`. Replay cannot be combined with transport configuration. The Dashboard remains local at [127.0.0.1:8765](http://127.0.0.1:8765/).

### Multiple rooms

Do not configure one physical ESP under both an IP address and a hostname: duplicate physical identities are rejected before subscribing, and the existing owner keeps receiving.

Configure a distinct proxy entry and API key for each receiver. For example, with proxies named `hall`, `bedroom` and `study`, add these fields inside `bluetooth`:

```json
{
  "gatt_proxy": "hall",
  "gatt_routes": {
    "AA:BB:CC:DD:EE:01": "hall",
    "AA:BB:CC:DD:EE:02": "bedroom",
    "AA:BB:CC:DD:EE:03": "study"
  }
}
```

These synthetic addresses can represent ES5BB, KS2BB and HS3BB respectively. MS1BB remains advertisement-only in the released profile and needs no GATT route. A route uses an already configured proxy name. Selection uses the address-specific route, then `gatt_proxy`, then the sole configured proxy. Ambiguous multi-proxy connections fail; routing does not automatically switch to the strongest RSSI.

All configured receivers can observe the same advertisement. Feed them to one `Decoder` to suppress repeated events, while recording every `receiver`/RSSI observation, including results marked `duplicate`. RSSI compares sensor-to-receiver links; it does not measure Wi-Fi or Ethernet quality. See [signal quality](SIGNAL_QUALITY.md).

Advertisement dispatch has bounded ingress per subscriber before crossing event loops. Burst overload drops oldest advertisements and increments ingress counters; it does not discard GATT notifications. Inspect both ingress and public scanner queue drops through `scanner.health()`. These limits contain memory use rather than promise lossless reception.

Reception can start when one proxy is ready while unavailable proxies retry in the background. A partial outage keeps healthy receivers running and reconnects the failed receiver. If all receivers are unavailable, the scanner reports an error; the application decides when to create a new scanner. Inspect per-proxy health rather than treating one working receiver as proof that every room is covered.

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

## Physical validation checklist

Physical ESPHome hardware is currently unavailable for this project, so the following checks remain **pending**. Software peers and CI do not replace them.

After preparing private sensor and transport configuration, run the [read-only probe](../examples/library_validate_proxy.py):

```powershell
python examples/library_validate_proxy.py --config sensors.local.json --transport-config transport.local.json --seconds 180 --gatt-read
```

`--seconds` sets the initial observation window; optional GATT adds bounded connection/read time while advertisement collection continues. The probe reads one allowlisted property from ES5BB, KS2BB and HS3BB, without streams, calibration or settings writes. Its JSON summary reports per-device/receiver RSSI (up to 600 retained samples), authentication, GATT outcomes and advertisement counts during each read. Missing observations remain unobserved. Keep the output private because it includes sensor addresses.

1. Run `doctor --transport-config transport.local.json` to validate configuration and dependencies. This makes no receiver connection.
2. Receive a bounded `scan`/`monitor` capture with the host's local BLE adapter disabled. Confirm the intended proxy names and fresh RSSI observations.
3. Trigger each sensor once and compare decoded values/events with the physical action. Place two proxies within range of one sensor; expect multiple radio observations and one decoded event.
4. Wake and read ES5BB, KS2BB and HS3BB through their assigned routes. Use the Dashboard or a separate bounded stream example if testing ES5BB radar, then stop it and disconnect. Preserve original settings; begin with reads only.
5. Disconnect one proxy, confirm the remaining receivers continue, then restore it and check recovery. Record firmware, proxy names, time, gaps and sanitized diagnostics.

Repeat each check for every actual proxy/firmware combination. Keep raw captures and credentials private, and record failures as well as successful observations.
