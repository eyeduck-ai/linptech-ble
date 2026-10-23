"""Explicit adapters for callers that already own a Bluetooth scanner.

No scanner, service browser, manager or configuration file is created here.
Historical callers should supply their recorded wall-clock receive timestamp;
BluetoothServiceInfo.time can be monotonic and is never treated as UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from .models import AdvertisementRecord, canonical_uuid
from .decoded import DecodedAdvertisement

if TYPE_CHECKING:
    from .decoder import Decoder


def record_from_service_info(info: Any, *, timestamp: str | datetime | None = None,
                             receiver: str | None = None) -> AdvertisementRecord:
    """Adapt a BluetoothServiceInfo/BluetoothServiceInfoBleak-like object.

    MAC addresses are required, matching the scanner address used to construct
    the MiBeacon nonce. Platform-specific UUID addresses are not substituted.
    ``receiver`` overrides ``info.source``; absent sources default to ``local``.
    Adapter errors never echo input objects or payloads.
    """
    try:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.isoformat()
        if not isinstance(timestamp, str):
            raise ValueError
        source = receiver if receiver is not None else getattr(info, "source", None)
        data = {
            "timestamp": timestamp, "address": info.address, "name": info.name,
            "rssi": info.rssi,
            "service_data": {canonical_uuid(key): bytes(value).hex() for key, value in info.service_data.items()},
            "manufacturer_data": {str(int(key)): bytes(value).hex() for key, value in info.manufacturer_data.items()},
            "service_uuids": [canonical_uuid(value) for value in info.service_uuids],
            "tx_power": getattr(info, "tx_power", None),
            "receiver": "local" if source is None else source,
        }
        return AdvertisementRecord.from_dict(data)
    except (AttributeError, TypeError, ValueError, OverflowError):
        # datetime/UUID/bytes converters may embed the original input in their
        # exception text. Hide the complete cause chain, not only str(error).
        raise ValueError("Invalid Bluetooth service information; a MAC address and valid advertisement fields are required.") from None


def decode_service_info(info: Any, decoder: Decoder, *, timestamp: str | datetime | None = None,
                        receiver: str | None = None) -> DecodedAdvertisement:
    """Feed a caller-owned Decoder, preserving its per-device event deduplication.

    The decoder must be passed explicitly; this helper never loads credentials
    or creates a global decoder shared with another application's devices.
    """
    from .decoder import Decoder
    if not isinstance(decoder, Decoder):
        raise TypeError("decoder must be a caller-owned linptech_ble.Decoder instance")
    return decoder.decode(record_from_service_info(info, timestamp=timestamp, receiver=receiver))
