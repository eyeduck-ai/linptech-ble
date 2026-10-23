"""Model-scoped BLE Spec settings for KS2BB and HS3BB.

Only properties explicitly marked GATT read/write by the released MIoT spec
are exposed. No cloud calls, pairing, firmware updates, or vendor commands.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from .client import LinptechClient, PropertyValue
from .es5_direct import DirectController, now_iso
from .property_catalog import PROFILES, PROTOCOL_MODELS, SensorProperty
from .standard_auth import DirectError
class SensorDirectController(DirectController):
    """Dashboard state adapter over the public, model-scoped async client."""

    def __init__(self, device, **kwargs):
        if device is None or getattr(device, "model", None) not in PROFILES:
            raise DirectError("此控制器僅支援 MS1BB、KS2BB 與 HS3BB。")
        super().__init__(device, **kwargs)
        self.catalog = {prop.name: prop for prop in PROFILES.get(device.model, ())}
        self.pairs = {(prop.siid, prop.piid): prop for prop in self.catalog.values()}
        self._observations: dict[str, PropertyValue] = {}
        self.model_confirmed = device.model != "HS3BB" or getattr(device, "miot_model", None) == PROTOCOL_MODELS[device.model]
        self.state.update(model=device.model, protocol_model=PROTOCOL_MODELS[device.model],
            model_confirmed=self.model_confirmed, supported=bool(self.catalog),
            note=("正式版規格沒有公開可寫 GATT 設定；不套用 debug v2 的門未關時間。" if device.model == "MS1BB" else
                  "本型號已以公開 client 完成兩項設定的實機寫入、回讀與還原；操作仍須核對本次韌體回覆。" if device.model == "KS2BB" else
                  "本型號已通過認證及指示燈／偵測間隔的寫入、回讀與還原；A/B 時間曾回傳規格外 0，其他欄位須逐項驗證。"),
            properties={name: prop.public() for name, prop in self.catalog.items()})
        for key in ("distance_mapping_verified", "bin_meters", "bin_count"):
            self.state.pop(key, None)
        self.state["calibration"].update(available=False, message="此型號未提供本專案的校正流程。")

    def snapshot(self):
        self._refresh_recording_state()
        with self.lock:
            return deepcopy(self.state)

    def submit(self, value):
        action = value.get("action")
        if action not in {"connect", "disconnect", "refresh", "set"}:
            raise DirectError("此型號不支援這項直連操作。")
        if action == "connect" and (not self.catalog or not self.device.auth_token):
            raise DirectError("此型號尚未提供可用設定，或缺少 12-byte Standard Auth token。")
        with self.lock:
            if action in {"set", "refresh"} and (not self.state["authenticated"] or self.state["status"] != "ready"):
                raise DirectError("請先連線並完成認證及讀取。")
            if action == "set":
                prop = self.catalog.get(value.get("property"))
                if prop is None:
                    raise DirectError("此型號沒有開放這個屬性。")
                prop.validate(value.get("value"))
                if not self.model_confirmed:
                    raise DirectError("尚未確認 HS3BB 的協定型號；請核對 linp.motion.hs1bb2 後在本機設定 miot_model。")
                if self.state["properties"][prop.name]["status"] != "read":
                    raise DirectError("必須先成功讀取本次連線的屬性，才能寫入。")
            if self.state["busy"] or self.loop is None or self.commands is None:
                raise DirectError("直連程序尚未就緒或正忙碌。")
            identity = uuid4().hex[:12]
            self.state.update(busy=True, last_operation={"id": identity, "action": action, "status": "pending", "at": now_iso()})
            self.loop.call_soon_threadsafe(self.commands.put_nowait, {**value, "id": identity})
            return identity

    async def _connect(self):
        if not self.catalog or not self.device.auth_token:
            raise DirectError("此型號尚未提供可用設定，或缺少 12-byte Standard Auth token。")
        await self._disconnect()
        self._observations.clear()
        self._update(status="connecting", error=None, firmware=None)

        def on_update(value):
            # The SDK callback worker may deliver a queued observation after a
            # newer explicit read, or after this controller replaces its client.
            if self.client is client and client.get_property(value.name) is value:
                self._accept(value)

        try:
            await self._open_recording()
            client = LinptechClient(self.device, transport=self.transport, on_update=on_update)
            self.client = client
            await client.connect()
            self._update(authenticated=True, client_diagnostics=client.diagnostics())
            await self._refresh()
        except BaseException:
            await self._disconnect()
            raise

    def _accept(self, observation: PropertyValue):
        if not isinstance(observation, PropertyValue):
            raise TypeError("The sensor controller accepts public PropertyValue observations only.")
        prop = self.catalog.get(observation.name)
        if prop is None or observation.pair != (prop.siid, prop.piid):
            return
        with self.lock:
            previous = self._observations.get(prop.name)
            if previous is observation:
                return
            if (previous and previous.updated_at and observation.updated_at
                    and observation.updated_at < previous.updated_at):
                return
            self._observations[prop.name] = observation
            self.state["properties"][prop.name].update(status=observation.status, value=observation.value,
                updated_at=observation.updated_at, error=observation.error)
        record = {"name": prop.name, "model": self.device.model, "address": self.device.address,
                  "siid": prop.siid, "piid": prop.piid, "value": observation.value,
                  "updated_at": observation.updated_at, "source": observation.source,
                  "status": observation.status, "error": observation.error, "authenticated": True}
        self._record(record)
        if self.on_value and observation.valid:
            self.on_value(record)

    async def _refresh(self):
        client = self.client
        if not client or not client.connected:
            raise DirectError("請先完成認證。")
        self._update(status="reading", error=None)
        observations = await client.refresh(tuple(self.catalog))
        if self.client is not client:
            raise DirectError("直連已變更，請重新讀取。")
        for name in observations:
            self._accept(client.get_property(name))
        with self.lock:
            successes = sum(prop["status"] == "read" for prop in self.state["properties"].values())
        self._update(status="ready" if successes else "error", error=None if successes else "韌體未提供可用的設定讀值。")
        self._update(client_diagnostics=client.diagnostics())
        self._diagnostic("refresh", successful_properties=successes)

    async def _idle(self):
        if self.client:
            self._update(client_diagnostics=self.client.diagnostics())
            if not self.client.connected:
                raise DirectError("裝置直連已中斷，請重新連線。")

    async def _set(self, name, value):
        prop = self.catalog.get(name)
        client = self.client
        if prop is None or not self.model_confirmed or not client or not client.connected:
            raise DirectError("尚未確認此型號的可寫設定。")
        prop.validate(value)
        if self.state["properties"][name]["status"] != "read":
            raise DirectError("此屬性沒有本次連線的有效讀值。")
        try:
            observed = await client.set(name, value)
        except BaseException:
            self._sync_client_property(client, name)
            raise
        if self.client is not client:
            raise DirectError("直連已變更，請重新讀取。")
        self._accept(observed)
        self._diagnostic("setting_verified", property=name, value=value)
        self._update(error=None, status="ready", client_diagnostics=client.diagnostics())

    async def _disconnect(self):
        # The public client owns model-specific cleanup; these profiles never
        # inherit ES5 stream commands. Ignore callbacks as soon as close starts.
        client, self.client = self.client, None
        try:
            if client:
                await client.aclose()
                self._update(client_diagnostics=client.diagnostics())
        finally:
            with self.lock:
                self._observations.clear()
                for prop in self.state["properties"].values():
                    if prop["status"] == "read":
                        prop["status"] = "cached"
                self.state.update(status="disconnected", authenticated=False)
            await self._close_recording()
