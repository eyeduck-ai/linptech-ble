"""Recording footer semantics with synthetic state and a minimal Node DOM."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


APP = Path(__file__).parents[1] / "linptech_ble" / "web" / "app.js"


@pytest.fixture(scope="module")
def javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; install it for dashboard recording tests")
    script = r"""
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const boundary = source.indexOf('let latest = null;');
if (boundary < 0) throw new Error('Missing pure helper boundary');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const fields = new Map(), writes = new Map();
const field = id => {
  if (!fields.has(id)) {
    let text = '';
    fields.set(id, { hidden: false, className: '',
      get textContent() { return text; },
      set textContent(value) { text = value; writes.set(id, (writes.get(id) || 0) + 1); },
      set innerHTML(_value) { throw new Error('HTML must not be used'); }
    });
  }
  return fields.get(id);
};
const context = vm.createContext({ Intl, Date, Number, Math,
  document: { getElementById: field }, payload,
  advancedPanels: new Map(), renderCards() {}, renderSensorSettings() {},
  renderRadio() {}, updateManagementAvailability() {}, shown: null, paused: true,
  latest: null, connected: true });
vm.runInContext(source.slice(0, boundary), context);
const run = expression => vm.runInContext(expression, context);
if (payload.action === 'render') {
  for (const name of ['setText', 'renderRecording']) {
    const line = source.split(/\r?\n/).find(line => line.startsWith(`function ${name}(`));
    if (!line) throw new Error(`Missing ${name}`);
    run(line);
  }
  const start = source.indexOf('function renderConnection() {');
  const end = source.indexOf('function receive(', start);
  if (start < 0 || end < 0) throw new Error('Missing connection render boundary');
  run(source.slice(start, end));
  for (const state of payload.states) {
    context.latest = state;
    context.connected = state.online !== false;
    run('renderConnection()');
  }
  process.stdout.write(JSON.stringify({ text: field('recording-info').textContent,
    writes: writes.get('recording-info'), notice: field('notice').textContent }));
} else {
  process.stdout.write(JSON.stringify(payload.cases.map(item => {
    context.item = item;
    return run('recordingFooter(item.state, item.online !== false)');
  })));
}
"""

    def run(payload):
        completed = subprocess.run(
            [node, "-e", script, str(APP)], input=json.dumps(payload),
            text=True, encoding="utf-8", capture_output=True, timeout=15, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    return run


def snapshot(status="running", **extra):
    return {"enabled": True, "status": status, "dropped": 0, "errors": 0, **extra}


def state(recording=None, **extra):
    return {"mode": "live", "scanner": {"status": "listening"},
            "recording": recording, **extra}


@pytest.mark.parametrize(("stats", "expected"), [
    (None, "廣播紀錄狀態未知"),
    (snapshot("idle"), "廣播紀錄準備中"),
    (snapshot("starting"), "廣播紀錄準備中"),
    (snapshot(), "廣播紀錄中"),
    (snapshot("disabled", enabled=False), "廣播紀錄已停用"),
    (snapshot("closing"), "廣播紀錄收尾中"),
    (snapshot("closed"), "廣播紀錄已停止"),
    (snapshot("error"), "廣播紀錄異常"),
    (snapshot("toString"), "廣播紀錄狀態未知"),
    (snapshot(errors=1), "廣播紀錄中（曾異常）"),
    (snapshot(dropped=1234), "廣播紀錄中 · 已漏記 1,234 筆"),
])
def test_advertisement_recording_phases_are_truthful(javascript, stats, expected):
    result, = javascript({"cases": [{"state": state(stats)}]})
    assert result == expected


def test_replay_never_claims_it_is_recording_and_disconnect_marks_last_status(javascript):
    results = javascript({"cases": [
        {"state": state(snapshot(), mode="replay", direct_devices={"A": {"recording_stats": snapshot("error")}})},
        {"state": state(snapshot()), "online": False},
    ]})
    assert results == ["歷史重播 · 不寫入紀錄", "最後狀態 · 廣播紀錄中"]


def test_direct_stats_are_unique_and_disconnected_errors_and_drops_remain_visible(javascript):
    failed = {"recording_stats": snapshot("closed", errors=1, dropped=3)}
    current = {"recording_stats": snapshot(dropped=2)}
    result, = javascript({"cases": [{"state": state(
        snapshot(dropped=1), direct_devices={"A": failed, "B": current},
        direct=failed, sensor_direct={"KS2BB": current},
    )}]})
    assert result == "廣播紀錄中 · 直連紀錄中（部分異常） · 已漏記 6 筆"


@pytest.mark.parametrize(("stats", "expected"), [
    (snapshot("idle"), "廣播紀錄已停用"),
    (snapshot("disabled", enabled=False), "廣播紀錄已停用"),
    (snapshot("starting"), "廣播紀錄已停用 · 直連紀錄準備中"),
    (snapshot(), "廣播紀錄已停用 · 直連紀錄中"),
    (snapshot("closed"), "廣播紀錄已停用 · 直連紀錄已停止"),
    (snapshot("error", dropped=1), "廣播紀錄已停用 · 直連紀錄異常 · 已漏記 1 筆"),
])
def test_direct_status_does_not_assume_all_recording_is_advertisement_recording(javascript, stats, expected):
    result, = javascript({"cases": [{"state": state(
        snapshot("disabled", enabled=False), direct_devices={"A": {"recording_stats": stats}},
    )}]})
    assert result == expected


def test_legacy_direct_stats_still_work_without_per_device_controllers(javascript):
    result, = javascript({"cases": [{"state": state(
        snapshot(), direct={"recording_stats": snapshot(dropped=2)},
        sensor_direct={"HS3BB": {"recording_stats": snapshot("error", errors=1)}},
    )}]})
    assert result == "廣播紀錄中 · 直連紀錄中（部分異常） · 已漏記 2 筆"


def test_footer_never_exposes_paths_errors_or_invalid_counter_values(javascript):
    result, = javascript({"cases": [{"state": state(snapshot(
        "error", dropped="<img src=x>", errors=-2, error_type="secret-token",
        paths={"events": "C:/private/account-token.jsonl"},
    ), direct_devices={"A": {"recording_stats": snapshot("closed", dropped=-1)}})}]})
    assert result == "廣播紀錄異常 · 直連紀錄已停止"
    assert not any(value in result for value in ["secret", "private", "<img", "jsonl"])


def test_paused_footer_uses_new_status_without_repeated_dom_writes(javascript):
    running = state(snapshot(written={"events": 1}))
    error = state(snapshot("error", errors=1))
    result = javascript({"action": "render", "states": [
        running, running, state(snapshot(written={"events": 200})), error, error,
    ]})
    assert result["text"] == "廣播紀錄異常"
    assert result["writes"] == 2
    assert "保存" not in result["notice"]
