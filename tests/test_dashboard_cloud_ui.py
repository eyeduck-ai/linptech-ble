"""Exercise the real QR drawer functions with synthetic requests and a small DOM.

No account, credentials, browser, network or Bluetooth device is used here.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


WEB = Path(__file__).parents[1] / "linptech_ble" / "web"


@pytest.fixture(scope="module")
def javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; install it for dashboard interaction tests")
    script = r"""
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const boundary = source.indexOf('function renderEs5Observations(');
if (boundary < 0) throw new Error('Missing cloud drawer test boundary');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
class Node {
  constructor(tag = 'div') { Object.assign(this, { tag, value: '', textContent: '',
    dataset: {}, children: [], listeners: {}, disabled: false, hidden: false,
    checked: false, indeterminate: false, src: '', attributes: {} }); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(kind, callback) { this.listeners[kind] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; if (name === 'src') this.src = ''; }
  querySelectorAll(selector) {
    const all = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
    return selector === 'input[data-device-id]' ? all.filter(child => child.tag === 'input' && child.dataset.deviceId) : all;
  }
}
const fields = new Map(), roots = new Map(), calls = [], cleared = [];
const field = id => { if (!fields.has(id)) fields.set(id, new Node()); return fields.get(id); };
const byId = id => { if (!roots.has(id)) roots.set(id, new Node()); return roots.get(id); };
const element = new Node();
element.querySelector = selector => field(selector.slice(1));
element.querySelectorAll = selector => field('cloud-devices').querySelectorAll(selector);
const response = data => ({ ok: true, json: async () => data });
let resolveDeferred, fetchBehavior = async () => response(payload.response || payload.initial);
const context = vm.createContext({ Intl, Date, Number, Math, payload, element, field, byId, calls,
  document: { getElementById: byId, createElement: tag => new Node(tag) },
  clearInterval: value => cleared.push(value),
  AbortSignal: { timeout: () => ({}) },
  fetch: (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null,
      headers: options.headers || {} });
    return fetchBehavior(url, options);
  }
});
vm.runInContext(source.slice(0, boundary), context);
vm.runInContext(`
  connected = true; latest = { mode: 'live', devices: [], management: { enabled: true } };
  const session = { element, selected: new Set(), data: null, loading: false,
    posting: false, polling: false, poll: 42, requestVersion: 0 };
  cloudSession = session;
  field('cloud-region').value = 'cn';
  function receive(state) { latest = state; }
`, context);
const run = code => vm.runInContext(code, context);
const render = data => { context.nextData = data; run('renderCloudCredentials(session, nextData)'); };
const controls = () => run(`({ selected: [...session.selected], phase: session.data?.phase,
  saveDisabled: field('cloud-save').disabled, checked: field('cloud-select-all').checked,
  partial: field('cloud-select-all').indeterminate,
  qrHidden: field('cloud-qr').hidden, qrSrc: field('cloud-qr-image').src,
  count: field('cloud-selected-count').textContent,
  resultHidden: field('cloud-result').hidden,
  resultRows: field('cloud-results').children.map(row => row.textContent),
  rows: field('cloud-devices').children.map(row => ({ text: row.children.map(child => child.textContent),
    checkbox: row.children[0]?.checked })) })`);
