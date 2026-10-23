"""Optional, explicit Xiaomi Cloud credential retrieval for owned devices.

Only the QR image and redacted metadata cross the dashboard boundary. Account
sessions and device credentials stay in memory until an explicit selected save.
Cloud protocol adaptation: PiotrMachowski/Xiaomi-cloud-tokens-extractor (MIT);
see THIRD_PARTY_NOTICES.md. This is not an official Xiaomi SDK.
"""
from __future__ import annotations

import base64
from contextlib import nullcontext
from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import math
import secrets
import struct
import threading
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
from cryptography.hazmat.primitives.ciphers import Cipher

from .config import normalize_address
from .standard_auth import DirectError
from .xiaomi_import import MODEL_ALIASES

REGIONS = ("cn", "de", "us", "ru", "tw", "sg", "in", "i2")
_PNG = b"\x89PNG\r\n\x1a\n"
_MAX_BODY = 2 * 1024 * 1024
_MAX_QR = 256 * 1024
_MAX_DEVICES = 1000
_RUN_SECONDS = 600
_SELECT_SECONDS = 300
_ACTIVE = {"preparing", "awaiting_qr", "querying", "selecting", "saving"}


class CloudCredentialError(ValueError):
    """A fixed, public-safe error; never wrap upstream exception text."""


class _Cancelled(CloudCredentialError):
    pass


class _Expired(CloudCredentialError):
    pass


class _PollTimeout(Exception):
    pass


