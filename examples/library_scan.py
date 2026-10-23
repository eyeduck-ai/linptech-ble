"""Decode live local/proxy advertisements with the public, file-free stream API."""
import argparse
import asyncio
from contextlib import suppress
import json

from linptech_ble import AdvertisementScanner, Decoder
from linptech_ble.config import load_config
from linptech_ble.transport_config import load_transport_config


async def run(args):
    devices = load_config(args.config)
    if not devices:
        raise ValueError("The explicitly selected sensor configuration is empty.")
    settings = load_transport_config(args.transport_config)
    decoder = Decoder(devices)
    async with AdvertisementScanner(transport=settings.bluetooth,
            addresses=[device.address for device in devices]) as scanner:
        with suppress(TimeoutError):
            async with asyncio.timeout(args.seconds):
                async for record in scanner:
                    result = decoder.decode(record)
                    print(json.dumps({key: result[key] for key in ("timestamp", "address", "model", "receiver", "rssi",
                        "status", "measurements", "binary_sensors", "events")}, ensure_ascii=False), flush=True)
        print(json.dumps({"scanner": scanner.health()}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Explicit private sensor configuration; never echoed")
    parser.add_argument("--transport-config", help="Explicit local/ESPHome transport JSON; omitted means local BLE")
    parser.add_argument("--seconds", type=float, default=60, help="Bounded observation duration (1-3600 seconds)")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Network/library exceptions can contain private endpoint information.
        print(json.dumps({"error": "Observation failed", "type": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
