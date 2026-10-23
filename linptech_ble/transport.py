"""Local BLE and explicitly selected ESPHome Native API transports.

ESPHome routes BLE replies to its advertisement subscriber, so advertisements
and GATT MUST share one Native API connection per proxy. A private broker loop
owns these sessions; callback and GATT facades marshal work to the caller loop.
No global habluetooth manager is installed and no APIClient crosses loops.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, field
import ipaddress
import inspect
import math
import re
import sys
import threading
from types import SimpleNamespace
from typing import Any, Callable

from .config import normalize_address


class TransportError(RuntimeError):
    """A credential-free transport failure suitable for display and logging."""


@dataclass(frozen=True)
class ProxyConfig:
    name: str
    host: str
    port: int = 6053
    noise_psk: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.name):
            raise ValueError("Proxy name 需為 1～64 個英數字、底線或連字號。")
        if not isinstance(self.host, str) or not self.host or len(self.host) > 253:
            raise ValueError("Proxy host 需為 IP 位址或主機名稱。")
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9.])?", self.host):
                raise ValueError("Proxy host 需為 IP 位址或主機名稱，不可包含 URL 或登入資訊。") from None
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Proxy port 需為 1～65535 的整數。")
        if self.noise_psk is not None:
            try:
                valid = isinstance(self.noise_psk, str) and len(base64.b64decode(self.noise_psk, validate=True)) == 32
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise ValueError("Proxy noise_psk 需為 ESPHome API 的 Base64 32-byte 金鑰。") from None
        if self.password is not None and (not isinstance(self.password, str) or not self.password):
            raise ValueError("Proxy password 需為非空字串或 null。")


@dataclass(frozen=True)
class BluetoothConfig:
    backend: str = "local"
    proxies: tuple[ProxyConfig, ...] = ()
    gatt_proxy: str | None = None
    connect_timeout: float = 15

    def __post_init__(self) -> None:
        if self.backend not in {"local", "esphome"}:
            raise ValueError("Bluetooth backend 只支援 local 或 esphome。")
        if not isinstance(self.proxies, (tuple, list)) or not all(isinstance(p, ProxyConfig) for p in self.proxies):
            raise ValueError("Bluetooth proxies 需為 ProxyConfig 清單。")
        object.__setattr__(self, "proxies", tuple(self.proxies))
        names = [p.name for p in self.proxies]
        if len(names) != len(set(names)):
            raise ValueError("Proxy name 不可重複。")
        endpoints = [(p.host.lower().rstrip("."), p.port) for p in self.proxies]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("同一 Proxy 位址與埠號不可重複設定。")
        if self.backend == "esphome" and not self.proxies:
            raise ValueError("esphome backend 至少需設定一個 Proxy。")
        if self.gatt_proxy is not None and self.gatt_proxy not in names:
            raise ValueError("gatt_proxy 需指定已設定的 Proxy name。")
        if type(self.connect_timeout) not in {int, float} or not math.isfinite(self.connect_timeout) or not 1 <= self.connect_timeout <= 120:
            raise ValueError("Bluetooth connect_timeout 需為 1～120 秒。")

    def direct_proxy(self) -> ProxyConfig:
        if self.gatt_proxy:
            return next(p for p in self.proxies if p.name == self.gatt_proxy)
        if len(self.proxies) == 1:
            return self.proxies[0]
        raise TransportError("多個 ESPHome Proxy 時，請先設定 gatt_proxy，指定 ES5BB 所在房間的 Proxy。")

    def public_info(self) -> dict[str, Any]:
        return {"backend": self.backend, "proxies": [p.name for p in self.proxies], "gatt_proxy": self.gatt_proxy}


def _dependencies() -> tuple[Any, ...]:
    try:
        from aioesphomeapi import APIClient, BluetoothProxyFeature
        from bleak_esphome.backend.client import ESPHomeClient, ESPHomeClientData
        from bleak_esphome.backend.device import ESPHomeBluetoothDevice
    except ImportError:
        raise TransportError("ESPHome 支援尚未安裝；請執行 python -m pip install -e .[proxy]。") from None
    return APIClient, BluetoothProxyFeature, ESPHomeClient, ESPHomeClientData, ESPHomeBluetoothDevice


def _api_client(proxy: ProxyConfig) -> Any:
    api_type = _dependencies()[0]
    return api_type(address=proxy.host, port=proxy.port, password=proxy.password,
                    noise_psk=proxy.noise_psk, client_info="Linptech BLE Lab")


def _advertisement(proxy: ProxyConfig, incoming: Any) -> tuple[Any, Any]:
    """Preserve complete decoded Native API service/manufacturer payloads."""
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
    from .models import canonical_uuid

    if type(incoming.address) is not int or not 0 <= incoming.address < 2**48:
        raise ValueError("Invalid proxy advertisement address")
    if type(incoming.rssi) is not int or not -128 <= incoming.rssi <= 127:
        raise ValueError("Invalid proxy advertisement RSSI")
    address = ":".join(f"{incoming.address:012X}"[i:i + 2] for i in range(0, 12, 2))
    device = BLEDevice(address, incoming.name or None,
                       {"source": proxy.name, "address_type": incoming.address_type})
    adv = AdvertisementData(local_name=incoming.name or None,
        manufacturer_data=dict(incoming.manufacturer_data),
        service_data={canonical_uuid(k): bytes(v) for k, v in incoming.service_data.items()},
        service_uuids=[canonical_uuid(k) for k in incoming.service_uuids],
        tx_power=getattr(incoming, "tx_power", None), rssi=incoming.rssi, platform_data=(proxy.name,))
    return device, adv


class _ConnectionOnlyScanner:
    """The pinned ESPHomeClient only needs connecting() on this object.

    Keeping capture active during GATT setup also avoids installing the global
    habluetooth manager through ESPHomeScanner's constructor.
    """
    def connecting(self):
        return nullcontext()


class _ProxySession:
    """One API owner for a physical proxy, wholly confined to the broker loop."""
    def __init__(self, proxy: ProxyConfig, timeout: float):
        self.proxy, self.timeout = proxy, timeout
        self.refs = 0
        self.callbacks: dict[object, Callable] = {}
        self.first = asyncio.Event()
        self.closed = False
        self.data: Any = None
        self.api: Any = None
        self.clients: set[Any] = set()
        self.state = {"name": proxy.name, "connected": False, "error": None,
                      "subscription_confirmed": False, "reconnects": 0,
                      "scanner_state": None, "malformed_advertisements": 0}
        self.task = asyncio.create_task(self._run(), name=f"proxy-{proxy.name}")

    def _invalidate_clients(self) -> None:
        if self.data:
            self.data.bluetooth_device.async_set_unavailable()
            for callback in list(self.data.disconnect_callbacks):
                with suppress(Exception):
                    callback()
            self.data.disconnect_callbacks.clear()

    def _incoming(self, incoming: Any) -> None:
        try:
            args = _advertisement(self.proxy, incoming)
        except (ValueError, TypeError, AttributeError, OverflowError):
            self.state["malformed_advertisements"] += 1
            return
        for callback in list(self.callbacks.values()):
            callback(*args)

    def _raw_incoming(self, batch: Any) -> None:
        from bluetooth_data_tools import parse_advertisement_data_bytes
        for raw in batch.advertisements:
            try:
                name, uuids, services, manufacturers, power = parse_advertisement_data_bytes(raw.data)
                self._incoming(SimpleNamespace(address=raw.address, rssi=raw.rssi,
                    address_type=raw.address_type, name=name, service_uuids=uuids,
                    service_data=services, manufacturer_data=manufacturers, tx_power=power))
            except (ValueError, TypeError, AttributeError, OverflowError, IndexError):
                self.state["malformed_advertisements"] += 1

    async def _run(self) -> None:
        while not self.closed:
            api, unsub = None, []
            gone, running = asyncio.Event(), asyncio.Event()
            transition: asyncio.TimerHandle | None = None
            async def on_stop(_expected: bool) -> None:
                self.state.update(connected=False, error="Proxy API 連線中斷。")
                self._invalidate_clients()
                gone.set()
            def scanner_stalled() -> None:
                self.state.update(connected=False, error="Proxy 藍牙掃描已停止，請檢查韌體與廣播訂閱是否被其他程式佔用。")
                gone.set()
            def on_scanner_state(value: Any) -> None:
                nonlocal transition
                # ESPHome wire values: IDLE 0, STARTING 1, RUNNING 2,
                # FAILED 3, STOPPING 4, STOPPED 5. Brief pauses are normal
                # while initiating GATT or changing scan mode.
                mode = getattr(value, "state", None)
                self.state["scanner_state"] = int(mode) if mode is not None else None
                if transition:
                    transition.cancel()
                    transition = None
                if mode == 2:
                    running.set()
                elif mode == 3:
                    scanner_stalled()
                elif running.is_set():
                    transition = asyncio.get_running_loop().call_later(5, scanner_stalled)
            try:
                api = self.api = _api_client(self.proxy)
                async with asyncio.timeout(self.timeout):
                    await api.connect(on_stop=on_stop, login=True, log_errors=False)
                    info = await api.device_info()
                    _, feature, _, data_type, device_type = _dependencies()
                    flags = info.bluetooth_proxy_feature_flags_compat(api.api_version)
                    if not flags & feature.PASSIVE_SCAN:
                        raise TransportError("ESPHome 裝置未啟用 Bluetooth Proxy；請檢查 bluetooth_proxy 韌體設定。")
                    if flags & feature.ACTIVE_CONNECTIONS:
                        source = info.bluetooth_mac_address or info.mac_address
                        device = device_type(self.proxy.name, source, available=True)
                        self.data = data_type(bluetooth_device=device, client=api, device_info=info,
                            api_version=api.api_version, title=self.proxy.name, scanner=_ConnectionOnlyScanner())
                        unsub.append(api.subscribe_bluetooth_connections_free(device.async_update_ble_connection_limits))
                    has_state = bool(flags & feature.FEATURE_STATE_AND_MODE)
                    if has_state:
                        unsub.append(api.subscribe_bluetooth_scanner_state(on_scanner_state))
                    if flags & feature.RAW_ADVERTISEMENTS:
                        unsub.append(api.subscribe_bluetooth_le_raw_advertisements(self._raw_incoming))
                    else:
                        unsub.append(api.subscribe_bluetooth_le_advertisements(self._incoming))
                    if has_state:
                        # A RUNNING reply proves the subscription was accepted
                        # and the radio is scanning, not merely API-connected.
                        waiter = asyncio.create_task(running.wait())
                        lost = asyncio.create_task(gone.wait())
                        try:
                            done, _ = await asyncio.wait({waiter, lost}, return_when=asyncio.FIRST_COMPLETED)
                            if lost in done:
                                raise TransportError("Proxy 廣播尚未就緒便已離線或停止掃描。")
                        finally:
                            waiter.cancel()
                            lost.cancel()
                            await asyncio.gather(waiter, lost, return_exceptions=True)
                    self.state.update(connected=True, error=None, subscription_confirmed=has_state)
                self.first.set()
                await gone.wait()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                public = str(error) if isinstance(error, TransportError) else f"Proxy 連線或廣播訂閱失敗（{type(error).__name__}）；檢查 API 設定並避免其他程式佔用廣播訂閱。"
                self.state.update(connected=False, error=public)
            finally:
                self.state["connected"] = False
                self.first.set()
                self._invalidate_clients()
                self.data = None
                if transition:
                    transition.cancel()
                for callback in reversed(unsub):
                    with suppress(Exception):
                        callback()
                if api:
                    with suppress(Exception):
                        await asyncio.wait_for(api.disconnect(), 4)
                self.api = None
            if not self.closed:
                self.state["reconnects"] += 1
                await asyncio.sleep(2)

    async def close(self) -> None:
        self.closed = True
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class _Broker:
    """Process-local event loop: one Native API subscriber per proxy."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.sessions: dict[ProxyConfig, _ProxySession] = {}
        self.thread = threading.Thread(target=self._run, name="esphome-api", daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def call(self, coroutine):
        if asyncio.get_running_loop() is self.loop:
            return await coroutine
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, self.loop))

    async def acquire(self, proxy: ProxyConfig, timeout: float, token: object, callback=None):
        # Refuse conflicting owners of one endpoint instead of opening another
        # API subscription with a different name/key by accident.
        if any(p != proxy and (p.host.lower().rstrip("."), p.port) == (proxy.host.lower().rstrip("."), proxy.port)
               for p in self.sessions):
            raise TransportError("同一 ESPHome Proxy 已由不同設定使用；請統一 Proxy 名稱與 API 設定。")
        session = self.sessions.get(proxy)
        if session is not None and session.closed:
            # Last-user cleanup may still be awaiting a socket close. Do not
            # subscribe a replacement while that session is still retiring.
            await asyncio.gather(session.task, return_exceptions=True)
            if self.sessions.get(proxy) is session:
                del self.sessions[proxy]
            session = self.sessions.get(proxy)
        if session is None:
            session = self.sessions[proxy] = _ProxySession(proxy, timeout)
        session.refs += 1
        if callback:
            session.callbacks[token] = callback
        try:
            await session.first.wait()
        except BaseException:
            await self.release(proxy, token)
            raise
        return session

    async def release(self, proxy: ProxyConfig, token: object):
        session = self.sessions.get(proxy)
        if session is None:
            return
        session.callbacks.pop(token, None)
        session.refs -= 1
        if not session.refs:
            await session.close()
            if self.sessions.get(proxy) is session:
                del self.sessions[proxy]


