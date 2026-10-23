"""Public result types; runtime decoding continues to return plain JSON dicts.

These definitions describe values after model/MIC validation. Missing sensor
reports are empty collections, never invented zero/false measurements. Optional
object metadata is diagnostic and may grow without changing the required keys.
"""
from typing import Literal, NotRequired, TypeAlias, TypedDict

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
DeviceModel: TypeAlias = Literal["ES5BB", "MS1BB", "KS2BB", "HS3BB"]
DecodeStatus: TypeAlias = Literal[
    "no_mibeacon", "malformed", "unsupported_model", "unsupported_protocol",
    "model_mismatch", "no_measurement", "encrypted_no_key", "duplicate",
    "decryption_failed", "decoded", "parse_error",
]


class MiBeaconHeader(TypedDict):
    product_id: int
    product_id_hex: str
    model: DeviceModel | None
    version: int
    encrypted: bool
    registered: bool
    has_objects: bool
    mesh: bool
    packet_counter: int


class Measurement(TypedDict):
    key: str
    device_id: str | None
    value: JSONValue
    unit: str | None


class BinarySensorValue(TypedDict):
    key: str
    device_id: str | None
    value: bool | None


class SensorEvent(TypedDict):
    key: str
    device_id: str | None
    event_type: str
    event_properties: JSONValue


class ObjectChoice(TypedDict):
    value: int
    label: str


class MiotObjectMapping(TypedDict):
    siid: int
    piid: NotRequired[int]
    eiid: NotRequired[int]


class BroadcastObjectValue(TypedDict):
    object_id: int
    object_id_hex: str
    length: int
    raw_hex: str
    status: Literal["decoded", "unknown_object", "unauthenticated", "unknown_enum", "outside_spec_range"]
    key: NotRequired[str]
    label: NotRequired[str]
    format: NotRequired[str]
    expected_lengths: NotRequired[list[int]]
    unit: NotRequired[str | None]
    kind: NotRequired[Literal["event", "property"]]
    source: NotRequired[str]
    evidence: NotRequired[str]
    miot: NotRequired[MiotObjectMapping]
    spec_source: NotRequired[str]
    spec_mapping: NotRequired[str]
    mapping_note: NotRequired[str]
    choices: NotRequired[list[ObjectChoice]]
    minimum: NotRequired[int | float]
    maximum: NotRequired[int | float | None]
    value: NotRequired[int | float | None]
    value_label: NotRequired[str]


class DecodedAdvertisement(TypedDict):
    """Stable envelope returned by Decoder.decode and decode_service_info.

    A duplicate retains receiver/RSSI/time and authentication metadata, while
    measurements, binary_sensors and events remain empty. Authentication alone
    does not establish that an optional firmware channel exists or is fresh.
    """

    timestamp: str
    address: str
    name: str | None
    rssi: int
    receiver: str
    model: DeviceModel | None
    status: DecodeStatus
    header: MiBeaconHeader | None
    measurements: list[Measurement]
    binary_sensors: list[BinarySensorValue]
    events: list[SensorEvent]
    unhandled: dict[str, JSONValue]
    objects: list[BroadcastObjectValue]
    bindkey_verified: bool
    reason: NotRequired[str]
