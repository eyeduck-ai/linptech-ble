"""Regenerate the wholly synthetic demo; no sensor, account, or network is used.

Run from the repository root: .\.venv\Scripts\python.exe examples/generate_demo.py
The public test key below is not a key for any physical device.
"""

from __future__ import annotations

import json
from pathlib import Path
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESCCM


KEY = "00112233445566778899aabbccddeeff"
UUID = "0000fe95-0000-1000-8000-00805f9b34fb"


def obj(identifier: int, data: bytes) -> bytes:
    return struct.pack("<HB", identifier, len(data)) + data


def main() -> None:
    folder = Path(__file__).resolve().parent
    profiles = [
        ("ES5BB", 0x6A0F, "AA:BB:CC:DD:EE:01"),
        ("MS1BB", 0x1889, "AA:BB:CC:DD:EE:02"),
        ("KS2BB", 0x520B, "AA:BB:CC:DD:EE:03"),
    ]
    payloads = [
        (0, obj(0x484E, b"\x01") + obj(0x5413, b"\x01")),
        (0, obj(0x484E, b"\x00") + obj(0x5413, b"\x00")),
        (1, obj(0x4A12, b"\x01")),
        (1, obj(0x4A12, b"\x02")),
        (2, obj(0x4C01, struct.pack("<f", 23.75)) + obj(0x4C08, struct.pack("<f", 57.5))),
        (2, obj(0x560C, b"\x01")),
        (2, obj(0x560D, b"\x01")),
        (2, obj(0x560E, b"\x01")),
    ]
    lines = []
    for counter, (profile, plaintext) in enumerate(payloads, start=1):
        model, product_id, address = profiles[profile]
        mac = bytes.fromhex(address.replace(":", ""))[::-1]
        identity = struct.pack("<HB", product_id, counter)
        extension = b"\x01\x00\x00"
        encrypted = AESCCM(bytes.fromhex(KEY), tag_length=4).encrypt(mac + identity + extension, plaintext, b"\x11")
        packet = struct.pack("<H", 0x5958) + identity + mac + encrypted[:-4] + extension + encrypted[-4:]
        lines.append(json.dumps({
            "timestamp": f"2026-10-21T12:00:{counter:02d}+00:00",
            "address": address,
            "name": f"SYNTHETIC {model}; not hardware capture",
            "rssi": -55,
            "service_data": {UUID: packet.hex()},
            "manufacturer_data": {},
            "service_uuids": [UUID],
            "tx_power": None,
        }))
    (folder / "demo.raw.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    devices = [{"address": address, "model": model, "name": f"SYNTHETIC {model}", "bindkey": KEY} for model, _, address in profiles]
    (folder / "demo.secrets.json").write_text(json.dumps({"version": 1, "devices": devices}, indent=2) + "\n", encoding="utf-8")
    print("Generated 8 synthetic broadcasts and a public-test-key config in examples/.")


if __name__ == "__main__":
    main()