_broker_instance: _Broker | None = None
_broker_lock = threading.Lock()


def _broker() -> _Broker:
    global _broker_instance
    with _broker_lock:
        if _broker_instance is None:
            _broker_instance = _Broker()
    return _broker_instance


def _deliver(loop: asyncio.AbstractEventLoop, callback: Callable, *args) -> None:
    with suppress(RuntimeError):
        loop.call_soon_threadsafe(callback, *args)


class _RemoteScanner:
    """Caller-loop handle; sessions and all Native API work stay in broker."""
    def __init__(self, config: BluetoothConfig, callback: Callable):
        self.config, self.callback = config, callback
        self.sessions: list[_ProxySession] = []
        self.token = object()
        self.closed = False

    @property
    def tasks(self):
        return [s.task for s in self.sessions]

    @property
    def state(self):
        return {s.proxy.name: s.state for s in self.sessions}

    def health(self) -> dict[str, Any]:
        return {"backend": "esphome", "connected": any(s.state["connected"] for s in self.sessions),
                "proxies": [dict(s.state) for s in self.sessions]}

    def check_health(self) -> None:
        if not self.health()["connected"]:
            raise TransportError("所有 ESPHome Proxy 均已離線或停止掃描；請確認網路、API 金鑰與廣播訂閱擁有者。")

    async def __aenter__(self):
        _dependencies()
        loop = asyncio.get_running_loop()
        broker = _broker()
        async def open_all():
            async def open_one(proxy):
                session = await broker.acquire(proxy, self.config.connect_timeout, self.token,
                    lambda *args: _deliver(loop, self.callback, *args))
                self.sessions.append(session)
            opening = [asyncio.create_task(open_one(proxy)) for proxy in self.config.proxies]
            try:
                await asyncio.gather(*opening)
            except BaseException:
                for task in opening:
                    task.cancel()
                await asyncio.gather(*opening, return_exceptions=True)
                for session in self.sessions:
                    await broker.release(session.proxy, self.token)
                self.sessions.clear()
                raise
        try:
            await broker.call(open_all())
            self.check_health()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *_):
        if self.closed:
            return
        self.closed = True
        async def close_all():
            for session in self.sessions:
                await _broker().release(session.proxy, self.token)
        await _broker().call(close_all())