async function main() {
  render(payload.initial);
  let result;
  if (payload.action === 'selections') {
    const checkbox = field('cloud-devices').querySelectorAll('input[data-device-id]')[0];
    checkbox.checked = true; checkbox.listeners.change();
    const before = controls();
    render(payload.next); result = { before, after: controls() };
  } else if (payload.action === 'save') {
    run('session.selected.add(payload.selected)');
    await run('cloudCommand(session, "save")');
    result = { calls, ...controls() };
  } else if (payload.action === 'close') {
    run('releaseDrawerContent()'); await Promise.resolve();
    result = { calls, cleared, qrSrc: field('cloud-qr-image').src, closed: run('cloudSession === null') };
  } else if (payload.action === 'delayed-start-close') {
    fetchBehavior = () => new Promise(resolve => { resolveDeferred = resolve; });
    const pending = run('cloudCommand(session, "start")');
    run('releaseDrawerContent()');
    fetchBehavior = async () => response({ phase: 'cancelled', run_id: payload.response.run_id });
    resolveDeferred(response(payload.response)); await pending; await Promise.resolve();
    result = { calls, closed: run('cloudSession === null'), qrSrc: field('cloud-qr-image').src };
  } else if (payload.action === 'stale-poll') {
    fetchBehavior = (_url, options) => options.method === 'POST' ? Promise.resolve(response(payload.response)) :
      new Promise(resolve => { resolveDeferred = resolve; });
    const polling = run('pollCloudCredentials(session)');
    run('session.selected.add(payload.selected)');
    await run('cloudCommand(session, "save")');
    resolveDeferred(response(payload.initial)); await polling;
    result = { calls, ...controls() };
  } else if (payload.action === 'availability') {
    run('connected = payload.online; paused = Boolean(payload.paused); updateCloudAvailability(session)');
    result = { ...controls(), startDisabled: field('cloud-start').disabled,
      regionDisabled: field('cloud-region').disabled,
      checkboxDisabled: field('cloud-devices').querySelectorAll('input[data-device-id]').map(input => input.disabled) };
  } else if (payload.action === 'qr') { const before = controls(); render(payload.next); result = { before, after: controls() }; }
  else if (payload.action === 'results') { render(payload.next); result = controls(); }
  else throw new Error('Unknown action');
  process.stdout.write(JSON.stringify(result));
}
main().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
"""

    def run(payload):
        result = subprocess.run(
            [node, "-e", script, str(WEB / "app.js")], input=json.dumps(payload),
            text=True, encoding="utf-8", capture_output=True, timeout=15, check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return run


def cloud_state(phase="selecting", run_id="synthetic-run", **values):
    return {
        "enabled": True, "dependencies_available": True, "phase": phase,
        "run_id": run_id, "region": "cn", "revision": 1,
        "devices": [
            {"id": "opaque-one", "address": "AA:BB:CC:DD:EE:01", "model": "ES5BB",
             "name": "Synthetic living room", "configured": True, "key_configured": True,
             "token_configured": True, "token_available": True},
            {"id": "opaque-two", "address": "AA:BB:CC:DD:EE:02", "model": "KS2BB",
             "name": "Synthetic bedroom", "configured": False, "token_available": False},
        ], **values,
    }


def test_poll_preserves_selected_device_when_cloud_metadata_changes(javascript):
    initial = cloud_state()
    changed = cloud_state()
    changed["devices"][0]["name"] = "Updated synthetic name"
    changed["devices"].reverse()
    result = javascript({"action": "selections", "initial": initial, "next": changed})
    assert result["before"]["selected"] == result["after"]["selected"] == ["opaque-one"]
    assert result["after"]["partial"] is True
    assert result["after"]["saveDisabled"] is False
    assert [row["checkbox"] for row in result["after"]["rows"]] == [False, True]


def test_new_login_clears_previous_selection(javascript):
    result = javascript({"action": "selections", "initial": cloud_state(),
                         "next": cloud_state(run_id="another-run")})
    assert result["after"]["selected"] == []
    assert result["after"]["saveDisabled"] is True


def test_save_sends_selected_opaque_ids_and_control_header(javascript):
    result = javascript({"action": "save", "initial": cloud_state(), "selected": "opaque-two",
                         "response": cloud_state("saving")})
    assert result["calls"] == [{
        "url": "/api/cloud", "method": "POST",
        "body": {"action": "save", "run_id": "synthetic-run", "devices": ["opaque-two"]},
        "headers": {"Content-Type": "application/json", "X-Linptech-Control": "1"},
    }]
    assert result["phase"] == "saving"
    assert result["saveDisabled"] is True


def test_closing_drawer_removes_qr_and_cancels_only_its_run(javascript):
    result = javascript({"action": "close", "initial": cloud_state("awaiting_qr", qr_available=True)})
    assert result["closed"] is True
    assert result["cleared"] == [42]
    assert result["qrSrc"] == ""
    assert result["calls"][0]["body"] == {"action": "cancel", "run_id": "synthetic-run"}


def test_closed_drawer_cancels_login_returned_after_start_request(javascript):
    result = javascript({"action": "delayed-start-close", "initial": cloud_state("idle", run_id=None),
                         "response": cloud_state("awaiting_qr", run_id="late-run", qr_available=True)})
    assert result["closed"] is True
    assert result["qrSrc"] == ""
    assert [call["body"] for call in result["calls"]] == [
        {"action": "start", "region": "cn"}, {"action": "cancel", "run_id": "late-run"},
    ]


def test_poll_started_before_save_cannot_roll_back_newer_command(javascript):
    result = javascript({"action": "stale-poll", "initial": cloud_state(), "selected": "opaque-one",
                         "response": cloud_state("saving")})
    assert result["phase"] == "saving"
    assert result["saveDisabled"] is True


@pytest.mark.parametrize(("online", "paused"), [(False, False), (True, True)])
def test_offline_or_paused_drawer_disables_device_selection(javascript, online, paused):
    result = javascript({"action": "availability", "initial": cloud_state(),
                         "online": online, "paused": paused})
    assert result["saveDisabled"] is True
    assert result["regionDisabled"] is True
    assert all(result["checkboxDisabled"])


def test_qr_is_same_origin_and_removed_when_login_completes(javascript):
    result = javascript({"action": "qr", "initial": cloud_state("awaiting_qr", run_id="run&unsafe=query", qr_available=True),
                         "next": cloud_state()})
    assert result["before"]["qrSrc"] == "/api/cloud/qr.png?run=run%26unsafe%3Dquery&revision=1"
    assert result["after"]["qrHidden"] is True
    assert result["after"]["qrSrc"] == ""


@pytest.mark.parametrize(("token_configured", "token_retrieved", "expected_token"), [
    (True, False, "沿用原值"),
    (False, False, "未取得"),
    (True, True, "已取得並保存"),
])
def test_results_distinguish_newly_retrieved_keys_from_preserved_or_missing_tokens(
    javascript, token_configured, token_retrieved, expected_token,
):
    result = javascript({
        "action": "results", "initial": cloud_state("saving"),
        "next": cloud_state("complete", results=[{
            "name": "Synthetic selected sensor", "model": "KS2BB",
            "key_configured": True, "bindkey_retrieved": True,
            "token_configured": token_configured, "token_retrieved": token_retrieved,
        }]),
    })
    assert result["resultHidden"] is False
    assert result["resultRows"] == [
        f"Synthetic selected sensor · bindkey 已取得並保存 · token {expected_token}",
    ]
    assert result["saveDisabled"] is True


def test_help_button_describes_sensors_and_qr_image_has_instructions():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="help-button"' in html
    assert "感測器介紹" in html
    assert 'id="cloud-qr-image" width="240" height="240" alt="米家登入 QR' in html
    assert 'id="cloud-region" required' in html
