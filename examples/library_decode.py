"""Decode caller-selected JSONL records; never scan or load a default config."""
import argparse
import json
import os
from pathlib import Path

from linptech_ble import AdvertisementRecord, Decoder, DeviceConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Raw AdvertisementRecord JSONL, selected explicitly")
    parser.add_argument("--model", required=True, choices=("ES5BB", "MS1BB", "KS2BB", "HS3BB"))
    parser.add_argument("--address", required=True, help="Sensor MAC, not a secret")
    args = parser.parse_args()
    key = os.environ.get("LINPTECH_BINDKEY")
    decoder = Decoder([DeviceConfig(args.address, args.model, args.model, bindkey=key)])
    with args.input.open(encoding="utf-8-sig") as source:
        for line in source:
            result = decoder.decode(AdvertisementRecord.from_dict(json.loads(line)))
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