def _winrt_out_of_range(advertisement: Any) -> bool:
    """Recognize a native WinRT range-loss indication, not a weak BLE packet.

    Windows may generate an RSSI -127 event containing old advertisement data:
    https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.bluetoothsignalstrengthfilter.outofrangetimeout
    Bleak exposes the current event's RSSI and its native RawAdvData pair. Check
    both rather than applying an RSSI threshold to portable records/proxies.
    Avoid importing the Windows backend here, keeping core imports portable.
    """
    try:
        if advertisement.rssi != -127:
            return False
        platform = advertisement.platform_data
        if not isinstance(platform, tuple) or len(platform) != 2:
            return False
        raw = platform[1]
        if (type(raw).__module__, type(raw).__name__) != ("bleak.backends.winrt.scanner", "RawAdvData"):
            return False
        # Bleak's current event is either adv or scan, and its raw RSSI must
        # agree with the sentinel exposed in AdvertisementData.rssi.
        return any(event is not None and event.raw_signal_strength_in_dbm == -127
                   for event in (raw.adv, raw.scan))
    except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
        # Incomplete or foreign metadata cannot prove an OS range-loss event.
        return False


@asynccontextmanager
async def scanner_context(config: BluetoothConfig | None, detection_callback: Callable[..., Any]):
    """Yield selected scanner; remote mode never touches a local adapter."""
    config = config or BluetoothConfig()
    if config.backend == "local":
        from bleak import BleakScanner
        callback = detection_callback
        if sys.platform == "win32":
            if inspect.iscoroutinefunction(detection_callback):
                async def callback(device, advertisement):
                    if not _winrt_out_of_range(advertisement):
                        await detection_callback(device, advertisement)
            else:
                def callback(device, advertisement):
                    if not _winrt_out_of_range(advertisement):
                        detection_callback(device, advertisement)
        async with BleakScanner(detection_callback=callback) as scanner:
            yield scanner
    else:
        async with _RemoteScanner(config, detection_callback) as scanner:
            yield scanner


