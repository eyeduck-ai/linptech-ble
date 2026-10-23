"""Dashboard management behavior using synthetic data and a small Node DOM fake.

No browser, network, credentials or BLE device is used. Test the actual request
and drawer functions rather than duplicating their implementation in Python.
"""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest


WEB = Path(__file__).parents[1] / "linptech_ble" / "web"
ADDRESS = "AA:BB:CC:DD:EE:01"
OTHER = "AA:BB:CC:DD:EE:02"
NOW = "2026-10-23T12:05:00Z"
SEEN = "2026-10-23T12:04:59Z"


@pytest.fixture(scope="module")
def javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; install it for dashboard management behavior tests")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const boundary = source.indexOf('function renderEs5Observations(');
if (boundary < 0) throw new Error('Missing management function boundary');
class Node {
  constructor(id = '') {
    Object.assign(this, { id, value: '', type: 'text', checked: false, disabled: false,
      hidden: false, readOnly: false, textContent: '', dataset: {}, children: [],
      parent: null, open: false, showCount: 0, focusCount: 0, scrollTop: 0 });
  }
  append(...nodes) {
    for (const child of nodes) {
      if (child.parent) child.parent.children = child.parent.children.filter(n => n !== child);
      this.children.push(child); child.parent = this;
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = []; this.append(...nodes);
  }
  showModal() { this.open = true; this.showCount++; }
  close() { this.open = false; }
  focus() { this.focusCount++; }
  querySelectorAll() { return this.children; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); this.parent = null; }
}
const rootNodes = new Map();
const byId = id => { if (!rootNodes.has(id)) rootNodes.set(id, new Node(id)); return rootNodes.get(id); };
const fields = new Map();
const field = id => { if (!fields.has(id)) fields.set(id, new Node(id)); return fields.get(id); };
const element = new Node('management-element');
element.querySelector = selector => field(selector.slice(1));
element.querySelectorAll = () => [...fields.values()].filter(n => n.type === 'password' || n.dataset.secret);
field('management-bindkey').type = field('management-token').type = 'password';
field('management-bindkey').dataset.secret = field('management-token').dataset.secret = 'true';
field('management-form').querySelectorAll = () => [...fields.values()].filter(n =>
  ['management-name', 'management-address', 'management-model', 'management-bindkey', 'management-token', 'management-reveal'].includes(n.id));
