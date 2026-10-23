"""Model-scoped BLE Spec settings for KS2BB and HS3BB.

Only properties explicitly marked GATT read/write by the released MIoT spec
are exposed. No cloud calls, pairing, firmware updates, or vendor commands.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from .es5_direct import DirectController, now_iso
from .property_catalog import PROFILES, PROTOCOL_MODELS, SensorProperty
from .standard_auth import DirectError, PropertyUnavailable, StandardSession, encode_value, uuid16
from .transport import connected_client



class SensorDirectController(DirectController):
    """Uses the established session engine, with a separate per-model catalog."""

    def __init__(self, device, **kwargs):
        if device is None or getattr(device, "model", None) not in PROFILES:
            raise DirectError("此控制器僅支援 MS1BB、KS2BB 與 HS3BB。")
        super().__init__(device, **kwargs)
        self.catalog = {prop.name: prop for prop in PROFILES.get(device.model, ())}
        self.pairs = {(prop.siid, prop.piid): prop for prop in self.catalog.values()}
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
        await self._disconnect()
        self.recording_dir.mkdir(parents=True, exist_ok=True)
        path = self.recording_dir / f"{self.device.model.lower()}-direct-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.jsonl"
        self.log = path.open("x", encoding="utf-8")
        self._update(status="connecting", error=None, recording=str(path))
        self.client_context = connected_client(self.transport, self.device.address, self.device.name)
        self.client = await self.client_context.__aenter__()
        firmware = None
        if self.client.services.get_characteristic(uuid16(4)):
            with suppress(Exception):
                firmware = (await asyncio.wait_for(self.client.read_gatt_char(uuid16(4)), 3)).decode("ascii", errors="replace").rstrip("\0")
        self._update(status="authenticating", firmware=firmware)
        self.session = StandardSession(self.client, bytes.fromhex(self.device.auth_token),
            on_property=self._accept, on_frame=self._protocol_frame,
            on_transport=lambda **fields: self._diagnostic("auth_transport", **fields))
        try:
            await asyncio.wait_for(self.session.authenticate(), 35)
            self._update(authenticated=True)
            await self._refresh()
        except BaseException:
            await self._disconnect()
            raise

    def _accept(self, result):
        prop = self.pairs.get((result["siid"], result["piid"]))
        if prop is None or result["status"] != 0 or not result.get("available", True):
            return
        value = result["value"]
        code, _ = encode_value(value, prop.format)
        if code != result["type_code"]:
            raise DirectError("直連回覆型別與此型號規格不符。")
        # Unknown enumerations and out-of-range firmware values remain visible,
        # but do not enable writes. Never reinterpret unknown as false or zero.
        valid = True
        try:
            prop.validate(value)
        except DirectError:
            valid = False
        at = now_iso()
        with self.lock:
            self.state["properties"][prop.name].update(status="read" if valid else "invalid_value", value=value,
                updated_at=at, error=None if valid else "韌體值超出目前規格，保留原值並停用寫入。")
        record = {"name": prop.name, "model": self.device.model, "address": self.device.address,
                  "siid": prop.siid, "piid": prop.piid, "value": value, "updated_at": at, "source": "gatt", "authenticated": True}
        if self.log:
            import json
            self.log.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.log.flush()
        if self.on_value and valid:
            self.on_value(record)

    async def _refresh(self):
        if not self.session:
            raise DirectError("請先完成認證。")
        self._update(status="reading", error=None)
        successes = 0
        for prop in self.catalog.values():
            try:
                result = await self.session.read(prop.siid, prop.piid, subscribe=prop.notify)
                self._accept(result)
                successes += self.state["properties"][prop.name]["status"] == "read"
            except DirectError as error:
                with self.lock:
                    self.state["properties"][prop.name].update(status="unavailable" if isinstance(error, PropertyUnavailable) else "error", error=str(error))
            if self.session.error:
                raise self.session.error
        self._update(status="ready" if successes else "error", error=None if successes else "韌體未提供可用的設定讀值。")
        self._diagnostic("refresh", successful_properties=successes)

    async def _idle(self):
        if self.session and self.session.error:
            raise self.session.error

    async def _set(self, name, value):
        prop = self.catalog.get(name)
        if prop is None or not self.model_confirmed or not self.session:
            raise DirectError("尚未確認此型號的可寫設定。")
        prop.validate(value)
        if self.state["properties"][name]["status"] != "read":
            raise DirectError("此屬性沒有本次連線的有效讀值。")
        result = await self.session.set(prop.siid, prop.piid, value, prop.format)
        self._accept(result)
        self._diagnostic("setting_verified", property=name, value=value)
        self._update(error=None, status="ready")

    async def _disconnect(self):
        # These models have no ES5 stream command; never send 6.9=0 to them.
        if self.session:
            with suppress(Exception):
                await asyncio.wait_for(self.session.close(), 4)
        self.session = None
        if self.client_context:
            with suppress(Exception):
                await asyncio.wait_for(self.client_context.__aexit__(None, None, None), 5)
        self.client_context, self.client = None, None
        with self.lock:
            for prop in self.state["properties"].values():
                if prop["status"] == "read":
                    prop["status"] = "cached"
            self.state.update(status="disconnected", authenticated=False)
        if self.log:
            self.log.close()
            self.log = None
