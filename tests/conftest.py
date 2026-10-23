"""Synthetic MiBeacon packets, generated locally with a public test key.

These fixtures verify parser integration; they do not establish hardware support.
"""

from __future__ import annotations

import struct

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from linptech_ble.models import AdvertisementRecord, MIBEACON_UUID

TEST_KEY = "00112233445566778899aabbccddeeff"
ADDRESS = "AA:BB:CC:DD:EE:01"
PRODUCT_IDS = {"ES5BB": 0x6A0F, "MS1BB": 0x1889, "KS2BB": 0x520B, "HS3BB": 0x50D4}


def object_bytes(object_id: int, value: bytes) -> bytes:
    """Encode a MiBeacon object, independently of the production decoder."""
    return struct.pack("<HB", object_id, len(value)) + value


def synthetic_record(
    model: str,
    objects: bytes,
    *,
    address: str = ADDRESS,
    key: str = TEST_KEY,
    counter: int = 1,
    encrypted: bool = True,
) -> AdvertisementRecord:
    product_id = PRODUCT_IDS[model]
    mac_reversed = bytes.fromhex(address.replace(":", ""))[::-1]
    frame_control = 0x5958 if encrypted else 0x5950
    header = struct.pack("<HHB", frame_control, product_id, counter)
    if encrypted:
        extended_counter = b"\x01\x00\x00"
        nonce = mac_reversed + struct.pack("<HB", product_id, counter) + extended_counter
        ciphertext_with_mic = AESCCM(bytes.fromhex(key), tag_length=4).encrypt(nonce, objects, b"\x11")
        payload = ciphertext_with_mic[:-4] + extended_counter + ciphertext_with_mic[-4:]
    else:
        payload = objects
    return AdvertisementRecord(
        timestamp="2026-10-21T12:00:00+00:00",
        address=address,
        name=f"SYNTHETIC {model}",
        rssi=-55,
        service_data={MIBEACON_UUID: (header + mac_reversed + payload).hex()},
        manufacturer_data={},
        service_uuids=[MIBEACON_UUID],
        tx_power=None,
    )


@pytest.fixture
def make_record():
    return synthetic_record