def _credential(value: Any, length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if len(value) == length and all(c in "0123456789abcdef" for c in value) else None


def _name(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    return "".join(c for c in value if ord(c) >= 32 and ord(c) != 127).strip()[:120] or fallback


def _json(body: bytes) -> dict[str, Any]:
    try:
        text = body.decode("utf-8")
        if text.startswith("&&&START&&&"):
            text = text[len("&&&START&&&"):]
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise CloudCredentialError("小米雲端回覆格式無法辨識；未變更任何裝置。") from None


def _validated_url(value: Any) -> str:
    try:
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError()
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username
                or parsed.password or parsed.fragment or not host
                or not any(host == domain or host.endswith("." + domain) for domain in ("xiaomi.com", "mi.com"))):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise CloudCredentialError("小米雲端提供了不允許的登入位址；已停止查詢。") from None


def _rc4(key: bytes, value: bytes) -> bytes:
    cipher = Cipher(ARC4(key), mode=None).encryptor()
    cipher.update(b"\0" * 1024)
    return cipher.update(value) + cipher.finalize()


class XiaomiCloudProvider:
    """Bounded HTTPS-only QR login and scoped cloud calls, without disk caches.

    ``http`` is an injectable requests-compatible session for synthetic tests.
    No network operation occurs during import or construction.
    """

    def __init__(self, region: str, cancelled: threading.Event, *, http: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        if region not in REGIONS:
            raise CloudCredentialError("請選擇有效的米家地區。")
        if http is None:
            try:
                import requests
            except ImportError:
                raise CloudCredentialError("雲端取憑證需要選用的 cloud 套件；請先安裝 linptech-ble[cloud]。") from None
            http = requests.Session()
            timeout_types = (requests.exceptions.Timeout, TimeoutError)
        else:
            timeout_types = (TimeoutError,)
        self.region, self.cancelled, self.clock = region, cancelled, clock
        self.http, self._timeout_types = http, timeout_types
        # Avoid implicit .netrc authorization and environmental proxy credentials.
        self.http.trust_env = False
        self._user_id: str | None = None
        self._security: bytes | None = None
        self._service_token: str | None = None
        self._poll_url: str | None = None
        self._expires_at = 0.0
        self._closed = False

    def _check(self, deadline: float | None = None) -> None:
        if self.cancelled.is_set() or self._closed:
            raise _Cancelled("已取消雲端登入。")
        if deadline is not None and self.clock() >= deadline:
            raise _Expired("登入或選取已逾期；請重新產生 QR。")

    def _request(self, method: str, url: str, *, deadline: float | None = None,
                 max_bytes: int = _MAX_BODY, poll: bool = False, **kwargs: Any) -> bytes:
        deadline = min(deadline or self.clock() + 25, self.clock() + 25)
        url = _validated_url(url)
        for redirect in range(4):
            self._check(deadline)
            response = None
            try:
                remaining = max(.05, deadline - self.clock())
                response = self.http.request(method, url, timeout=(min(5, remaining), min(8, remaining)),
                    allow_redirects=False, stream=True, **kwargs)
                if response.status_code in (301, 302, 303, 307, 308):
                    if method != "GET" or redirect == 3:
                        raise CloudCredentialError("小米雲端重新導向次數過多；請重試。")
                    url = _validated_url(urljoin(url, response.headers.get("Location", "")))
                    kwargs.pop("params", None)
                    continue
                if response.status_code != 200:
                    if poll and response.status_code in (408, 504):
                        raise _PollTimeout()
                    raise CloudCredentialError("小米雲端暫時無法完成請求；請稍後重試。")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > max_bytes or int(declared) < 0:
                            raise ValueError()
                    except (ValueError, TypeError):
                        raise CloudCredentialError("小米雲端回覆超過安全大小限制。") from None
                body = bytearray()
                # A one-byte iterator also checks the absolute deadline for a
                # slow trickle; requests' read timeout alone is only inactivity.
                for chunk in response.iter_content(chunk_size=1):
                    self._check(deadline)
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise CloudCredentialError("小米雲端回覆超過安全大小限制。")
                self._check(deadline)
                return bytes(body)
            except self._timeout_types:
                self._check(deadline)
                if poll:
                    raise _PollTimeout() from None
                raise CloudCredentialError("連線至小米雲端逾時；請稍後重試。") from None
            except (CloudCredentialError, _PollTimeout):
                raise
            except Exception:
                raise CloudCredentialError("無法連線至小米雲端；請檢查網路後重試。") from None
            finally:
                if response is not None:
                    response.close()
        raise CloudCredentialError("小米雲端重新導向次數過多；請重試。")

    def prepare(self) -> tuple[bytes, float]:
        data = _json(self._request("GET", "https://account.xiaomi.com/longPolling/loginUrl", params={
            "_qrsize": "480", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "callback": "https://sts.api.io.mi.com/sts", "_hasLogo": "false", "sid": "xiaomiio",
            "serviceParam": "", "_locale": "en_GB", "_dc": str(int(self.clock() * 1000)),
        }))
        try:
            lifetime = data["timeout"]
            if type(lifetime) not in (int, float) or not math.isfinite(lifetime) or not 1 <= lifetime <= 600:
                raise ValueError()
            image_url = _validated_url(data["qr"])
            self._poll_url = _validated_url(data["lp"])
            self._expires_at = self.clock() + lifetime
        except (KeyError, TypeError, ValueError):
            raise CloudCredentialError("無法準備米家登入 QR；請重試。") from None
        image = self._request("GET", image_url, max_bytes=_MAX_QR, deadline=self._expires_at)
        if not image.startswith(_PNG):
            raise CloudCredentialError("小米登入 QR 不是有效的 PNG 圖片；請重試。")
        return image, self._expires_at

    def poll_login(self, deadline: float) -> None:
        if self._poll_url is None:
            raise CloudCredentialError("尚未準備登入 QR。")
        deadline = min(deadline, self._expires_at)
        while True:
            self._check(deadline)
            try:
                data = _json(self._request("GET", self._poll_url, deadline=deadline, poll=True))
                break
            except _PollTimeout:
                # Back off even if an endpoint returns an immediate 408.
                if self.cancelled.wait(.15):
                    raise _Cancelled("已取消雲端登入。")
        try:
            user_id, security, location = data["userId"], data["ssecurity"], _validated_url(data["location"])
            if not isinstance(user_id, (int, str)) or not str(user_id).isdigit():
                raise ValueError()
            decoded = base64.b64decode(security, validate=True)
            if len(decoded) != 16:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise CloudCredentialError("米家登入尚未完成或已過期；請重新掃碼確認。") from None
        self._user_id, self._security = str(user_id), decoded
        self._request("GET", location, deadline=deadline)
        # The redirect chain can set a serviceToken at its original or final URL.
        tokens = {cookie.value for cookie in self.http.cookies if cookie.name == "serviceToken"
                  and (cookie.domain.lstrip(".") == "mi.com" or cookie.domain.endswith(".mi.com"))}
        if len(tokens) != 1 or not next(iter(tokens)):
            raise CloudCredentialError("無法建立米家雲端工作階段；請重新登入。")
        self._service_token = tokens.pop()
        self._poll_url = None

    def _api(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        self._check()
        if self._security is None or self._service_token is None or self._user_id is None:
            raise CloudCredentialError("米家雲端尚未登入。")
        nonce = secrets.token_bytes(8) + struct.pack(">I", int(self.clock() // 60))
        signed = hashlib.sha256(self._security + nonce).digest()
        signed64 = base64.b64encode(signed).decode("ascii")
        plain = {"data": json.dumps(data, separators=(",", ":"), ensure_ascii=False)}

        def signature(values: dict[str, str]) -> str:
            text = "&".join(["POST", path] + [f"{key}={value}" for key, value in values.items()] + [signed64])
            return base64.b64encode(hashlib.sha1(text.encode()).digest()).decode("ascii")

        plain["rc4_hash__"] = signature(plain)
        fields = {key: base64.b64encode(_rc4(signed, value.encode())).decode("ascii") for key, value in plain.items()}
        fields.update(signature=signature(fields), ssecurity=base64.b64encode(self._security).decode("ascii"),
                      _nonce=base64.b64encode(nonce).decode("ascii"))
        host = ("" if self.region == "cn" else self.region + ".") + "api.io.mi.com"
        body = self._request("POST", "https://" + host + "/app" + path, data=fields, headers={
            "Accept-Encoding": "identity", "User-Agent": "Android-7.1.1-1.0.0-ONEPLUS A3010-136-" +
                "APP/com.xiaomi.mihome APPV/10.5.201",
            "Content-Type": "application/x-www-form-urlencoded", "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }, cookies={"userId": self._user_id, "yetAnotherServiceToken": self._service_token,
                    "serviceToken": self._service_token, "locale": "en_GB", "timezone": "GMT+08:00",
                    "is_daylight": "0", "dst_offset": "0", "channel": "MI_APP_STORE"})
        try:
            decoded = _rc4(signed, base64.b64decode(body, validate=True))
        except (ValueError, TypeError):
            raise CloudCredentialError("無法解讀米家雲端加密回覆；請重新登入。") from None
        return _json(decoded)

    @staticmethod
    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result")
        if payload.get("code") != 0 or not isinstance(result, dict):
            raise CloudCredentialError("無法查詢所選地區的米家裝置；請確認地區與帳號權限。")
        return result

    def list_devices(self) -> list[dict[str, Any]]:
        result = self._result(self._api("/v2/homeroom/gethome", {
            "fg": True, "fetch_share": True, "fetch_share_dev": True, "limit": 300, "app_ver": 7}))
        homes = [(home.get("id"), self._user_id) for home in result.get("homelist", []) if isinstance(home, dict)]
        shared = self._result(self._api("/v2/user/get_device_cnt", {"fetch_own": True, "fetch_share": True}))
        share = shared.get("share") or {}
        if not isinstance(share, dict):
            raise CloudCredentialError("米家家庭清單格式無法辨識。")
        homes += [(home.get("home_id"), home.get("home_owner")) for home in share.get("share_family", [])
                  if isinstance(home, dict)]
        homes = list(dict.fromkeys((home, owner) for home, owner in homes
                                  if isinstance(home, (int, str)) and str(home).isdigit()
                                  and isinstance(owner, (int, str)) and str(owner).isdigit()))
        if len(homes) > 32:
            raise CloudCredentialError("米家家庭數量超過本工具限制；未變更任何裝置。")
        devices: list[dict[str, Any]] = []
        for home, owner in homes:
            result = self._result(self._api("/v2/home/home_device_list", {
                "home_owner": int(owner), "home_id": int(home), "limit": 200,
                "get_split_device": True, "support_smart_home": True}))
            rows = result.get("device_info") or []
            if not isinstance(rows, list):
                raise CloudCredentialError("米家裝置清單格式無法辨識。")
            for row in rows:
                if not isinstance(row, dict) or row.get("model") not in MODEL_ALIASES:
                    continue
                try:
                    address = normalize_address(row.get("mac"))
                except ValueError:
                    continue
                did = row.get("did")
                if not isinstance(did, str) or not 1 <= len(did) <= 128:
                    continue
                devices.append({"address": address, "model": MODEL_ALIASES[row["model"]],
                    "miot_model": row["model"], "name": _name(row.get("name"), MODEL_ALIASES[row["model"]]),
                    "did": did, "token": _credential(row.get("token"), 24)})
                if len(devices) > _MAX_DEVICES:
                    raise CloudCredentialError("米家感測器數量超過本工具限制；未變更任何裝置。")
        return devices

    def fetch_credentials(self, device: dict[str, Any]) -> dict[str, str | None]:
        response = self._api("/v2/device/blt_get_beaconkey", {"did": device["did"], "pdid": 1})
        result = response.get("result")
        key = result.get("beaconkey") if response.get("code") == 0 and isinstance(result, dict) else None
        if key is not None and _credential(key, 32) is None:
            raise CloudCredentialError("小米雲端回傳的 BLE key 格式不符；未變更任何裝置。")
        return {"bindkey": _credential(key, 32), "auth_token": _credential(device.get("token"), 24)}

    def close(self) -> None:
        self._closed = True
        self._security = self._user_id = self._service_token = self._poll_url = None
        self._expires_at = 0
        try:
            self.http.cookies.clear()
            self.http.close()
        except Exception:
            pass


@dataclass(repr=False)
class _Run:
    id: str
    region: str
    hard_deadline: float
    expires_at: float
    phase: str = "preparing"
    revision: int = 0
    cancelled: threading.Event = field(default_factory=threading.Event)
    provider: Any = None
    qr: bytes | None = field(default=None, repr=False)
    devices: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    public_devices: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    timer: threading.Timer | None = None


class CloudCredentialManager:
    """One explicit browser session; no stored login or automatic cloud calls.

    Provider contract: ``prepare() -> (PNG bytes, expiry epoch seconds)``,
    ``poll_login(expiry)``, ``list_devices()`` (private records),
    ``fetch_credentials(record)`` and ``close()``. All calls run off HTTP threads.
    """

    def __init__(self, inventory: Any, *, provider_factory: Callable | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.inventory, self.clock = inventory, clock
        self._custom_provider = provider_factory is not None
        self.provider_factory = provider_factory or (lambda region, cancelled: XiaomiCloudProvider(region, cancelled, clock=clock))
        self.lock = threading.RLock()
        self._run: _Run | None = None
        self._threads: set[threading.Thread] = set()
        self._closed = False

    def _dependencies(self) -> bool:
        return self._custom_provider or importlib.util.find_spec("requests") is not None

    def _clear_private(self, run: _Run) -> Any:
        run.cancelled.set()
        if run.timer:
            run.timer.cancel()
            run.timer = None
        provider, run.provider = run.provider, None
        run.qr = None
        run.devices.clear()
        return provider

    @staticmethod
    def _close_provider(provider: Any) -> None:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass

    def _expire(self, run: _Run) -> None:
        provider = None
        with self.lock:
            if self._run is run and run.phase in _ACTIVE and self.clock() >= run.expires_at:
                run.phase, run.error = "expired", "登入或選取已逾期；請重新產生 QR。"
                provider = self._clear_private(run)
                run.public_devices.clear()
        self._close_provider(provider)

    def _timer(self, run: _Run) -> None:
        if run.timer:
            run.timer.cancel()
        run.timer = threading.Timer(max(.01, run.expires_at - self.clock()), self._expire, (run,))
        run.timer.daemon = True
        run.timer.start()

    def _current(self, run: _Run) -> bool:
        return self._run is run and not self._closed and not run.cancelled.is_set()

    def snapshot(self) -> dict[str, Any]:
        run = self._run
        if run:
            self._expire(run)
        with self.lock:
            run = self._run
            return {"enabled": not self._closed, "dependencies_available": self._dependencies(),
                "phase": run.phase if run else "idle", "run_id": run.id if run else None,
                "revision": run.revision if run else 0, "region": run.region if run else "cn",
                "expires_at": run.expires_at if run and run.phase in _ACTIVE else None,
                "qr_available": bool(run and run.qr is not None and run.phase == "awaiting_qr"),
                "devices": [dict(item) for item in run.public_devices] if run else [],
                "results": [dict(item) for item in run.results] if run else [],
                "error": run.error if run else None}

    def _worker(self, run: _Run, target: Callable[[], None]) -> None:
        try:
            target()
        except Exception as error:
            with self.lock:
                if self._current(run):
                    run.phase = "expired" if isinstance(error, _Expired) else "error"
                    run.error = (str(error) if type(error) in (CloudCredentialError, _Expired)
                                 else "雲端查詢未完成；未儲存憑證，請重新登入後重試。")
                    run.public_devices.clear()
                    provider = self._clear_private(run)
                else:
                    provider = None
            self._close_provider(provider)
        finally:
            with self.lock:
                self._threads.discard(threading.current_thread())

    def _capacity(self) -> None:
        self._threads = {thread for thread in self._threads if thread.is_alive()}
        if len(self._threads) >= 2:
            raise CloudCredentialError("上一個雲端連線正在結束；請稍候再試。")

    def _spawn(self, run: _Run, target: Callable[[], None]) -> None:
        self._capacity()
        thread = threading.Thread(target=self._worker, args=(run, target), daemon=True,
                                  name="linptech-cloud-credentials")
        self._threads.add(thread)
        thread.start()

    def start(self, region: str = "cn") -> dict[str, Any]:
        if region not in REGIONS:
            raise CloudCredentialError("請選擇有效的米家地區。")
        self.snapshot()
        with self.lock:
            if self._closed:
                raise CloudCredentialError("雲端憑證服務已關閉。")
            if not self._dependencies():
                raise CloudCredentialError("雲端取憑證需要選用的 cloud 套件；請先安裝 linptech-ble[cloud]。")
            if self._run and self._run.phase in _ACTIVE:
                raise CloudCredentialError("已有雲端登入進行中；請先取消或完成。")
            self._capacity()
            now = self.clock()
            run = _Run(secrets.token_urlsafe(18), region, now + _RUN_SECONDS, now + _RUN_SECONDS)
            self._run = run
            try:
                self._timer(run)
                self._spawn(run, lambda: self._login(run))
            except Exception:
                run.phase, run.error = "error", "無法啟動雲端工作階段；請稍後重試。"
                self._close_provider(self._clear_private(run))
                raise CloudCredentialError(run.error) from None
        return self.snapshot()

    def _login(self, run: _Run) -> None:
        provider = self.provider_factory(run.region, run.cancelled)
        with self.lock:
            if not self._current(run):
                self._close_provider(provider)
                return
            run.provider = provider
        image, expires = provider.prepare()
        if (not isinstance(image, bytes) or not image.startswith(_PNG) or len(image) > _MAX_QR
                or type(expires) not in (int, float) or not math.isfinite(expires)):
            raise CloudCredentialError("無法準備有效的登入 QR；請重試。")
        with self.lock:
            if not self._current(run):
                return
            run.expires_at = min(expires, run.hard_deadline)
            if run.expires_at <= self.clock():
                raise _Expired("登入 QR 已過期；請重新產生。")
            run.qr, run.revision, run.phase = image, run.revision + 1, "awaiting_qr"
            self._timer(run)
        provider.poll_login(run.expires_at)
        with self.lock:
            if not self._current(run):
                return
            run.qr, run.phase = None, "querying"
            run.expires_at = run.hard_deadline
            self._timer(run)
        records = provider.list_devices()
        if not isinstance(records, list) or len(records) > _MAX_DEVICES:
            raise CloudCredentialError("米家裝置清單超過安全限制；未變更任何裝置。")
        configured = {item["address"]: item for item in self.inventory.snapshot()["devices"]}
        private: dict[str, dict[str, Any]] = {}
        public: list[dict[str, Any]] = []
        addresses: dict[str, dict[str, Any]] = {}
        for item in records:
            if not isinstance(item, dict) or item.get("miot_model") not in MODEL_ALIASES:
                continue
            address = normalize_address(item.get("address"))
            model = MODEL_ALIASES[item["miot_model"]]
            if item.get("model") != model:
                raise CloudCredentialError("雲端型號與感測器資料不符；未變更任何裝置。")
            old = addresses.get(address)
            if old:
                if any(old.get(key) != item.get(key) for key in ("model", "miot_model", "did", "token")):
                    raise CloudCredentialError("同一感測器在雲端有衝突資料；未變更任何裝置。")
                continue
            addresses[address] = item
            existing = configured.get(address)
            identifier = secrets.token_urlsafe(18)
            private[identifier] = {**item, "address": address, "model": model}
            public.append({"id": identifier, "address": address, "model": model,
                "name": existing["name"] if existing else _name(item.get("name"), model),
                "configured": existing is not None, "key_available": None,
                "token_available": bool(_credential(item.get("token"), 24)),
                "key_configured": bool(existing and existing.get("key_configured")),
                "token_configured": bool(existing and existing.get("token_configured"))})
        with self.lock:
            if not self._current(run):
                return
            run.devices, run.public_devices, run.phase = private, public, "selecting"
            run.expires_at = min(run.hard_deadline, self.clock() + _SELECT_SECONDS)
            self._timer(run)

    def _require(self, run_id: str) -> _Run:
        if not isinstance(run_id, str) or self._run is None or self._run.id != run_id:
            raise CloudCredentialError("此雲端工作階段已失效；請重新整理。")
        return self._run

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            run = self._require(run_id)
            # The commit shares this lock; cancellation either wins before
            # commit or observes a completed transaction afterwards.
            provider = self._clear_private(run)
            run.phase, run.error, run.expires_at = "idle", None, 0
            run.public_devices.clear()
        self._close_provider(provider)
        return self.snapshot()

    def qr_image(self, run_id: str, revision: int) -> bytes | None:
        self.snapshot()
        with self.lock:
            run = self._run
            if (run is None or run.id != run_id or type(revision) is not int or revision != run.revision
                    or run.phase != "awaiting_qr"):
                return None
            return run.qr

    def save(self, run_id: str, device_ids: list[str]) -> dict[str, Any]:
        self.snapshot()
        with self.lock:
            run = self._require(run_id)
            if run.phase != "selecting":
                raise CloudCredentialError("請先完成登入並選取要儲存的感測器。")
            if (not isinstance(device_ids, list) or not device_ids or len(device_ids) > _MAX_DEVICES
                    or any(not isinstance(item, str) for item in device_ids)
                    or len(set(device_ids)) != len(device_ids)
                    or any(item not in run.devices for item in device_ids)):
                raise CloudCredentialError("請選取此工作階段中的有效感測器；不可重複。")
            selected = [dict(run.devices[item]) for item in device_ids]
            self._capacity()
            expected = self.inventory.credential_snapshot([item["address"] for item in selected])
            configured = {item["address"] for item in self.inventory.snapshot()["devices"]}
            run.phase, run.expires_at = "saving", run.hard_deadline
            try:
                self._timer(run)
                self._spawn(run, lambda: self._save(run, selected, expected, configured))
            except Exception:
                run.phase, run.error = "error", "無法啟動憑證儲存；未變更任何裝置，請重新登入。"
                self._close_provider(self._clear_private(run))
                run.public_devices.clear()
                raise CloudCredentialError(run.error) from None
        return self.snapshot()

    def _save(self, run: _Run, selected: list[dict[str, Any]], expected: Any, configured: set[str]) -> None:
        provider = run.provider
        rows = []
        for item in selected:
            if run.cancelled.is_set():
                return
            credentials = provider.fetch_credentials(item)
            if not isinstance(credentials, dict):
                raise CloudCredentialError("雲端憑證回覆格式不符；未變更任何裝置。")
            row = {key: item[key] for key in ("address", "model", "miot_model", "name")}
            for key, length in (("bindkey", 32), ("auth_token", 24)):
                value = credentials.get(key)
                if value is not None and _credential(value, length) is None:
                    raise CloudCredentialError("雲端憑證長度或格式不符；未變更任何裝置。")
                row[key] = _credential(value, length)
            if row["address"] not in configured and not (row["bindkey"] or row["auth_token"]):
                raise CloudCredentialError("新感測器沒有取得可用的 bindkey 或直連 token；未變更任何裝置。")
            rows.append(row)
        with self.lock:
            if not self._current(run):
                return
            try:
                # Another GUI mutation can hold this lock during native BLE
                # cleanup. Recheck expiry after acquiring it, before persistence.
                with getattr(self.inventory, "lock", nullcontext()):
                    if self.clock() >= run.hard_deadline:
                        raise _Expired("雲端工作階段已過期；未儲存憑證。")
                    result = self.inventory.merge_credentials(rows, expected=expected)
            except DirectError:
                raise CloudCredentialError("無法套用憑證；請先中斷所選裝置直連並完成待還原校正。若本機設定已變更，請重新登入選取。") from None
            # Only allowlisted summary fields can escape a third-party inventory.
            retrieved = {row["address"]: row for row in rows}
            run.results = [{**{key: item.get(key) for key in ("address", "model", "name", "key_configured", "token_configured")},
                "bindkey_retrieved": bool(retrieved[item["address"]].get("bindkey")),
                "token_retrieved": bool(retrieved[item["address"]].get("auth_token"))}
                for item in result["devices"]]
            run.phase, run.error = "complete", None
            provider = self._clear_private(run)
            run.public_devices.clear()
        self._close_provider(provider)

    def close(self) -> None:
        with self.lock:
            self._closed = True
            provider = self._clear_private(self._run) if self._run else None
            if self._run:
                self._run.phase = "idle"
                self._run.public_devices.clear()
            threads = list(self._threads)
        self._close_provider(provider)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=.1)
