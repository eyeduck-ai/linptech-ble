# Python library guide

**linptech-ble 0.4.0 · Python 3.11+ · MIT.** Import as `linptech_ble`. Install from this checkout with `python -m pip install -e .`; optional extras are `proxy`, `ha` and `bridge`. See the [README](../README.md) for Windows environment setup.

The public API works without the Dashboard, CLI, MQTT or configuration files. Imports, `DeviceConfig`, `Decoder` construction and client construction do not open Bluetooth or load credentials. Supply your own keys; no Xiaomi account or gateway is required at runtime.

## Decode caller-supplied advertisements

```python
from linptech_ble import AdvertisementRecord, Decoder, DeviceConfig

device = DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Presence", bindkey=bindkey)
decoder = Decoder([device])
result = decoder.decode(AdvertisementRecord.from_dict(raw_record))
```

`bindkey` is your 16-byte key encoded as 32 hex characters. `raw_record` contains `timestamp`, `address`, `name`, `rssi`, `service_data`, `manufacturer_data` and `service_uuids`; payload values are hex strings. `AdvertisementRecord.from_bleak(device, advertisement)` adapts an existing Bleak callback without starting a scanner.

For HA/Bleak-compatible service information:

```python
from linptech_ble import decode_service_info, record_from_service_info

record = record_from_service_info(info, timestamp=received_at)
result = decoder.decode(record)
# Equivalent shortcut, using the same caller-owned decoder:
result = decode_service_info(info, decoder, timestamp=received_at)
```

Adapters require a MAC address and advertisement fields. Supply a UTC wall-clock timestamp for historical data; an omitted timestamp uses current UTC, not a scanner's monotonic clock. Keep one decoder to retain per-device duplicate suppression. Identical packets repeated within five seconds do not replay button events; this is not persistent exactly-once delivery.

Each result describes one packet, not accumulated device state:

| Field | Meaning |
| --- | --- |
| `status` | Decode outcome, such as `decoded`, `duplicate`, `encrypted_no_key` or `decryption_failed` |
| `bindkey_verified` | Authenticated MIC, or a recent duplicate of an authenticated packet |
| `measurements` | Numeric observations with `key`, `value`, `unit` and `device_id` |
| `binary_sensors` / `events` | Explicit Boolean observations and discrete events |
| `objects` | Object IDs, raw hex, mapping, evidence and parse status |

Missing data stays unknown. Silence is not absence. Unknown objects/enums remain diagnostic; malformed known objects reject the entire packet. Encrypted extensions accept authenticated MiBeacon V4/V5, not Mesh or legacy unauthenticated encryption. Raw decode results may contain addresses and payloads; handle them as application data.

`object_catalog(model=None)` lists supported advertisement definitions. `hex_series()`, `bitmap_data()` and `radar_bins()` transform ES5BB direct values; `bitmap_data(value)["pir"]` is a trigger flag, not raw PIR energy. `EmptyRoomScan` calculates an empty-room threshold preview from supplied samples and never connects or writes.

## Authenticated async client

```python
from linptech_ble import DeviceConfig, LinptechClient

async def inspect_sensor(address, auth_token):
    device = DeviceConfig(address, "ES5BB", "Presence", auth_token=auth_token)
    async with LinptechClient(device) as client:
        value = await client.read("occupancy_status")
        selected = await client.refresh(["detect_mode", "pir_sensitivity"])
        cached = client.get_property("pir_sensitivity")
        return value, selected, cached
```

`auth_token` is a separate 12-byte Standard Auth token encoded as 24 hex characters. It cannot be derived or substituted from the bindkey by this API. Credentials are excluded from configuration repr; the caller chooses storage and lifecycle.

`LinptechClient(device, *, transport=None, on_update=None, auth_timeout=35, operation_timeout=15, queue_size=256)` belongs to one asyncio loop. Entering its async context connects and authenticates; it does not refresh every property, start a stream or change settings. `connect()` / `aclose()` are also available.

