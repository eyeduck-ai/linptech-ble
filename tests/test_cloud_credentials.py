"""Synthetic cloud sessions only; never log in or query a real account."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import threading
import time

import pytest

from linptech_ble.cloud_credentials import (
    CloudCredentialError, CloudCredentialManager, XiaomiCloudProvider,
    _Cancelled, _json, _rc4, _validated_url,
)
from linptech_ble.standard_auth import DirectError

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-qr"
KEY, TOKEN = "11" * 16, "22" * 12
MAC1, MAC2 = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
SECRET_DID = "private-did-not-for-browser"
ROW = {"address": MAC1, "model": "KS2BB", "miot_model": "linp.sensor_ht.ks2bb",
       "name": "Cloud name", "did": SECRET_DID, "token": TOKEN}


def wait_phase(manager, phases, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = manager.snapshot()
        if status["phase"] in phases:
            return status
        time.sleep(.005)
    raise AssertionError(f"cloud phase was {manager.snapshot()['phase']}")


class Inventory:
    def __init__(self, *, configured=True):
        self.items = {MAC1: {"address": MAC1, "model": "KS2BB", "name": "Local name",
            "key_configured": True, "token_configured": True}} if configured else {}
        self.calls = []
        self.revision = 0
        self.failure = None

    def snapshot(self):
        return {"devices": copy.deepcopy(list(self.items.values()))}

    def credential_snapshot(self, addresses):
        self.calls.append(("snapshot", list(addresses)))
        return self.revision

    def merge_credentials(self, rows, *, expected):
        if self.failure:
            raise self.failure
        if expected != self.revision:
            raise DirectError("private concurrent details must not escape")
        self.calls.append(("merge", copy.deepcopy(rows)))
        result = []
        for row in rows:
            previous = self.items.get(row["address"], {})
            item = {"address": row["address"], "model": row["model"],
                "name": previous.get("name", row["name"]),
                "key_configured": bool(row.get("bindkey") or previous.get("key_configured")),
                "token_configured": bool(row.get("auth_token") or previous.get("token_configured"))}
            self.items[row["address"]] = item
            result.append(item)
        return {"devices": result}


class Provider:
    def __init__(self, *, records=None, credentials=None, login_gate=None, fetch_gate=None, clock=time.time):
        self.records = copy.deepcopy(records if records is not None else [ROW])
        self.credentials = credentials if credentials is not None else {"bindkey": KEY, "auth_token": TOKEN}
        self.login_gate, self.fetch_gate, self.clock = login_gate, fetch_gate, clock
        self.fetched = []
        self.closed = False
        self.cancelled = None

    def prepare(self):
        return PNG, self.clock() + 180

    def poll_login(self, deadline):
        while self.login_gate is not None and not self.login_gate.wait(.01):
            if self.cancelled.is_set():
                raise _Cancelled("cancelled")

    def list_devices(self):
        return self.records

    def fetch_credentials(self, item):
        self.fetched.append(item["address"])
        if self.fetch_gate:
            self.fetch_gate.wait(2)
        return self.credentials

    def close(self):
        self.closed = True
        self.records.clear()


def manager_for(provider, inventory=None, **kwargs):
    def factory(region, cancelled):
        provider.cancelled = cancelled
        return provider
    return CloudCredentialManager(inventory or Inventory(), provider_factory=factory, **kwargs)


def test_manager_is_lazy_and_snapshot_contains_no_session_or_device_secrets():
    provider = Provider()
    manager = manager_for(provider)
    try:
        assert manager.snapshot()["phase"] == "idle"
        assert provider.cancelled is None
        run = manager.start("cn")
        status = wait_phase(manager, {"selecting"})
        public = json.dumps(status)
        assert all(secret not in public for secret in (KEY, TOKEN, SECRET_DID, "beaconkey", "serviceToken"))
        assert status["devices"][0]["name"] == "Local name"
        assert status["devices"][0]["id"] != MAC1
        assert provider.fetched == []
        assert status["run_id"] == run["run_id"]
    finally:
        manager.close()
    assert provider.closed and not manager._run.devices and manager._run.provider is None


def test_selected_save_fetches_only_selected_device_and_destroys_session():
    second = {**ROW, "address": MAC2, "name": "Second", "did": "different-private-did"}
    provider = Provider(records=[ROW, second])
    inventory = Inventory()
    manager = manager_for(provider, inventory)
    try:
        manager.start("tw")
        before = wait_phase(manager, {"selecting"})
        manager.save(before["run_id"], [before["devices"][0]["id"]])
        complete = wait_phase(manager, {"complete"})
        assert provider.fetched == [MAC1]
        assert inventory.calls[0] == ("snapshot", [MAC1])
        assert inventory.calls[1][0] == "merge"
        assert MAC2 not in inventory.items
        assert complete["results"] == [{"address": MAC1, "model": "KS2BB", "name": "Local name",
            "key_configured": True, "token_configured": True, "bindkey_retrieved": True, "token_retrieved": True}]
        assert complete["devices"] == [] and not complete["qr_available"]
        assert manager._run.devices == {} and manager._run.provider is None and provider.closed
        assert KEY not in json.dumps(complete) and TOKEN not in json.dumps(complete)
    finally:
        manager.close()


@pytest.mark.parametrize("device_ids", [[], ["unknown"], [123], "not-list", None])
def test_invalid_selection_never_fetches_or_writes(device_ids):
    provider = Provider()
    manager = manager_for(provider)
    try:
        manager.start()
        status = wait_phase(manager, {"selecting"})
        with pytest.raises(CloudCredentialError):
            manager.save(status["run_id"], device_ids)
        assert provider.fetched == [] and manager.snapshot()["phase"] == "selecting"
        identifier = status["devices"][0]["id"]
        with pytest.raises(CloudCredentialError):
            manager.save(status["run_id"], [identifier, identifier])
        with pytest.raises(CloudCredentialError):
            manager.save("old-run", [identifier])
    finally:
        manager.close()


def test_qr_is_current_run_revision_only_and_cancel_destroys_session():
    provider = Provider(login_gate=threading.Event())
    manager = manager_for(provider)
    try:
        manager.start()
        status = wait_phase(manager, {"awaiting_qr"})
        assert manager.qr_image(status["run_id"], status["revision"]) == PNG
        assert manager.qr_image("old", status["revision"]) is None
        assert manager.qr_image(status["run_id"], status["revision"] + 1) is None
        assert manager.qr_image(status["run_id"], True) is None
        manager.cancel(status["run_id"])
        assert manager.snapshot()["phase"] == "idle"
        assert manager.qr_image(status["run_id"], status["revision"]) is None
        assert provider.closed and manager._run.provider is None
        with pytest.raises(CloudCredentialError):
            manager.save(status["run_id"], ["old"])
    finally:
        manager.close()


def test_cancel_during_fetch_prevents_late_commit():
    gate = threading.Event()
    provider = Provider(fetch_gate=gate)
    inventory = Inventory()
    manager = manager_for(provider, inventory)
    try:
        manager.start()
        status = wait_phase(manager, {"selecting"})
        manager.save(status["run_id"], [status["devices"][0]["id"]])
        end = time.monotonic() + 2
        while not provider.fetched and time.monotonic() < end:
            time.sleep(.005)
        manager.cancel(status["run_id"])
        gate.set()
        for thread in list(manager._threads):
            thread.join(1)
        assert manager.snapshot()["phase"] == "idle"
        assert all(call[0] != "merge" for call in inventory.calls)
    finally:
        gate.set()
        manager.close()


def test_expiry_clears_login_and_pending_qr_without_a_save():
    clock = [100.0]
    provider = Provider(login_gate=threading.Event(), clock=lambda: clock[0])
    manager = manager_for(provider, clock=lambda: clock[0])
    try:
        manager.start()
        status = wait_phase(manager, {"awaiting_qr"})
        clock[0] = 281
        assert manager.snapshot()["phase"] == "expired"
        assert manager.qr_image(status["run_id"], status["revision"]) is None
        assert provider.closed and manager._run.provider is None and not manager._run.devices
    finally:
        manager.close()


def test_missing_cloud_credentials_preserve_existing_and_report_not_retrieved():
    provider = Provider(credentials={"bindkey": None, "auth_token": None})
    manager = manager_for(provider)
    try:
        manager.start()
        status = wait_phase(manager, {"selecting"})
        manager.save(status["run_id"], [status["devices"][0]["id"]])
        complete = wait_phase(manager, {"complete"})
        assert complete["results"][0]["key_configured"] is True
        assert complete["results"][0]["token_configured"] is True
        assert complete["results"][0]["bindkey_retrieved"] is False
        assert complete["results"][0]["token_retrieved"] is False
    finally:
        manager.close()


def test_new_device_without_any_usable_credential_fails_atomically():
    provider = Provider(credentials={"bindkey": None, "auth_token": None})
    inventory = Inventory(configured=False)
    manager = manager_for(provider, inventory)
    try:
        manager.start()
        status = wait_phase(manager, {"selecting"})
        manager.save(status["run_id"], [status["devices"][0]["id"]])
        status = wait_phase(manager, {"error"})
        assert "沒有取得可用" in status["error"] and inventory.items == {}
        assert not manager._run.devices and provider.closed
    finally:
        manager.close()


@pytest.mark.parametrize("exception", [RuntimeError(KEY + TOKEN + SECRET_DID), DirectError(KEY + TOKEN)])
def test_save_errors_are_redacted_and_never_return_upstream_details(exception):
    inventory = Inventory()
    inventory.failure = exception
    provider = Provider()
    manager = manager_for(provider, inventory)
    try:
        manager.start()
        status = wait_phase(manager, {"selecting"})
        manager.save(status["run_id"], [status["devices"][0]["id"]])
        status = wait_phase(manager, {"error"})
        assert all(secret not in json.dumps(status) for secret in (KEY, TOKEN, SECRET_DID))
        assert not manager._run.devices and provider.closed
    finally:
        manager.close()


def test_concurrent_configuration_change_prevents_commit():
    gate = threading.Event()
    inventory = Inventory()
    provider = Provider(fetch_gate=gate)
    manager = manager_for(provider, inventory)
    try:
        manager.start()
        status = wait_phase(manager, {"selecting"})
        manager.save(status["run_id"], [status["devices"][0]["id"]])
        inventory.revision += 1
        gate.set()
        assert "設定已變更" in wait_phase(manager, {"error"})["error"]
        assert all(call[0] != "merge" for call in inventory.calls)
    finally:
        gate.set()
        manager.close()


def test_worker_capacity_rejection_does_not_create_phantom_run_or_saving_phase():
    provider = Provider()
    manager = manager_for(provider)
    try:
        manager._threads = {SimpleNamespaceThread(), SimpleNamespaceThread()}
        with pytest.raises(CloudCredentialError, match="正在結束"):
            manager.start()
        assert manager._run is None
        manager._threads.clear()
        manager.start()
        status = wait_phase(manager, {"selecting"})
        manager._threads = {SimpleNamespaceThread(), SimpleNamespaceThread()}
        with pytest.raises(CloudCredentialError, match="正在結束"):
            manager.save(status["run_id"], [status["devices"][0]["id"]])
        assert manager.snapshot()["phase"] == "selecting"
    finally:
        manager._threads.clear()
        manager.close()


class SimpleNamespaceThread:
    def is_alive(self):
        return True


@pytest.mark.parametrize("stage", ["timer", "worker"])
def test_start_resource_failure_cannot_leave_an_active_phantom_session(monkeypatch, stage):
    provider = Provider()
    manager = manager_for(provider)

    def fail(*_):
        raise RuntimeError(KEY)

    if stage == "timer":
        monkeypatch.setattr("linptech_ble.cloud_credentials.threading.Timer.start", fail)
    else:
        monkeypatch.setattr(manager, "_spawn", fail)
    try:
        with pytest.raises(CloudCredentialError):
            manager.start()
        status = manager.snapshot()
        assert status["phase"] == "error" and KEY not in json.dumps(status)
        assert not manager._run.devices and manager._run.timer is None
        assert provider.cancelled is None
    finally:
        manager.close()


def test_save_worker_resource_failure_destroys_login_without_committing(monkeypatch):
    provider = Provider()
    manager = manager_for(provider)
    try:
        manager.start()
        status = wait_phase(manager, {"selecting"})

        def fail(*_):
            raise RuntimeError(KEY)

        monkeypatch.setattr(manager, "_spawn", fail)
        with pytest.raises(CloudCredentialError):
            manager.save(status["run_id"], [status["devices"][0]["id"]])
        status = manager.snapshot()
        assert status["phase"] == "error" and KEY not in json.dumps(status)
        assert provider.closed and manager._run.provider is None and manager._run.timer is None
        assert provider.fetched == []
    finally:
        manager.close()


def test_session_expiry_is_checked_after_waiting_for_inventory_lock():
    clock = [100.0]
    fetch_gate, attempted = threading.Event(), threading.Event()
    inventory = Inventory()
    underlying = threading.RLock()

    class MarkedLock:
        def __enter__(self):
            attempted.set()
            underlying.acquire()

        def __exit__(self, *_):
            underlying.release()

    inventory.lock = MarkedLock()
    provider = Provider(fetch_gate=fetch_gate, clock=lambda: clock[0])
    manager = manager_for(provider, inventory, clock=lambda: clock[0])
    try:
        manager.start()
        status = wait_phase(manager, {"selecting"})
        underlying.acquire()
        try:
            manager.save(status["run_id"], [status["devices"][0]["id"]])
            fetch_gate.set()
            assert attempted.wait(2)
            clock[0] = 701
        finally:
            underlying.release()
        status = wait_phase(manager, {"expired"})
        assert all(call[0] != "merge" for call in inventory.calls)
        assert provider.closed and manager._run.provider is None
    finally:
        fetch_gate.set()
        manager.close()


@pytest.mark.parametrize("region", ["all", "", "CN", None, []])
def test_invalid_region_is_rejected_before_provider_creation(region):
    provider = Provider()
    manager = manager_for(provider)
    try:
        with pytest.raises(CloudCredentialError):
            manager.start(region)
        assert provider.cancelled is None
    finally:
        manager.close()


@pytest.mark.parametrize("url", ["http://account.xiaomi.com/a", "https://evil.test/", "https://mi.com.evil.test/",
    "https://user:password@account.xiaomi.com/", "https://account.xiaomi.com:444/", "https://account.xiaomi.com/#secret",
    "https://127.0.0.1/", "file:///etc/passwd", None])
def test_cloud_urls_reject_insecure_or_untrusted_destinations(url):
    with pytest.raises(CloudCredentialError):
        _validated_url(url)


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b"\xff", b'{"broken":'])
def test_bad_json_never_echoes_remote_data(body):
    with pytest.raises(CloudCredentialError) as error:
        _json(body)
    assert "not-json" not in str(error.value)


class Response:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 1
        for index in range(len(self.body)):
            yield self.body[index:index + 1]

    def close(self):
        self.closed = True


class Cookies(list):
    pass


class HTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = Cookies()
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def test_provider_qr_preparation_and_cancellation_are_bounded_and_no_cache():
    http = HTTP([Response(json.dumps({"timeout": 180, "qr": "https://account.xiaomi.com/qr", "lp": "https://account.xiaomi.com/lp"}).encode()), Response(PNG)])
    event = threading.Event()
    provider = XiaomiCloudProvider("cn", event, http=http)
    assert provider.prepare()[0] == PNG
    assert not http.trust_env
    assert all(call[2]["allow_redirects"] is False and call[2]["stream"] for call in http.calls)
    assert all(0 < max(call[2]["timeout"]) <= 8 for call in http.calls)
    event.set()
    with pytest.raises(_Cancelled):
        provider.poll_login(time.time() + 180)
    provider.close()
    assert http.closed and not http.cookies


def test_provider_http_errors_are_sanitized_and_response_always_closed():
    bad = Response(KEY.encode(), status=403)
    http = HTTP([bad])
    provider = XiaomiCloudProvider("cn", threading.Event(), http=http)
    with pytest.raises(CloudCredentialError) as error:
        provider.prepare()
    assert KEY not in str(error.value) and bad.closed
    provider.close()


@pytest.mark.parametrize("response", [Response(b"1234", headers={"Content-Length": "999999999"}),
    Response(b"1234", headers={"Content-Length": "bad"}), Response(b"1234")])
def test_provider_enforces_declared_and_streamed_body_limits(response):
    provider = XiaomiCloudProvider("cn", threading.Event(), http=HTTP([response]))
    with pytest.raises(CloudCredentialError, match="大小限制"):
        provider._request("GET", "https://account.xiaomi.com/qr", max_bytes=3)
    assert response.closed
    provider.close()


def test_provider_validates_redirect_before_following_it():
    response = Response(status=302, headers={"Location": "https://evil.test/steal"})
    http = HTTP([response])
    provider = XiaomiCloudProvider("cn", threading.Event(), http=http)
    with pytest.raises(CloudCredentialError, match="不允許"):
        provider.prepare()
    assert len(http.calls) == 1 and response.closed
    provider.close()


def reference_rc4(key, value):
    # Independent KSA/PRGA instead of the implementation's cryptography cipher.
    state, j = list(range(256)), 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    result, i, j = bytearray(), 0, 0
    for offset, value_byte in enumerate(b"\0" * 1024 + value):
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        output = value_byte ^ state[(state[i] + state[j]) % 256]
        if offset >= 1024:
            result.append(output)
    return bytes(result)


def test_rc4_drop1024_agrees_with_independent_reference():
    assert _rc4(bytes(range(32)), b"synthetic cloud response") == reference_rc4(bytes(range(32)), b"synthetic cloud response")


def test_encrypted_api_uses_form_body_and_independently_verified_signature(monkeypatch):
    nonce_prefix = bytes(range(8))
    security = bytes(range(16))
    monkeypatch.setattr("linptech_ble.cloud_credentials.secrets.token_bytes", lambda length: nonce_prefix)
    nonce = nonce_prefix + int(120 // 60).to_bytes(4, "big")
    signed = hashlib.sha256(security + nonce).digest()
    expected_response = {"code": 0, "result": {"beaconkey": KEY}}
    http = HTTP([Response(base64.b64encode(reference_rc4(signed, json.dumps(expected_response).encode())))])
    provider = XiaomiCloudProvider("tw", threading.Event(), http=http, clock=lambda: 120)
    provider._security, provider._service_token, provider._user_id = security, "private-service-token", "123"
    try:
        assert provider._api("/v2/device/blt_get_beaconkey", {"did": SECRET_DID, "pdid": 1}) == expected_response
        method, url, request = http.calls[0]
        assert method == "POST" and url == "https://tw.api.io.mi.com/app/v2/device/blt_get_beaconkey"
        assert "params" not in request
        fields = request["data"]
        plain = reference_rc4(signed, base64.b64decode(fields["data"]))
        assert json.loads(plain) == {"did": SECRET_DID, "pdid": 1}
        signed64 = base64.b64encode(signed).decode()
        signature = "&".join(["POST", "/v2/device/blt_get_beaconkey", "data=" + fields["data"],
            "rc4_hash__=" + fields["rc4_hash__"], signed64])
        assert fields["signature"] == base64.b64encode(hashlib.sha1(signature.encode()).digest()).decode()
        assert fields["_nonce"] == base64.b64encode(nonce).decode()
        assert request["cookies"]["serviceToken"] == "private-service-token"
    finally:
        provider.close()
    assert provider._security is provider._service_token is provider._user_id is None


def test_provider_device_list_filters_models_and_uses_numeric_home_fields():
    provider = XiaomiCloudProvider("cn", threading.Event(), http=HTTP([]))
    provider._user_id = "123"
    calls = []

    def api(path, payload):
        calls.append((path, payload))
        if path.endswith("gethome"):
            return {"code": 0, "result": {"homelist": [{"id": "456"}]}}
        if path.endswith("get_device_cnt"):
            return {"code": 0, "result": {"share": {"share_family": []}}}
        return {"code": 0, "result": {"device_info": [
            {"mac": MAC1, "model": ROW["miot_model"], "name": "Climate", "did": SECRET_DID, "token": TOKEN},
            {"mac": MAC2, "model": "unrelated.device", "did": "do-not-import", "token": KEY}]}}

    provider._api = api
    try:
        rows = provider.list_devices()
        assert len(rows) == 1 and rows[0]["address"] == MAC1
        assert calls[-1][1]["home_owner"] == 123 and calls[-1][1]["home_id"] == 456
        assert type(calls[-1][1]["home_owner"]) is int
    finally:
        provider.close()
