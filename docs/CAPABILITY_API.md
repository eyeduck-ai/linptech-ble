# Capability API

`capabilities()` returns static protocol definitions and evidence for four models. It reads packaged public specifications, not user configuration or credentials, and starts no Bluetooth, network, dashboard, or background thread.

```python
from linptech_ble.capabilities import capabilities, capabilities_schema

inventory = capabilities("KS2BB")  # Omit the argument for all four models.
assert inventory["version"] == 1
assert inventory["details"]["schema_version"] == 1

model = inventory["details"]["models"]["KS2BB"]
print(model["advertisement"]["observed"])
for prop in model["direct"]["properties"]:
    print(prop["name"], prop["sdk_access"], prop["validation"])

schema = capabilities_schema()  # JSON Schema draft 2020-12, as a dict.
```

Names are case-sensitive: `ES5BB`, `MS1BB`, `KS2BB`, `HS3BB`. Unknown names return empty inventories. The existing `version/broadcast/direct` structure remains compatible; additive details have their own schema version, independent of the package or firmware version. Schema validation permits future fields.

## Model details

Each `details.models[model]` contains:

| Field | Meaning |
| --- | --- |
| `identity` | PID, exact MIoT model, released specification, and confirmation scope. HS3BB still requires confirmation for the caller's device. |
| `runtime` | Gateway/cloud requirements, transports and validation level, and the absence of Mesh transport in these profiles. |
| `credentials` | Credential kinds and byte lengths only: 16-byte advertising bindkey, separate 12-byte direct token where settings are exposed, and a separate proxy API key. |
| `wake` | Observed, reported, or unknown wake evidence. `required=null` means a universal requirement has not been established. |
| `advertisement` | Object IDs grouped as observed, protocol-only, or compatibility-only. Full definitions remain in top-level `broadcast`. |
| `field_reporting` | Optional ES5BB field notes: advertising object, separate GATT property/access, related settings, and unknown reporting interval. Missing means not received, never zero/false. |
| `direct` | Property access, evidence, write prerequisites, and links to validation reports. |

The inventory contains no configured MAC, serial number, current reading, or connection state. Evidence describes tested devices, not a live observation of the caller's device.

## Advertising evidence

| Group | Interpretation |
| --- | --- |
| `observed` | Previously captured and authenticated; not a promise of reporting frequency. |
| `protocol_defined_not_observed` | Defined and supported by the parser, but not captured; KS2BB battery is an example. |
| `compatibility_only` | Upstream-compatible variants without evidence of reporting by the tested device. |

Unknown objects and enum values remain diagnostics. An update to battery or illuminance does not refresh occupancy. Radar bins and independent PIR energy are not inferred from occupancy advertisements.

ES5BB `field_reporting` distinguishes an advertising value from an unread GATT property. The released specification declares empty GATT access for `2.1005` light and `5.1019` close-range occupancy, and no GATT access declaration for `7.1003` battery. These fields have authenticated advertising evidence; none has an established reporting interval. `6.75 light_value_lux` is a separate light-judgment value, not a substitute for measured illuminance.

## Direct-property evidence

| Field | Interpretation |
| --- | --- |
| `specification.miot_access` | MIoT permissions; these do not imply GATT access. |
| `specification.gatt_access` | Published GATT permissions. `null` means absent; `[]` means explicitly empty. Notify permission does not prove GET success. |
| `sdk_access.get/set` | Requests the SDK permits attempting; firmware may still decline them. |
| `sdk_access.notify` | A catalog hint for notification/stream handling, not proof that firmware does or does not report autonomously. |
| `sdk_access.notification_only` | GET is blocked; use the appropriate stream and observe updates. |
| `validation.read` | `verified`, `out_of_spec_value`, or `not_verified`. |
| `validation.notify` | `samples_observed`, `no_samples`, or `not_verified`. |
| `validation.write` | `verified_readback_restored`, `not_verified`, or `not_allowed`. |

For ES5BB, 24 settings have valid read evidence, while only individually tested writes receive write verification. Occupancy and power notifications do not establish GET support. PIR energy remains `no_samples/unconfirmed`; `notify_evidence` records successful controls, NULL replies, final setting checks, and the distinction between historical firmware and an unsuccessful current query. See the [PIR investigation](PIR_INVESTIGATION.md).

HS3BB wake is `observed_on_tested_device`: short press, fresh connectable advertising, authentication, and valid reads succeeded. Separate before/after scan windows do not establish universal necessity (`required=null`) or exclusive causation. See [wake evidence](HS3BB_WAKE.md).

Use [LinptechClient](LIBRARY.md) for actual operations. Static metadata never bypasses authentication, current valid-read checks, typed ranges, independent readback, or calibration preparation. See [device capabilities](DEVICE_CAPABILITIES.md) for the concise model comparison.