| API | Result |
| --- | --- |
| `properties` | Read-only mapping of `PropertyDefinition` metadata |
| `read(name_or_pair, subscribe=None)` | One `PropertyValue`; invalid returned data raises and remains diagnostic |
| `refresh(names=None, timeout=None)` | Mapping of observations, preserving per-field unavailable/error status |
| `get_property(name_or_pair)` | Cached observation; no I/O |
| `set(name_or_pair, value)` | Validated write followed by independent agreeing readback |
| `updates(names=None, replay=True)` | Async iterator of bounded property updates |

Prefer explicit names in `refresh()`; a whole catalog can include firmware-unsupported fields. A key is a property name or `(siid, piid)` tuple. `ClientError.code` identifies client failures; transport/protocol errors and timeouts can also propagate.

`PropertyValue` exposes `definition`, `value`, `status`, `updated_at`, `source`, `error`, `operation`, plus `name`, `pair`, `valid` and `to_dict()`. Only `status == "read"` is valid. `operation` preserves the parsed BLE Spec operation (3 for a snapshot, 4 for an update, or `None` when absent), even when timestamps coincide. `invalid_value` preserves an undefined enum/range; `cached` means the connection ended. Error/cached states can retain an older value, timestamp and operation, so a non-null value or operation 4 alone is not proof of current data.

Writes require the model allowlist, correct type/range and a fresh valid read in the current authenticated session. ES5BB `pir_threshold` also requires explicit custom `pir_sensitivity=0`. HS3BB writes require the caller to confirm `DeviceConfig(..., miot_model="linp.motion.hs1bb2")`. MS1BB's released profile has no supported GATT settings. The API does not expose arbitrary commands or firmware updates.

For example, explicitly changing and restoring ES5BB PIR sensitivity:

```python
async def try_sensitivity(device):
    async with LinptechClient(device) as client:
        original = await client.read("pir_sensitivity")
        try:
            await client.set("pir_sensitivity", 2)
        finally:
            if client.connected:
                await client.set("pir_sensitivity", original.value)
```

A disconnect can prevent restoration; the application must report an unresolved change rather than assume success.

## ES5BB streams and lifecycle

```python
import asyncio

async def observe_radar(device):
    async with LinptechClient(device) as client:
        async with client.stream("radar"):
            try:
                async with asyncio.timeout(10):
                    async for update in client.updates(["motion_energy", "presence_energy"]):
                        print(update.name, update.value, update.updated_at)
            except TimeoutError:
                pass
    return client.diagnostics()
```

`set_stream("radar" | "pir" | "off")` switches the ES5BB mode; `stream()` restores the prior mode on exit. `stream_mode` records the last acknowledged request, not data freshness. Independent `pir_energy` is notification-only: use `stream("pir")` and `updates(["pir_energy"])`, not `read("pir_energy")`. Bound the observation time even if no samples arrive.

Updates and synchronous/asynchronous `on_update` callbacks use bounded queues, dropping the oldest entries when full. Keep synchronous callbacks nonblocking. Inspect `dropped_updates` / `callback_errors`; this API is not a lossless recorder. Cancelling an iterator removes that observer. Cancelling or timing out a wire operation closes the connection; async context exit releases it.

There is no automatic polling or reconnection. For long ES5BB observations, schedule `await client.keepalive()` about every 120 seconds from the caller. `radar_calibrate(empty_room=True)` requires an active radar stream and sends the fixed calibration command: ACK is not proof of completion or calibration quality. Empty-room confirmation is required.

## Commit custom ES5BB radar thresholds

`await client.commit_radar_thresholds(empty_room=True)` sends only the fixed threshold-commit command `512`. It requires ES5BB, an authenticated connection, the radar stream and explicit empty-room confirmation. A stream context owned by another task cannot be borrowed. Before sending the command, the client freshly reads and validates `motion_threshold`, `presence_threshold`, `radar_motion_sens` and `radar_presence_sens`. Both threshold arrays must contain 32 hexadecimal bytes; both sensitivity modes must be custom (`0`). Cached values cannot satisfy these checks.

