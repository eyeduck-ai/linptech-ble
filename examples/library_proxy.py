"""Read ES5BB through a caller-selected ESPHome Bluetooth Proxy."""
import argparse
import asyncio
import os

from linptech_ble import BluetoothConfig, DeviceConfig, LinptechClient, ProxyConfig


async def run(args):
    transport = BluetoothConfig(backend="esphome", proxies=(
        ProxyConfig(name="room", host=args.host, noise_psk=os.environ["ESPHOME_NOISE_PSK"]),
    ), gatt_proxy="room")
    device = DeviceConfig(args.address, "ES5BB", "ES5BB", auth_token=os.environ["LINPTECH_AUTH_TOKEN"])
    async with LinptechClient(device, transport=transport) as client:
        print(await client.read("occupancy_status"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="ESPHome Native API host on your own LAN")
    parser.add_argument("--address", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
