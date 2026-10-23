# linptech-ble

Python library for **ES5BB, MS1BB, KS2BB and HS3BB**: decode MiBeacon advertisements and use authenticated GATT on supported models. Works with local BLE or an optional ESPHome Bluetooth Proxy. **No Xiaomi gateway is required.**

**0.3.0 · Python 3.11+ · MIT · experimental API.** The caller supplies credentials; the library does not log into Xiaomi, discover account keys or load local configuration automatically. Importing the package does not scan, connect or start background threads.

## Install

Clone the repository and install it in a virtual environment:

```powershell
git clone https://github.com/eyeduck-ai/linptech-ble.git
cd linptech-ble
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Optional dependencies: install `.[proxy]` for ESPHome, `.[ha]` for the MQTT bridge, or `.[bridge]` for both. A local wheel can also be installed with pip; this release is not published on PyPI.

## Decode advertisements

Supply your own 16-byte bindkey as a 32-character hex string and an advertisement record received by your application:

```python
from linptech_ble import AdvertisementRecord, Decoder, DeviceConfig

device = DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Presence", bindkey=bindkey)
decoder = Decoder([device])
result = decoder.decode(AdvertisementRecord.from_dict(raw_record))
```

`Decoder` performs no Bluetooth or network I/O. Reuse it to suppress recent duplicate events. Existing HA/Bleak-compatible scanner data can be passed through `decode_service_info(info, decoder, timestamp=received_at)`. Missing values remain unknown; encrypted extensions require valid MIC, structure and values.

## Connect directly

Direct connections require a **separate 12-byte Standard Auth token**, supplied as a 24-character hex string. A broadcast bindkey cannot replace it.

```python
from linptech_ble import DeviceConfig, LinptechClient

async def read_presence(address, auth_token):
    device = DeviceConfig(address, "ES5BB", "Presence", auth_token=auth_token)
    async with LinptechClient(device) as client:
        value = await client.read("occupancy_status")
        return value.value if value.valid else None
```

The async client supports selected reads, bounded updates, ES5BB streams and allowlisted writes with fresh reads and independent readback. It owns no Dashboard. The caller controls reconnection, observation timeouts and keepalive.

| Model | Advertisement data | Direct support |
| --- | --- | --- |
| ES5BB | Occupancy, near-range occupancy, illuminance and battery | 32 radar bins, region/PIR flags, settings and calibration commands |
| MS1BB | Door state, battery and door events | No settings in the released profile |
| KS2BB | Temperature, humidity and button events | Temperature unit and button mode |
| HS3BB | Motion, illuminance and battery | Selected settings; writes require confirmed MIoT model |

See [device capabilities and evidence](docs/DEVICE_CAPABILITIES.md) for field-level limits. ES5BB's independent **PIR energy has no valid samples** from the tested device; its PIR trigger flag works. A command ACK is not proof of sensor data or completed calibration. HS3BB short-press wake followed by authentication/read was observed on one device. Physical ESPHome and full Home Assistant deployment remain unverified.

## API and examples

- [Library guide](docs/LIBRARY.md): decoding, adapters, async client, streams and ESPHome.
- [Capabilities and JSON Schema](docs/CAPABILITY_API.md): static definitions and evidence; `client.diagnostics()` provides payload-free session counters.
- Runnable examples: [decode](examples/library_decode.py), [local direct connection](examples/library_direct.py), [proxy connection](examples/library_proxy.py).

## Optional live test Dashboard

From this checkout, after configuring your own devices:

```powershell
.\start-dashboard.cmd
# Or, in the Python environment where the package is installed:
python -m linptech_ble dashboard --open
```

The Dashboard uses the existing local `sensors.local.json` by default and opens `http://127.0.0.1:8765/`. Live mode receives real hardware data; replay/demo data is only an interface test. Keep credentials out of version control. See [Windows Dashboard](docs/DASHBOARD_WINDOWS.md) for setup.

Home Assistant can use the optional **MQTT Discovery bridge**; this is not a native `custom_components` integration. A proxy dedicated to this bridge must have a single BLE subscription owner: do not simultaneously assign it to HA's native ESPHome Bluetooth integration. See [ESPHome and Home Assistant](docs/ESPHOME_HOME_ASSISTANT.md).

[Documentation index](docs/README.md) · [Contributing](CONTRIBUTING.md) · [LICENSE](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
