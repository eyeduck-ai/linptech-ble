"""Explicit async BLE client: read a property or observe a bounded stream."""
import argparse
import asyncio
import json
import os

from linptech_ble import DeviceConfig, LinptechClient


async def run(args):
    device = DeviceConfig(args.address, args.model, args.model,
                          auth_token=os.environ["LINPTECH_AUTH_TOKEN"])
    async with LinptechClient(device) as client:
        if args.stream:
            # Stream contexts restore the previous stream when they exit.
            async with client.stream(args.stream):
                try:
                    async with asyncio.timeout(args.seconds):
                        async for value in client.updates():
                            print(value)
                except TimeoutError:
                    pass
        else:
            print(await client.read(args.read))
    if args.diagnostics:
        # The closed-session snapshot contains counts, never payloads or keys.
        print(json.dumps(client.diagnostics(), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="ES5BB", choices=("ES5BB", "MS1BB", "KS2BB", "HS3BB"))
    parser.add_argument("--address", required=True)
    parser.add_argument("--read", default="occupancy_status")
    parser.add_argument("--stream", choices=("radar", "pir"), help="ES5BB only; starts device notifications")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--diagnostics", action="store_true", help="Print payload-free counters after close")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
