"""Typed provenance must not infer push data from a coincident timestamp."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json

import pytest

from linptech_ble.client import LinptechClient, PropertyValue
from linptech_ble.standard_auth import parse_properties
from test_client import KEY, Radio, device, until
from test_pir_routing import packet
from test_standard_auth import TOKEN


STAMP = datetime(2026, 10, 23, 0, 0, 0, 123000, tzinfo=timezone.utc)


class FixedClock:
    @staticmethod
    def now(tz=None):
        return STAMP


def decoded(operation, value, *, format="int32"):
    result = parse_properties(packet(7, operation, [(6, 18, format, value)]))[0]
    assert "operation" not in result  # The pure parser's public contract stays unchanged.
    return {**result, "operation": operation}


def test_property_value_optional_operation_is_frozen_and_positional_compatible():
    definition = LinptechClient(device()).properties["pir_energy"]
    legacy = PropertyValue(definition, -7, "read", STAMP.isoformat(), "gatt", None)
    assert legacy.operation is None
    assert legacy.to_dict()["operation"] is None
    with pytest.raises(FrozenInstanceError):
        legacy.operation = 4


def test_same_timestamp_snapshot_and_push_keep_distinct_operation_and_observation_identity(monkeypatch):
    monkeypatch.setattr("linptech_ble.client.datetime", FixedClock)
    client = LinptechClient(device())
    snapshot = client._accept(decoded(3, 0))
    pushed = client._accept(decoded(4, 0))
    repeated = client._accept(decoded(4, 0))
    assert snapshot.updated_at == pushed.updated_at == repeated.updated_at == STAMP.isoformat()
    assert snapshot.value == pushed.value == 0
    assert snapshot.operation == 3 and pushed.operation == repeated.operation == 4
    assert snapshot.to_dict()["operation"] == 3 and pushed.to_dict()["operation"] == 4
    assert repeated == pushed and repeated is not pushed
    assert client.diagnostics()["valid_op4_updates"]["pir_energy"]["count"] == 2
    without_provenance = parse_properties(packet(7, 4, [(6, 18, "int32", 0)]))[0]
    unknown = client._accept(without_provenance)
    assert unknown.operation is None and unknown.updated_at == pushed.updated_at
    assert client.diagnostics()["valid_op4_updates"]["pir_energy"]["count"] == 2


def test_null_ack_invalid_and_missing_operation_preserve_status_without_fabricating_data(monkeypatch):
    monkeypatch.setattr("linptech_ble.client.datetime", FixedClock)
    client = LinptechClient(device())
    valid = client._accept(decoded(4, 0))
    for operation, write_ack in ((3, False), (1, True), (None, False)):
        result = {"siid": 6, "piid": 18, "status": 0, "available": False,
                  "value": None, "type_code": 0xF0, "write_ack": write_ack}
        if operation is not None:
            result["operation"] = operation
        observation = client._accept(result)
        assert observation.status == "unavailable" and not observation.valid
        assert observation.value == valid.value == 0  # Historical value, not a NULL converted to zero.
        assert observation.updated_at == valid.updated_at
        assert observation.operation == operation
    invalid = client._accept(decoded(4, 1001))
    assert invalid.status == "invalid_value" and invalid.value == 1001
    assert invalid.operation == 4 and invalid.to_dict()["operation"] == 4
    assert client.diagnostics()["valid_op4_updates"]["pir_energy"]["count"] == 1


def test_public_callbacks_cache_and_new_session_keep_provenance_without_replaying_old_push(monkeypatch):
    monkeypatch.setattr("linptech_ble.client.datetime", FixedClock)
    radio = Radio(monkeypatch)
    received = []

    async def run():
        client = LinptechClient(device(), on_update=received.append)
        assert client.get_property("pir_energy").operation is None
        await client.connect()
        try:
            radio.peer.encrypted(packet(88, 3, [(6, 18, "int32", -8)]))
            await until(lambda: len(received) == 1)
            radio.peer.encrypted(packet(88, 4, [(6, 18, "int32", -8)]))
            await until(lambda: len(received) == 2)
            assert [value.operation for value in received] == [3, 4]
            assert received[0].updated_at == received[1].updated_at
            assert client.get_property("pir_energy") is received[1]
        finally:
            await client.aclose()
        cached = client.get_property("pir_energy")
        assert cached.status == "cached" and not cached.valid
        assert cached.operation == 4 and cached.to_dict()["operation"] == 4
        assert cached.updated_at == STAMP.isoformat()
        await client.connect()
        try:
            assert len(received) == 2  # Connecting does not replay old notifications.
            assert client.get_property("pir_energy").status == "cached"
            assert client.get_property("pir_energy").operation == 4  # Provenance only; never fresh.
            assert not client.diagnostics()["pir_data_received"]
            assert client.diagnostics()["valid_op4_updates"] == {}
            radio.peer.encrypted(packet(90, 3, [(6, 18, "int32", -8)]))
            await until(lambda: len(received) == 3)
            assert received[-1].operation == 3 and received[-1].valid
            assert not client.diagnostics()["pir_data_received"]
            exported = json.dumps(received[-1].to_dict())
            assert KEY not in exported and TOKEN.hex() not in exported
        finally:
            await client.aclose()
        assert radio.closed == 2

    asyncio.run(asyncio.wait_for(run(), 5))
