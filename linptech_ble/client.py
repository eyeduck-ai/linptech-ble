"""Public asynchronous, account-free GATT API for supported Linptech devices.

This module owns no dashboard, HTTP server or controller thread. It reuses the
model allowlists, Standard Auth engine and local/ESPHome transport. Construction
and catalog inspection perform no I/O; ``connect`` authenticates without
starting a stream, reading the entire catalog or changing device settings.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import inspect
import math
from types import MappingProxyType
from typing import Any

from .config import DeviceConfig, MIOT_MODELS
from .standard_auth import DirectError, PropertyUnavailable, StandardSession, encode_value
from .transport import BluetoothConfig, TransportError, connected_client

PropertyKey = str | tuple[int, int]
_CLOSED = object()
_COUNT_LIMIT = (1 << 63) - 1


class ClientError(DirectError):
    """Public high-level failure, with a stable machine-readable ``code``."""

    def __init__(self, message: str, *, code: str = "operation_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PropertyDefinition:
    """Credential-free metadata. Firmware may still decline a documented field."""

    name: str
    label: str
    siid: int
    piid: int
    format: str
    writable: bool
    gatt_read: bool = True
    gatt_notify: bool = False
    notification_only: bool = False
    minimum: int | None = None
    maximum: int | None = None
    step: int = 1
    choices: tuple[str, ...] = ()
    allowed_values: tuple[int, ...] = ()
    allowed_mask: int | None = None
    unit: str | None = None
    optional: bool = False

    @property
    def pair(self) -> tuple[int, int]:
        return self.siid, self.piid


@dataclass(frozen=True)
class PropertyValue:
    """A typed observation; only ``status == 'read'`` denotes a valid value.

    Unknown enumerations/ranges remain inspectable as ``invalid_value``.
    Unavailable/error observations may retain the previous value and timestamp;
    they never manufacture a zero. ``cached`` means the connection has ended.
    """

    definition: PropertyDefinition
    value: Any = None
    status: str = "unread"
    updated_at: str | None = None
    source: str = "cache"
    error: str | None = None

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def pair(self) -> tuple[int, int]:
        return self.definition.pair

    @property
    def valid(self) -> bool:
        return self.status == "read"

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible metadata/value fields, never credentials."""
        return asdict(self)


def _catalog(model: str) -> tuple[dict[str, PropertyDefinition], dict[str, Any]]:
    # Import definitions only. No controller instance, worker or manager is
    # created, and the wire/security implementation is not duplicated here.
    if model == "ES5BB":
        from .property_catalog import CATALOG
        source = dict(CATALOG)
    else:
        from .property_catalog import PROFILES
        source = {p.name: p for p in PROFILES[model]}
    result = {}
    for name, prop in source.items():
        notify_only = getattr(prop, "notification_only", False)
        stream_field = name in {"motion_energy", "presence_energy", "human_sensor_bitmaps", "pir_energy"}
        result[name] = PropertyDefinition(name, prop.label, prop.siid, prop.piid, prop.format,
            prop.writable, gatt_read=not notify_only,
            gatt_notify=notify_only or stream_field or getattr(prop, "notify", False),
            notification_only=notify_only, minimum=prop.minimum, maximum=prop.maximum,
            step=getattr(prop, "step", 1), choices=prop.choices,
            allowed_values=getattr(prop, "allowed_values", ()), allowed_mask=getattr(prop, "allowed_mask", None),
            unit=getattr(prop, "unit", None), optional=getattr(prop, "optional", False))
    return result, source