This method does not write properties or create a backup. The calling application must save the original four settings before using verified `set()` calls, commit only the intended preview, then independently read back the expected settings. It must restore and verify the originals on failure and report any unresolved restoration. Existing `set()` guards still reject undefined types, ranges, enum values and overwriting unknown reserved bits. A timeout or cancellation closes the connection and can prevent restoration.

The method returns `True` for a command ACK; a missing ACK raises `ClientError` with `code="calibration_unconfirmed"`. An ACK does not establish physical calibration completion or quality. No arbitrary command API is exposed.

## Static capabilities and live-session diagnostics

```python
from linptech_ble import capabilities, capabilities_schema

inventory = capabilities("ES5BB")
schema = capabilities_schema()
# After an explicitly created client session:
diagnostic = client.diagnostics()
```

`capabilities(model=None)` is a static inventory with advertisement definitions, direct properties and evidence. Existing top-level `version=1`, `broadcast` and `direct` remain; `details.schema_version=1` adds per-model official access, SDK operations and validation. `capabilities_schema()` returns JSON Schema draft 2020-12. Neither function reads credentials, scans or contacts a service. See [capability API](CAPABILITY_API.md).

`diagnostics()` performs no I/O and returns a detached snapshot without payloads, sensor values, addresses or credentials. `session_state` is `not_started`, `current` or `last_closed`. Session counters reset on a new connection; callback/drop/unknown counters are lifetime totals.

A **write ACK**, a **NULL snapshot** and a **valid op4 property notification** are separate outcomes. Only valid known op4 values increment `valid_op4_updates`; only valid `pir_energy` op4 makes `pir_data_received` true. These counters prove receipt, not freshness. `stream_mode_is_acknowledgement_only` makes the distinction explicit. Fragment counts are not sample counts.

## Optional ESPHome transport and Home Assistant

```python
from linptech_ble import BluetoothConfig, ProxyConfig

transport = BluetoothConfig(
    backend="esphome",
    proxies=(ProxyConfig("room", "192.0.2.10", noise_psk=api_noise_key),),
    gatt_proxy="room",
)
async def read_via_proxy(device):
    async with LinptechClient(device, transport=transport) as client:
        return await client.read("occupancy_status")
```

Replace the documentation IP and supply your own ESPHome API encryption key. Install `.[proxy]`; the proxy needs `active: true` and an available GATT slot. Multiple proxies require `gatt_proxy`. Advertisement and GATT traffic share one Native API connection per proxy. This standalone transport must own the BLE subscription; do not simultaneously assign the same proxy to HA's native Bluetooth integration.

Home Assistant support is an optional MQTT Discovery bridge (`.[ha]`, or `.[bridge]` with proxies), not a native custom integration. It is not required by the public client. See [ESPHome/Home Assistant setup](ESPHOME_HOME_ASSISTANT.md).

## Tested scope and optional tools

Four models' advertisements were authenticated on real devices. Local Windows direct tests covered ES5BB authentication/reads, both KS2BB settings and HS3BB indicator/detection-interval writes, independent readback and restoration. HS3BB short press followed by immediate connection/read worked on one device; confirm your own model and connectable window. Its A/B timeouts returned out-of-spec zero and were not written.

ES5BB radar and PIR trigger flags work; independent PIR energy still has no valid samples. Static specification and command ACK do not establish that channel's availability. Native SDK wire details remain uncertain; permanent firmware non-support is not proven. Physical ESPHome and a full HA deployment remain unverified. See [device comparison](DEVICE_CAPABILITIES.md), [PIR investigation](PIR_INVESTIGATION.md) and [HS3BB wake](HS3BB_WAKE.md).

Runnable examples: [decode](../examples/library_decode.py), [direct/diagnostics](../examples/library_direct.py), [proxy](../examples/library_proxy.py). They read credentials from caller-provided environment variables when explicitly executed.

For live hardware testing, use `start-dashboard.cmd` or `python -m linptech_ble dashboard --open` with your existing local `sensors.local.json`. Replay/demo mode is sample data, not a live connection. See [Windows Dashboard](DASHBOARD_WINDOWS.md). Controllers, workers and HTTP state are internal tools, not public API.
