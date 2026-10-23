# Examples

Run commands from the project folder after installing the package. Each library script explicitly selects its input or device; importing it does not start Bluetooth or load a default credential file.

| Script | Purpose | Caller-provided secrets |
| --- | --- | --- |
| `library_decode.py` | Decode a selected raw JSONL file with `--model` and `--address` | `LINPTECH_BINDKEY` for encrypted packets |
| `library_direct.py` | Read an allowed property or observe a bounded ES5 stream | `LINPTECH_AUTH_TOKEN` |
| `library_proxy.py` | Read ES5BB through an explicit `--host` ESPHome proxy | `LINPTECH_AUTH_TOKEN`, `ESPHOME_NOISE_PSK` |

Use your own secret-management mechanism to supply these environment variables. Do not put real keys in source, terminal transcripts or command history. GATT examples require a connectable device and a separate 12-byte token; stream examples send the documented start/stop commands.

Inspect arguments before connecting:

```powershell
.\.venv\Scripts\python.exe examples/library_decode.py --help
.\.venv\Scripts\python.exe examples/library_direct.py --help
.\.venv\Scripts\python.exe examples/library_proxy.py --help
```

`library_direct.py --diagnostics` prints payload-free counters after closing. A PIR command ACK is not proof of energy samples: [PIR energy remains unconfirmed](../docs/PIR_INVESTIGATION.md) on the tested ES5BB. The 10-second default stream example does not implement long-term reconnection; longer ES5 observers should perform the documented keepalive. Physical ESPHome hardware remains unvalidated in this project.

## Synthetic offline demo

In a source checkout, `demo.raw.jsonl` and `demo.secrets.json` contain generated packets and a public test key. They are not physical-device recordings or usable sensor credentials. The demo covers ES5BB occupancy/close-region state, MS1BB opening/closing, and KS2BB temperature/humidity and button events. It does not include HS3BB.

```powershell
.\.venv\Scripts\python.exe -m linptech_ble dashboard --config examples/demo.secrets.json --replay examples/demo.raw.jsonl --port 8766 --open
```

Replay requires no Bluetooth, cloud or gateway and disables device controls. It demonstrates decoder/application wiring, not hardware compatibility. Regenerate the source-checkout fixtures with `python examples/generate_demo.py`; these generated files are not guaranteed to be included in installed distributions.

See the [library guide](../docs/LIBRARY.md), [credential setup](../docs/BINDKEY_WINDOWS.md) and [dashboard guide](../docs/DASHBOARD_WINDOWS.md) for real-device use.

## Network transport

`transport.example.json` configures BLE reception and optional ES5BB automatic connection for CLI/Dashboard use. Copy it to private `transport.local.json`, fill in the proxy address/API key, and pass `--transport-config transport.local.json`. Sensor keys remain in `sensors.local.json`. See [ESPHome setup and migration](../docs/ESPHOME_PROXY.md).
