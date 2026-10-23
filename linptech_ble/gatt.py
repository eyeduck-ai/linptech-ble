"""Minimal GATT connection contracts for host-owned Bluetooth integrations.

A factory receives only a MAC address and display name. Its async context
manager resolves, connects, and releases an exclusive connection on the caller's
event loop. It must clean up partial entry failures and honor cancellation.
No scanner, Bluetooth manager, connection, or credentials are created here.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, AsyncContextManager, Protocol, TypeAlias


class GattClient(Protocol):
    """Connected Bleak-compatible operations used by Standard Auth/BLE Spec.

    ``services`` must implement ``get_characteristic(uuid)`` and return objects
    whose ``properties`` describe notification and write capabilities. Notify
    callbacks must run on the client event loop, or be marshalled there by the
    host adapter. No connection or scanner ownership is implied by this type.
    """

    @property
    def services(self) -> Any: ...

    @property
    def is_connected(self) -> bool: ...

    async def start_notify(self, characteristic: str, callback: Callable[[Any, bytearray], Any]) -> Any: ...

    async def stop_notify(self, characteristic: str) -> Any: ...

    async def write_gatt_char(self, characteristic: str, data: bytes | bytearray, *, response: bool) -> Any: ...


GattConnectionFactory: TypeAlias = Callable[[str, str], AsyncContextManager[GattClient]]