const calls = [], intervals = [];
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = vm.createContext({ Intl, Date, Number, Math, payload, element, field, byId, calls, Node,
  document: { getElementById: byId },
  setText: (id, value) => { byId(id).textContent = value; },
  AbortSignal: { timeout: () => ({}) },
  clearInterval: value => intervals.push(value),
  fetch: async (url, options = {}) => {
    calls.push({ url, ...options, signal: undefined });
    if (payload.failPost && options.method === 'POST') return { ok: false, json: async () => ({ error: 'Synthetic rejected write' }) };
    return { ok: true, json: async () => url === '/api/state' ? payload.state : { devices: payload.publicDevices || payload.state?.devices || [] } };
  }
});
vm.runInContext(source.slice(0, boundary), context);
vm.runInContext(`
  connected = true; latest = payload.state || { mode: 'live', devices: [], management: { enabled: true } };
  function receive(state) { latest = state; }
  const session = { element, editing: Boolean(payload.editing), address: payload.address || null,
    posting: false, loading: false, scanning: false, poll: 123 };
  managementSession = session;
`, context);
async function main() {
  let result;
  if (payload.action === 'field') {
    result = vm.runInContext('es5FieldView(payload.device, payload.state, payload.key)', context);
  } else if (payload.action === 'recover') {
    result = vm.runInContext('canDisconnect(payload.direct)', context);
  } else if (payload.action === 'fill') {
    result = vm.runInContext(`(() => {
      field('management-bindkey').value = 'typed secret'; field('management-token').value = 'typed token';
      fillManagement(session, payload.device);
      return Object.fromEntries(['management-name', 'management-address', 'management-model',
        'management-bindkey', 'management-token', 'credential-status'].map(id => [id,
          { value: field(id).value, text: field(id).textContent, disabled: field(id).disabled, readOnly: field(id).readOnly }]));
    })()`, context);
  } else if (payload.action === 'save') {
    vm.runInContext(`for (const [id, value] of Object.entries(payload.values)) field(id).value = value;`, context);
    await vm.runInContext('saveManagedDevice(session, Boolean(payload.remove))', context);
    result = vm.runInContext(`({ calls, bindkey: field('management-bindkey').value,
      token: field('management-token').value, feedback: field('management-feedback').textContent,
      posting: session.posting })`, context);
  } else if (payload.action === 'release') {
    result = vm.runInContext(`(() => {
      field('management-bindkey').value = 'newly typed secret'; field('management-token').value = 'newly typed token';
      field('management-bindkey').type = field('management-token').type = 'text';
      releaseDrawerContent();
      return { bindkey: field('management-bindkey').value, token: field('management-token').value,
        managementClosed: managementSession === null, drawerCleared: drawerTarget === null, calls };
    })()`, context);
  } else if (payload.action === 'availability') {
    result = vm.runInContext(`(() => {
      for (const id of ['management-name', 'management-address', 'management-model', 'management-bindkey', 'management-token']) field(id);
      connected = payload.online ?? true; paused = payload.paused ?? false;
      session.loading = Boolean(payload.loading); session.posting = Boolean(payload.posting);
      field('management-remove-confirm').checked = true;
      updateManagementAvailability();
      return { save: field('management-save').disabled, scan: field('discovery-start').disabled,
        remove: field('management-remove').disabled, add: byId('add-device').disabled,
        inputs: ['management-name', 'management-address', 'management-bindkey', 'management-token'].map(id => field(id).disabled),
        model: field('management-model').disabled, calls };
    })()`, context);
  } else if (payload.action === 'drawer') {
    result = vm.runInContext(`(() => {
      managementSession = null;
      const first = new Node('first-panel'), second = new Node('second-panel'), trigger = new Node('trigger');
      first.draft = 'unsaved user edit';
      advancedPanels.set(payload.state.devices[0].address, { host: first });
      advancedPanels.set(payload.state.devices[1].address, { host: second });
      openAdvanced(payload.state.devices[0].address, trigger);
      const initialFirst = byId('drawer-content').children[0] === first;
      openAdvanced(payload.state.devices[1].address, trigger);
      const firstStaged = first.parent === byId('panel-staging');
      const secondMounted = byId('drawer-content').children[0] === second;
      openAdvanced(payload.state.devices[0].address, trigger);
      return { initialFirst, firstStaged, secondMounted,
        firstRestored: byId('drawer-content').children[0] === first,
        draft: first.draft, showCount: byId('device-drawer').showCount,
        title: byId('drawer-title').textContent, selected: drawerTarget, calls };
    })()`, context);
  } else throw new Error('Unknown test action');
  process.stdout.write(JSON.stringify(result));
}
main().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
"""

    def run(payload):
        completed = subprocess.run([node, "-e", script, str(WEB / "app.js")],
                                   input=json.dumps(payload), text=True, encoding="utf-8",
                                   capture_output=True, timeout=15, check=False)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    return run


def es5_device(**values):
    return {"address": ADDRESS, "model": "ES5BB", "name": "Synthetic radar",
            "measurements": {}, "binary_sensors": {}, **values}


@pytest.mark.parametrize(("direct", "allowed"), [
    ({"status": "error", "authenticated": False}, True),
    ({"status": "ready", "authenticated": True}, True),
    ({"status": "disconnected", "authenticated": False}, False),
    (None, False),
])
def test_failed_authentication_can_disconnect_before_correcting_credentials(javascript, direct, allowed):
    assert javascript({"action": "recover", "direct": direct}) is allowed


def live_state(device=None, controller=None):
    device = device or es5_device()
    return {"mode": "live", "server_time": NOW, "stale_after": 180,
            "devices": [device], "direct_devices": {ADDRESS: controller or {}}}


@pytest.mark.parametrize("key", ["illuminance", "battery", "occupancy_close_range"])
def test_missing_es5_fields_never_become_zero_or_vacant(javascript, key):
    sensor = es5_device()
    result = javascript({"action": "field", "device": sensor, "state": live_state(sensor), "key": key})
    assert "entry" not in result
    assert result["missing"] == "本輪未上報"
    assert result["detail"]


def test_broadcast_lux_remains_available_when_the_gatt_property_is_unread(javascript):
    report = {"value": 78, "source": "advertisement", "updated_at": SEEN, "unit": "lx"}
    sensor = es5_device(measurements={"illuminance": report})
    state = live_state(sensor, {"authenticated": False, "properties": {"illuminance": {"status": "unread", "value": None}}})
    result = javascript({"action": "field", "device": sensor, "state": state, "key": "illuminance"})
    assert result["entry"] == report


@pytest.mark.parametrize(("key", "collection", "value"), [
    ("illuminance", "measurements", 0), ("battery", "measurements", 0),
    ("occupancy_close_range", "binary_sensors", False),
])
def test_reported_zero_and_false_are_real_values(javascript, key, collection, value):
    report = {"value": value, "updated_at": SEEN, "source": "advertisement"}
    sensor = es5_device(**{collection: {key: report}})
    result = javascript({"action": "field", "device": sensor, "state": live_state(sensor), "key": key})
    assert result["entry"] == report


@pytest.mark.parametrize(("authenticated", "status", "value", "disabled"), [
    (True, "read", False, True), (True, "cached", False, False),
    (False, "read", False, False), (True, "unread", False, False),
    (True, "unavailable", False, False), (True, "read", True, False),
    (True, "read", 0, False),
])
def test_near_range_disabled_requires_current_authenticated_boolean_read(javascript, authenticated, status, value, disabled):
    sensor = es5_device()
    controller = {"authenticated": authenticated, "properties": {"interest_enable": {
        "status": status, "value": value, "updated_at": SEEN}}}
    result = javascript({"action": "field", "device": sensor,
                         "state": live_state(sensor, controller), "key": "occupancy_close_range"})
    assert (result["missing"] == "未啟用") is disabled


def test_other_es5_controller_cannot_disable_this_devices_near_range(javascript):
    sensor = es5_device()
    state = live_state(sensor)
    state["direct_devices"][OTHER] = {"authenticated": True, "properties": {
        "interest_enable": {"status": "read", "value": False}}}
    result = javascript({"action": "field", "device": sensor, "state": state, "key": "occupancy_close_range"})
    assert result["missing"] == "本輪未上報"


def management_values(**values):
    return {"management-address": ADDRESS, "management-model": "KS2BB",
            "management-name": "Bedroom", "management-bindkey": "", "management-token": "", **values}


def test_existing_credentials_are_not_refilled_and_only_saved_status_is_shown(javascript):
    sensor = {"address": ADDRESS, "model": "KS2BB", "name": "Bedroom", "key_configured": True,
              "token_configured": True, "bindkey": "never expose this", "auth_token": "never expose this either"}
    result = javascript({"action": "fill", "device": sensor, "state": live_state(sensor)})
    assert result["management-bindkey"]["value"] == result["management-token"]["value"] == ""
    assert "已保存" in result["credential-status"]["text"]
    assert "never expose" not in json.dumps(result)
    assert result["management-model"]["disabled"] is True
    assert result["management-address"]["readOnly"] is True


def test_rename_omits_blank_credentials_so_saved_values_are_preserved(javascript):
    result = javascript({"action": "save", "editing": True,
                         "values": management_values(**{"management-name": "  Renamed bedroom  "}),
                         "state": live_state({"address": ADDRESS, "model": "KS2BB", "name": "Renamed bedroom"})})
    post = next(call for call in result["calls"] if call.get("method") == "POST")
    assert json.loads(post["body"]) == {"action": "update", "address": ADDRESS, "name": "Renamed bedroom"}
    assert post["url"] == "/api/devices"
    assert post["headers"]["X-Linptech-Control"] == "1"
    assert "?" not in post["url"]


def test_add_sends_normalized_address_and_explicit_credentials_then_clears_inputs(javascript):
    key, token = "00112233445566778899aabbccddeeff", "00112233445566778899aabb"
    result = javascript({"action": "save", "editing": False,
                         "values": management_values(**{"management-address": "aabbccddee01",
                                                          "management-bindkey": f" {key} ",
                                                          "management-token": f" {token} "}),
                         "state": live_state({"address": ADDRESS, "model": "KS2BB", "name": "Bedroom"})})
    post = next(call for call in result["calls"] if call.get("method") == "POST")
    assert json.loads(post["body"]) == {"action": "add", "address": ADDRESS, "model": "KS2BB",
                                        "name": "Bedroom", "bindkey": key, "auth_token": token}
    assert result["bindkey"] == result["token"] == ""
    assert key not in result["feedback"] and token not in result["feedback"]
    assert result["posting"] is False


def test_remove_request_does_not_send_name_model_or_typed_secrets(javascript):
    result = javascript({"action": "save", "remove": True, "editing": True,
                         "values": management_values(**{"management-bindkey": "typed-secret", "management-token": "typed-token"}),
                         "state": live_state()})
    post = next(call for call in result["calls"] if call.get("method") == "POST")
    assert json.loads(post["body"]) == {"action": "remove", "address": ADDRESS}
    assert result["bindkey"] == result["token"] == ""


def test_rejected_save_keeps_the_draft_for_correction_and_does_not_claim_success(javascript):
    result = javascript({"action": "save", "editing": True, "failPost": True,
                         "values": management_values(**{"management-bindkey": "typed-secret"}), "state": live_state()})
    assert result["bindkey"] == "typed-secret"
    assert result["feedback"] == "Synthetic rejected write"
    assert result["posting"] is False
    assert len(result["calls"]) == 1


def test_closing_management_erases_even_revealed_plaintext_secret_inputs(javascript):
    result = javascript({"action": "release", "state": live_state()})
    assert result["bindkey"] == result["token"] == ""
    assert result["managementClosed"] is True and result["drawerCleared"] is True
    assert result["calls"] == []


@pytest.mark.parametrize("conditions", [
    {"online": False}, {"paused": True}, {"loading": True}, {"posting": True},
    {"state": {"mode": "replay", "devices": []}},
    {"state": {"mode": "live", "devices": [], "management": {"enabled": False}}},
])
def test_management_controls_lock_for_offline_replay_pause_and_pending_operations(javascript, conditions):
    result = javascript({"action": "availability", "state": live_state(), **conditions})
    assert result["save"] and result["scan"] and result["remove"]
    assert all(result["inputs"])
    assert result["calls"] == []


@pytest.mark.parametrize("editing", [False, True])
def test_management_fields_unlock_when_ready_but_existing_model_stays_fixed(javascript, editing):
    result = javascript({"action": "availability", "state": live_state(), "editing": editing})
    assert not result["save"] and not result["scan"] and not result["remove"]
    assert not any(result["inputs"])
    assert result["model"] is editing
    assert result["calls"] == []


def test_switching_drawers_moves_existing_panels_and_keeps_unsaved_draft(javascript):
    first = es5_device()
    second = es5_device(address=OTHER, name="Second radar")
    result = javascript({"action": "drawer", "state": {"mode": "live", "devices": [first, second]}})
    assert all(result[key] for key in ["initialFirst", "firstStaged", "secondMounted", "firstRestored"])
    assert result["draft"] == "unsaved user edit"
    assert result["showCount"] == 1
    assert result["title"] == first["name"] and result["selected"] == ADDRESS
    assert result["calls"] == []  # Opening settings must never issue BLE/API commands.


class HtmlInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.nodes[attrs["id"]] = (tag, attrs)


def test_management_html_uses_password_inputs_and_an_accessibly_labeled_dialog():
    inventory = HtmlInventory()
    inventory.feed((WEB / "index.html").read_text(encoding="utf-8"))
    tag, drawer = inventory.nodes["device-drawer"]
    assert tag == "dialog" and drawer["aria-labelledby"] == "drawer-title"
    assert drawer["aria-describedby"] == "drawer-subtitle"
    for identity, size in [("management-bindkey", "32"), ("management-token", "24")]:
        tag, attrs = inventory.nodes[identity]
        assert tag == "input" and attrs["type"] == "password"
        assert attrs["autocomplete"] == "new-password" and attrs["maxlength"] == size
        assert not attrs.get("value")
    _, form = inventory.nodes["management-form"]
    assert form["method"] == "post" and form["autocomplete"] == "off"
    assert "disabled" in inventory.nodes["management-remove"][1]