def _timeout(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number.")
    return float(value)


class LinptechClient:
    """Async GATT connection with model-scoped reads and verified writes.

    ``DeviceConfig.auth_token`` must contain the separate 12-byte Standard Auth
    token; a MiBeacon bindkey cannot replace it. HS3BB writes additionally need
    the explicitly confirmed ``miot_model='linp.motion.hs1bb2'``. MS1BB's released
    profile exposes no GATT settings, so it is intentionally advertisement-only.

    All public async methods belong to the loop that called ``connect``. Slow
    callbacks cannot block the protocol reader while awaiting: callbacks run on
    one separate task, in order. Synchronous callbacks must themselves be quick
    and nonblocking. Exceptions are counted and sanitized in ``callback_error``.
    Observation queues drop the oldest update when full; ``dropped_updates``
    records this, so this state API is not a lossless packet recorder.

    Cancelling/timing out a wire operation closes the connection to avoid
    continuing a potentially partial transaction. Cancelling an ``updates``
    iterator only removes that observer. Context exit always releases GATT.
    """

    def __init__(self, device: DeviceConfig, *, transport: BluetoothConfig | None = None,
                 on_update: Callable[[PropertyValue], Any] | None = None,
                 auth_timeout: float = 35, operation_timeout: float = 15,
                 queue_size: int = 256) -> None:
        if not isinstance(device, DeviceConfig):
            raise TypeError("device must be a DeviceConfig.")
        if on_update is not None and not callable(on_update):
            raise TypeError("on_update must be callable or None.")
        if type(queue_size) is not int or not 1 <= queue_size <= 65536:
            raise ValueError("queue_size must be an integer between 1 and 65536.")
        if transport is not None and not isinstance(transport, BluetoothConfig):
            raise TypeError("transport must be a BluetoothConfig or None.")
        self._device = replace(device)
        self.transport = transport or BluetoothConfig()
        self.auth_timeout = _timeout(auth_timeout, "auth_timeout")
        self.operation_timeout = _timeout(operation_timeout, "operation_timeout")
        self.queue_size, self.on_update = queue_size, on_update
        definitions, self._source = _catalog(device.model)
        self.properties: Mapping[str, PropertyDefinition] = MappingProxyType(definitions)
        self._pairs = {d.pair: d for d in definitions.values()}
        self._values = {name: PropertyValue(d) for name, d in definitions.items()}
        self._session: StandardSession | None = None
        self._ble: Any = None
        self._context: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._operation_lock: asyncio.Lock | None = None
        self._connect_lock: asyncio.Lock | None = None
        self._callback_queue: asyncio.Queue | None = None
        self._callback_task: asyncio.Task | None = None
        self._observers: dict[object, tuple[asyncio.Queue, frozenset[str]]] = {}
        self._close_task: asyncio.Task | None = None
        self._close_caller: asyncio.Task | None = None
        self._close_waiters: set[asyncio.Task] = set()
        self._close_waiter_event: asyncio.Event | None = None
        self._opening_task: asyncio.Task | None = None
        self._closing = False
        self._stream_mode = "off"
        self._stream_owner: asyncio.Task | None = None
        self._stream_owner_session: StandardSession | None = None
        self._stream_depth = 0
        self.dropped_updates = 0
        self.callback_errors = 0
        self.callback_error: str | None = None
        self.unknown_updates = 0
        self._valid_op4_updates: dict[str, dict[str, Any]] = {}
        self._last_session_diagnostics: dict[str, Any] | None = None
        self._stream_mode_before_close: str | None = None

    @property
    def model(self) -> str:
        return self._device.model

    @property
    def protocol_model(self) -> str:
        return MIOT_MODELS[self.model]

    @property
    def supported(self) -> bool:
        return bool(self.properties)

    @property
    def connected(self) -> bool:
        return bool(not self._closing and self._session and self._session.cipher is not None
                    and self._session.error is None and self._ble and self._ble.is_connected)

    @property
    def stream_mode(self) -> str:
        """Last acknowledged mode, not a claim that fresh data has arrived."""
        return self._stream_mode

    def diagnostics(self) -> dict[str, Any]:
        """Return a detached, payload-free diagnostic snapshot without I/O.

        ``stream_mode`` records a command acknowledgement, never proof of data.
        ``pir_data_received`` requires a model-valid op4 PIR-energy value in the
        current/last session; it says nothing about how recently data arrived.
        ``valid_op4_updates`` contains only counts/timestamps for known sensor
        fields, excluding NULL, command-status replies, ACKs and invalid values.
        Session counters reset on a new connection; the last closed session
        remains inspectable until another session starts. Callback/drop/unknown
        counts are lifetime totals. No values, addresses or credentials appear.
        """
        session = self._session.diagnostics() if self._session else self._last_session_diagnostics
        return deepcopy({"model": self.model, "connected": self.connected,
            "session_state": "current" if self._session else "last_closed" if session is not None else "not_started",
            "session": session, "stream_mode": self._stream_mode,
            "stream_mode_is_acknowledgement_only": True,
            "stream_mode_before_close": self._stream_mode_before_close,
            "pir_data_received": bool(self._valid_op4_updates.get("pir_energy", {}).get("count", 0)),
            "valid_op4_updates": self._valid_op4_updates,
            "counts": {"callback_errors": self.callback_errors, "dropped_updates": self.dropped_updates,
                       "unknown_updates": self.unknown_updates}})

    def _definition(self, key: PropertyKey) -> PropertyDefinition:
        if isinstance(key, str):
            definition = self.properties.get(key)
        elif isinstance(key, tuple) and len(key) == 2 and all(type(v) is int for v in key):
            definition = self._pairs.get(key)
        else:
            definition = None
        if definition is None:
            raise ClientError("Property is not in this model's allowlist.", code="unknown_property")
        return definition

    def get_property(self, key: PropertyKey) -> PropertyValue:
        """Inspect the last observation synchronously; this never performs I/O."""
        value = self._values[self._definition(key).name]
        if value.status == "read" and not self.connected:
            return replace(value, status="cached", source="cache")
        return value

    def _check_loop(self, *, connected: bool = True) -> None:
        if self._loop is not asyncio.get_running_loop():
            raise ClientError("Use this client on the event loop that connected it.", code="wrong_loop")
        if connected and not self.connected:
            raise ClientError("Connect and authenticate before this operation.", code="not_connected")

    async def __aenter__(self) -> LinptechClient:
        return await self.connect()

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def connect(self) -> LinptechClient:
        if not self.supported:
            raise ClientError("This released model has no supported GATT settings; use advertisement decoding.", code="advertisement_only")
        if not self._device.auth_token:
            raise ClientError("A separate 12-byte Standard Auth token is required.", code="missing_auth_token")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise ClientError("Create a client for each event loop.", code="wrong_loop")
        if self._loop is None:
            self._loop, self._operation_lock, self._connect_lock = loop, asyncio.Lock(), asyncio.Lock()
            self._close_waiter_event = asyncio.Event()
        assert self._connect_lock is not None
        async with self._connect_lock:
            if self.connected:
                return self
            if self._context is not None or self._session is not None:
                await self.aclose()
            if self._close_task:
                await asyncio.shield(self._close_task)
                self._close_task = None
            self._closing = False
            self._opening_task = asyncio.current_task()
            try:
                self._context = connected_client(self.transport, self._device.address, self._device.name)
                self._ble = await self._context.__aenter__()
                self._valid_op4_updates = {}
                self._last_session_diagnostics = None
                self._stream_mode_before_close = None
                self._session = StandardSession(self._ble, bytes.fromhex(self._device.auth_token), on_property=self._accept)
                await asyncio.wait_for(self._session.authenticate(), self.auth_timeout)
                if self.on_update:
                    self._callback_queue = asyncio.Queue(self.queue_size)
                    self._callback_task = asyncio.create_task(self._callbacks(self._callback_queue), name="linptech-callback")
            except BaseException as error:
                # An external close cancels this opening task and then cleans
                # it up. Waiting for that same close here would form a cycle.
                if not self._closing:
                    await self.aclose()
                if isinstance(error, (asyncio.CancelledError, DirectError, TransportError)):
                    raise
                raise ClientError(f"Connection/authentication failed ({type(error).__name__}).", code="connection_failed") from None
            finally:
                self._opening_task = None
        return self

    def _put(self, queue: asyncio.Queue, value: Any) -> None:
        if queue.full():
            queue.get_nowait()
            self.dropped_updates = min(_COUNT_LIMIT, self.dropped_updates + 1)
        queue.put_nowait(value)

    def _publish(self, value: PropertyValue) -> None:
        self._values[value.name] = value
        if self._callback_queue is not None and not self._closing:
            self._put(self._callback_queue, value)
        for queue, names in list(self._observers.values()):
            if value.name in names:
                self._put(queue, value)

    def _accept(self, result: dict[str, Any]) -> PropertyValue | None:
        definition = self._pairs.get((result.get("siid"), result.get("piid")))
        if definition is None:
            self.unknown_updates = min(_COUNT_LIMIT, self.unknown_updates + 1)
            return None
        previous = self._values[definition.name]
        if result.get("status") != 0 or not result.get("available", True):
            value = replace(previous, status="unavailable", source="gatt", error="Device did not provide an available value.")
        else:
            observed = result.get("value")
            error = None
            try:
                code, _ = encode_value(observed, definition.format)
                if code != result.get("type_code"):
                    raise DirectError("Wire type differs from this model's property definition.")
                source = self._source[definition.name]
                if self.model == "ES5BB":
                    source.validate(observed, reading=True)
                else:
                    source.validate(observed)
            except (DirectError, ValueError, TypeError):
                error = "Value/type/enum is outside this model's documented definition; raw value preserved."
            value = PropertyValue(definition, observed, "invalid_value" if error else "read",
                                  datetime.now(timezone.utc).isoformat(), "gatt", error)
        if (value.valid and result.get("operation") == 4 and not result.get("write_ack", False)
                and definition.pair != (6, 9)):
            previous_count = self._valid_op4_updates.get(definition.name, {}).get("count", 0)
            self._valid_op4_updates[definition.name] = {"count": min(_COUNT_LIMIT, previous_count + 1),
                                                       "last_at": value.updated_at}
        self._publish(value)
        return value

    async def _callbacks(self, queue: asyncio.Queue) -> None:
        # A callback may itself close and reconnect. An old worker must never
        # become a second consumer of that new connection's callback queue.
        while not self._closing and self._callback_queue is queue:
            value = await queue.get()
            try:
                result = self.on_update(value)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.callback_errors = min(_COUNT_LIMIT, self.callback_errors + 1)
                self.callback_error = f"on_update failed ({type(error).__name__}); callback details were not logged."

    async def _read(self, definition: PropertyDefinition, subscribe: bool | None = None) -> PropertyValue:
        if not definition.gatt_read:
            raise ClientError("This field is notification-only; select its stream and observe updates.", code="notification_only")
        assert self._session is not None
        result = await self._session.read(definition.siid, definition.piid,
            subscribe=(self.model == "ES5BB" or definition.gatt_notify) if subscribe is None else subscribe)
        value = self._accept(result)
        assert value is not None
        if not value.valid:
            raise ClientError(value.error or "Device returned an invalid value.", code="invalid_value")
        return value

    async def _wire(self, operation: Callable[[], Any]):
        self._check_loop()
        session = self._session
        assert self._operation_lock is not None
        try:
            async with self._operation_lock:
                self._check_loop()
                if self._session is not session:
                    raise ClientError("Connection changed while this operation was queued; submit it again explicitly.", code="session_changed")
                return await asyncio.wait_for(operation(), self.operation_timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if self._session is session:
                await self.aclose()
            raise

    async def read(self, key: PropertyKey, *, subscribe: bool | None = None) -> PropertyValue:
        """Read one allowed property; invalid values are cached but raise."""
        definition = self._definition(key)
        if subscribe is not None and type(subscribe) is not bool:
            raise ValueError("subscribe must be bool or None.")
        return await self._wire(lambda: self._read(definition, subscribe))

    async def keepalive(self) -> PropertyValue:
        """Read ES5's detect-region property, following the official keepalive.

        Long-running ES5 observers should call this about every 120 seconds.
        This client performs no automatic retry/reconnect or background polling.
        Other models do not inherit an undocumented keepalive command.
        """
        if self.model != "ES5BB":
            raise ClientError("This keepalive belongs to ES5BB only.", code="unsupported_keepalive")
        return await self.read("detect_region")

    async def refresh(self, names: Iterable[PropertyKey] | None = None, *, timeout: float | None = None) -> dict[str, PropertyValue]:
        """Read selected fields, preserving per-field unavailable/error status.

        Default selection is the whole readable catalog, which may take time on
        unsupported firmware. Prefer explicit names for routine use. No stream
        starts and notification-only fields never issue GET. An optional hard
        batch timeout follows cancellation semantics and closes the connection.
        """
        self._check_loop()
        session = self._session
        definitions = self._selection(names)
        if timeout is not None:
            timeout = _timeout(timeout, "timeout")
        result = {}
        async def run():
            for definition in definitions:
                self._check_loop()
                if self._session is not session:
                    raise ClientError("Connection changed during refresh; submit it again explicitly.", code="session_changed")
                if not definition.gatt_read:
                    result[definition.name] = self.get_property(definition.name)
                    continue
                try:
                    result[definition.name] = await self.read(definition.name)
                except DirectError as error:
                    previous = self._values[definition.name]
                    value = previous if previous.status == "invalid_value" else replace(previous,
                        status="unavailable" if isinstance(error, PropertyUnavailable) else "error", source="gatt",
                        error=str(error) if type(error) in (DirectError, PropertyUnavailable, ClientError) else "Property read failed.")
                    self._publish(value)
                    result[definition.name] = value
                    if self._session and self._session.error:
                        raise self._session.error
            return result
        if timeout is None:
            return await run()
        try:
            return await asyncio.wait_for(run(), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if self._session is session:
                await self.aclose()
            raise

    async def set(self, key: PropertyKey, value: Any) -> PropertyValue:
        """Fresh read → model/security checks → write → agreeing fresh read."""
        definition = self._definition(key)
        source = self._source[definition.name]
        source.validate(value)
        if self.model == "HS3BB" and self._device.miot_model != self.protocol_model:
            raise ClientError("Confirm HS3BB's protocol model before enabling writes.", code="model_unconfirmed")
        async def operation():
            current = await self._read(definition)
            if definition.allowed_mask is not None and current.value & ~definition.allowed_mask:
                raise ClientError("Unknown reserved bits are present; original settings were preserved.", code="unknown_reserved_bits")
            if self.model == "ES5BB" and definition.name == "pir_threshold":
                sensitivity = await self._read(self._definition("pir_sensitivity"))
                if sensitivity.value != 0:
                    raise ClientError("Select the explicit custom PIR sensitivity (0) before changing its threshold.", code="pir_not_custom")
            assert self._session is not None
            observed = self._accept(await self._session.set(definition.siid, definition.piid, value, definition.format))
            if observed is None or not observed.valid or observed.value != value:
                raise ClientError("Write did not produce an agreeing valid readback.", code="verification_failed")
            return observed
        return await self._wire(operation)

    def _check_stream(self, mode: str, *, requester: asyncio.Task | None = None) -> None:
        if self.model != "ES5BB":
            raise ClientError("Radar/PIR streams belong to ES5BB only.", code="unsupported_stream")
        if mode not in ("off", "radar", "pir"):
            raise ClientError("Stream must be off, radar or pir.", code="invalid_stream")
        if (self._stream_owner and self._stream_owner_session is self._session
                and self._stream_owner is not (requester or asyncio.current_task())):
            raise ClientError("A stream context is owned by another task.", code="stream_in_use")

    async def _start_stream(self, mode: str) -> None:
        assert self._session is not None
        # Unlike the dashboard, connect() deliberately performs no startup
        # refresh. Probe the selected fields with op2 snapshots; this is not
        # proof of the native SDK's push-registration operation. NULL snapshots
        # are legitimate and do not themselves establish a live data stream.
        names = {"radar": ("motion_energy", "presence_energy", "human_sensor_bitmaps"),
                 "pir": ("pir_energy",), "off": ()}[mode]
        for name in names:
            definition = self._definition(name)
            initial = await self._session.subscribe(*definition.pair)
            if initial is not None:
                self._accept(initial)
        if mode != "off" and not await self._session.command({"radar": 1, "pir": 4}[mode]):
            raise ClientError("Device did not acknowledge the selected stream.", code="stream_unconfirmed")
        self._stream_mode = mode

    async def _switch_stream(self, mode: str) -> None:
        assert self._session is not None
        original = self._stream_mode if self._stream_mode in ("off", "radar", "pir") else "off"
        try:
            if not await self._session.command(0):
                raise ClientError("Device did not acknowledge stopping the previous stream.", code="stream_unconfirmed")
            await self._start_stream(mode)
        except Exception:
            self._stream_mode = "unknown"
            with suppress(Exception):
                if await self._session.command(0):
                    await self._start_stream(original if original in ("radar", "pir") else "off")
            raise

    async def set_stream(self, mode: str) -> None:
        """Select one ES5 stream; failed switches attempt to restore the old one."""
        self._check_stream(mode)
        requester = asyncio.current_task()
        async def operation():
            # Conditions can change while this request waits for the wire lock.
            self._check_stream(mode, requester=requester)
            await self._switch_stream(mode)
        await self._wire(operation)

    @asynccontextmanager
    async def stream(self, mode: str):
        """Temporarily select a stream and restore the prior mode on exit.

        Nested contexts on the same task are supported. A body cancellation
        still restores its previous stream before propagating cancellation.
        """
        self._check_stream(mode)
        original = "off"
        owner = asyncio.current_task()
        session = self._session
        if self._stream_owner_session is not session:
            self._stream_depth = 0
        self._stream_owner_session = session
        self._stream_owner, self._stream_depth = owner, self._stream_depth + 1
        entered = False
        try:
            async def enter():
                nonlocal original
                self._check_stream(mode, requester=owner)
                original = self._stream_mode if self._stream_mode in ("off", "radar", "pir") else "off"
                await self._switch_stream(mode)
            await self._wire(enter)
            entered = True
            yield self
        finally:
            try:
                if entered and self.connected and self._session is session:
                    # Restoration is a separate protected task; ownership is
                    # already checked by this context, not reassigned to it.
                    async def restore():
                        if self._session is session:
                            await self._switch_stream(original)
                    await self._finish(self._wire(restore))
            finally:
                if self._stream_owner is owner and self._stream_owner_session is session:
                    self._stream_depth -= 1
                    if not self._stream_depth:
                        self._stream_owner, self._stream_owner_session = None, None

    async def radar_calibrate(self, *, empty_room: bool = False) -> bool:
        """Send fixed ES5 radar-calibration command 256; ACK is not completion."""
        self._check_stream("radar")
        if empty_room is not True:
            raise ClientError("Confirm an empty room and select the radar stream first.", code="calibration_preparation_required")
        requester = asyncio.current_task()
        async def operation():
            self._check_stream("radar", requester=requester)
            if self._stream_mode != "radar":
                raise ClientError("Confirm an empty room and select the radar stream first.", code="calibration_preparation_required")
            assert self._session is not None
            return await self._session.command(256)
        return await self._wire(operation)

    async def updates(self, names: Iterable[PropertyKey] | None = None, *, replay: bool = True) -> AsyncIterator[PropertyValue]:
        """Observe matching updates; each iterator owns its own bounded queue."""
        self._check_loop()
        if type(replay) is not bool:
            raise ValueError("replay must be bool.")
        selected = frozenset(definition.name for definition in self._selection(names))
        queue, token = asyncio.Queue(self.queue_size), object()
        self._observers[token] = (queue, selected)
        try:
            if replay:
                for name in selected:
                    value = self.get_property(name)
                    if value.status != "unread":
                        self._put(queue, value)
            while True:
                value = await queue.get()
                if value is _CLOSED:
                    return
                yield value
        finally:
            self._observers.pop(token, None)

    def _selection(self, names: Iterable[PropertyKey] | None) -> list[PropertyDefinition]:
        if names is None:
            return list(self.properties.values())
        if isinstance(names, str):
            return [self._definition(names)]
        return [self._definition(key) for key in names]

    async def _finish(self, coroutine):
        """Finish bounded cleanup even if its caller is cancelled again."""
        task = asyncio.ensure_future(coroutine)
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancelled = error
        result = task.result()
        if cancelled:
            raise cancelled
        return result

    async def aclose(self) -> None:
        """Idempotently stop owned ES5 reporting, close auth and release GATT."""
        if self._loop is None:
            return
        self._check_loop(connected=False)
        if self._close_task is None:
            self._closing = True
            self._close_caller = asyncio.current_task()
            self._close_task = asyncio.create_task(self._close(), name="linptech-close")
        caller = asyncio.current_task()
        self._close_waiters.add(caller)
        assert self._close_waiter_event is not None
        self._close_waiter_event.set()
        try:
            await self._finish(asyncio.shield(self._close_task))
        finally:
            self._close_waiters.discard(caller)
            self._close_waiter_event.set()

    async def _close(self) -> None:
        opening = self._opening_task
        if opening and opening is not self._close_caller:
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
        session, context = self._session, self._context
        if session:
            self._stream_mode_before_close = self._stream_mode
        try:
            if session:
                if self.model == "ES5BB" and self._stream_mode != "off" and self._ble and self._ble.is_connected:
                    with suppress(Exception):
                        await asyncio.wait_for(session.command(0), self.operation_timeout)
                with suppress(Exception):
                    await asyncio.wait_for(session.close(), 5)
        finally:
            if session:
                self._last_session_diagnostics = session.diagnostics()
            self._session = None
            self._stream_mode = "off"
            if context:
                with suppress(Exception):
                    await asyncio.wait_for(context.__aexit__(None, None, None), self.transport.connect_timeout + 10)
            self._ble, self._context = None, None
            callback = self._callback_task
            if callback and callback not in self._close_waiters:
                callback.cancel()
                # A callback's finally block may also call aclose(). Never
                # await a task that is itself waiting for this close task.
                # Bound arbitrary user cleanup: protocol/GATT are already shut.
                assert self._close_waiter_event is not None
                deadline = asyncio.get_running_loop().time() + 1
                while not callback.done() and callback not in self._close_waiters:
                    self._close_waiter_event.clear()
                    wake = asyncio.create_task(self._close_waiter_event.wait())
                    try:
                        completed, _ = await asyncio.wait((callback, wake),
                            timeout=max(0, deadline - asyncio.get_running_loop().time()),
                            return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        wake.cancel()
                        await asyncio.gather(wake, return_exceptions=True)
                    if not completed:
                        break
                if callback.done():
                    await asyncio.gather(callback, return_exceptions=True)
            self._callback_task, self._callback_queue = None, None
            for queue, _ in self._observers.values():
                self._put(queue, _CLOSED)
            # A prior connection's iterator must end even when its consumer is
            # paused during close/reconnect and has a one-item queue.
            self._observers.clear()
            for name, value in self._values.items():
                if value.status == "read":
                    self._values[name] = replace(value, status="cached", source="cache")