class _RemoteClient:
    """Bleak facade with operations on broker and notifications on caller loop."""
    def __init__(self, client: Any, loop: asyncio.AbstractEventLoop):
        self.client, self.loop = client, loop

    @property
    def services(self):
        return self.client.services

    @property
    def is_connected(self):
        return self.client.is_connected

    @property
    def mtu_size(self):
        return self.client.mtu_size

    async def read_gatt_char(self, *args, **kwargs):
        return await _broker().call(self.client.read_gatt_char(*args, **kwargs))

    async def write_gatt_char(self, *args, **kwargs):
        return await _broker().call(self.client.write_gatt_char(*args, **kwargs))

    async def start_notify(self, characteristic, callback, **kwargs):
        def incoming(sender, data):
            _deliver(self.loop, callback, sender, data)
        return await _broker().call(self.client.start_notify(characteristic, incoming, **kwargs))

    async def stop_notify(self, *args, **kwargs):
        return await _broker().call(self.client.stop_notify(*args, **kwargs))

    async def disconnect(self):
        return await _broker().call(self.client.disconnect())


@asynccontextmanager
async def connected_client(config: BluetoothConfig | None, address: str, name: str = "ES5BB"):
    """Yield a connected Bleak-compatible client, cleaning GATT and API refs.

    Remote sessions are shared with capture in the SAME PROCESS. Therefore the
    remote DirectController runs in a thread, while local WinRT alone retains
    process isolation. No credentials or API objects pass between processes.
    """
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
    from bleak.exc import BleakDeviceNotFoundError
    config = config or BluetoothConfig()
    address = normalize_address(address)
    if config.backend == "local":
        if sys.platform == "win32":
            target = BLEDevice(address, name, None)
        else:
            target = await BleakScanner.find_device_by_address(address, timeout=10)
            if target is None:
                raise TransportError("未找到目標裝置；請依裝置說明喚醒並靠近藍牙接收器。")
        client = BleakClient(target, timeout=18, winrt={"use_cached_services": False})
        try:
            try:
                await asyncio.wait_for(client.connect(), 22)
            except BleakDeviceNotFoundError:
                if sys.platform != "win32":
                    raise
                # A manually constructed BLEDevice skips Bleak's discovery.
                # Windows cannot create an unpaired BluetoothLEDevice until
                # an advertisement watcher has populated its system cache.
                # Release this first client before starting that watcher; only
                # cache-miss errors get one discovery/retry, never timeouts or
                # authentication errors. The original fast path stays intact.
                with suppress(Exception):
                    await asyncio.wait_for(client.disconnect(), 4)
                client = None
                scan_timeout = min(10.0, config.connect_timeout)
                target = await asyncio.wait_for(
                    BleakScanner.find_device_by_address(address, timeout=scan_timeout), scan_timeout + 1)
                if target is None:
                    raise TransportError("未找到目標裝置；請依裝置說明喚醒並靠近藍牙接收器。")
                client = BleakClient(target, timeout=18, winrt={"use_cached_services": False})
                await asyncio.wait_for(client.connect(), 22)
            yield client
        finally:
            if client is not None:
                with suppress(Exception):
                    await asyncio.wait_for(client.disconnect(), 4)
        return

    _dependencies()
    proxy, token, session, client = config.direct_proxy(), object(), None, None
    broker, caller_loop = _broker(), asyncio.get_running_loop()
    async def open_client():
        nonlocal session, client
        try:
            session = await broker.acquire(proxy, config.connect_timeout, token)
            if not session.state["connected"]:
                raise TransportError("所選 ESPHome Proxy 尚未就緒；請確認 API 與廣播訂閱狀態。")
            if not session.data:
                raise TransportError("所選 ESPHome Proxy 僅支援廣播；裝置直連需設定 bluetooth_proxy.active: true。")
            data = session.data
            await data.bluetooth_device.wait_for_ble_connections_free(config.connect_timeout)
            target = BLEDevice(address, name, {"source": data.bluetooth_device.mac_address, "address_type": 0})
            client = BleakClient(target, backend=_dependencies()[2], client_data=data, timeout=18)
            session.clients.add(client)
            await asyncio.wait_for(client.connect(), 22)
            return _RemoteClient(client, caller_loop)
        except asyncio.CancelledError:
            raise
        except TransportError:
            raise
        except Exception as error:
            raise TransportError(f"ESPHome 直連失敗（{type(error).__name__}）；確認 API、可用連線槽與裝置喚醒狀態。") from None
    async def close_client():
        if client:
            if session:
                session.clients.discard(client)
            with suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 5)
        if session:
            await broker.release(proxy, token)
    try:
        facade = await broker.call(open_client())
        yield facade
    finally:
        await broker.call(close_client())


