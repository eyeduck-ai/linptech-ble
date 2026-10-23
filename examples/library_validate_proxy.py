"""Bounded, read-only hardware probe for a caller-selected proxy deployment.

No device settings, calibration or streams are started. The optional GATT phase
authenticates and reads one allowlisted property per supported configured model.
Silence is reported as unobserved, never as a passing sensor validation.
"""
import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import suppress
import json
import statistics

from linptech_ble import AdvertisementScanner, Decoder, LinptechClient
from linptech_ble.config import load_config
from linptech_ble.transport_config import load_transport_config

READS = {"ES5BB": "occupancy_status", "KS2BB": "temperature_unit", "HS3BB": "detection_interval"}


async def run(args):
    devices = load_config(args.config)
    if not devices:
        raise ValueError("The explicitly selected sensor configuration is empty.")
    settings = load_transport_config(args.transport_config)
    if settings.bluetooth.backend != "esphome":
        raise ValueError("This probe requires an explicit ESPHome transport configuration.")
    decoder, counts = Decoder(devices), Counter()
    samples, verified = defaultdict(list), set()
    gatt = []
    async with AdvertisementScanner(transport=settings.bluetooth,
            addresses=[device.address for device in devices]) as scanner:
        async def collect():
            async for record in scanner:
                result = decoder.decode(record)
                counts[result["status"]] += 1
                # Ignore sentinel/invalid RSSI without hiding the observation.
                if -126 <= record.rssi <= -1:
                    points = samples[(record.address, record.receiver)]
                    points.append(record.rssi)
                    del points[:-600]
                if result["bindkey_verified"]:
                    verified.add(record.address)
        collector = asyncio.create_task(collect())
        try:
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(collector), args.seconds)
            # Continue consuming advertisements while GATT shares the API owner.
            if args.gatt_read:
                for device in devices:
                    row = {"address": device.address, "model": device.model}
                    before = sum(counts.values())
                    if device.model not in READS:
                        row["status"] = "advertisement_only"
                    elif not device.auth_token:
                        row["status"] = "missing_token"
                    else:
                        try:
                            async with asyncio.timeout(75):
                                async with LinptechClient(device, transport=settings.bluetooth) as client:
                                    value = await client.read(READS[device.model])
                                    row.update(status="read" if value.valid else value.status, property=value.name)
                        except Exception as error:
                            row.update(status="failed", error_type=type(error).__name__)
                    row["advertisements_during_gatt"] = sum(counts.values()) - before
                    gatt.append(row)
            # Surface an observer failure even when it occurs during GATT.
            if collector.done():
                await collector
            receiver_health = scanner.health()
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
    report = {"mode": "physical_read_only_probe", "requested_seconds": args.seconds,
        "decoded_status_counts": dict(counts), "scanner": receiver_health,
        "all_devices_observed": len(verified) == len(devices), "gatt_read_requested": args.gatt_read,
        "devices": [{"address": device.address, "model": device.model,
            "authenticated_advertisement_observed": device.address in verified,
            "receivers": [{"receiver": receiver, "samples_retained": len(points),
                "rssi_min": min(points), "rssi_median": statistics.median(points), "rssi_max": max(points)}
                for (address, receiver), points in sorted(samples.items()) if address == device.address]}
            for device in devices], "gatt": gatt,
        "limits": "Observation only; no writes, calibration, radio accuracy or long-term stability certification."}
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Explicit private sensor JSON; never echoed")
    parser.add_argument("--transport-config", required=True, help="Explicit private ESPHome transport JSON")
    parser.add_argument("--seconds", type=float, default=180, help="Advertisement observation, 1-3600 seconds")
    parser.add_argument("--gatt-read", action="store_true", help="Also authenticate/read supported devices; requires wake and tokens")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(json.dumps({"error": "Proxy validation could not complete", "type": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
