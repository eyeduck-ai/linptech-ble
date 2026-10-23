"use strict";

const $ = (id) => document.getElementById(id);
const timeFormat = new Intl.DateTimeFormat("zh-TW", { timeZone: "Asia/Taipei", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dateFormat = new Intl.DateTimeFormat("zh-TW", { timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit" });
const numberFormat = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 1 });
const TYPES = { ES5BB: "人體存在感測器", MS1BB: "門窗感測器", KS2BB: "無線按鍵・溫濕度", HS3BB: "人體移動感測器" };
const FIELD_NAMES = { temperature: "溫度", humidity: "濕度", illuminance: "照度", battery: "電池電量", occupancy: "人體存在", occupancy_close_range: "近距區間", motion: "移動偵測", door: "門窗", opening: "門窗", door_left_open: "門未關警示", device_forcibly_removed: "防拆警示", door_stuck: "門卡住", knock_on_the_door: "敲門偵測", pry_the_door: "撬門警示" };
Object.assign(FIELD_NAMES, { no_motion_duration: "未移動持續時間", custom_no_motion_time: "自訂無人時間", duration_detected: "有人持續時間", duration_cleared: "無人持續時間" });
const ART = {
  ES5BB: '<circle cx="36" cy="36" r="5"/><circle cx="36" cy="36" r="16"/><path d="M11 24a28 28 0 0 1 50 0M8 44a29 29 0 0 0 56 0M36 3v5M36 64v5M3 36h5M64 36h5"/>',
  MS1BB: '<rect x="12" y="10" width="25" height="52" rx="9"/><rect x="44" y="20" width="14" height="32" rx="5"/><path d="M24 23v9M39 36h3"/>',
  KS2BB: '<rect x="12" y="12" width="48" height="48" rx="15"/><circle cx="36" cy="36" r="12"/><path d="M32 36h8M36 32v8M30 66h12"/>',
  HS3BB: '<path d="M13 50V27a23 23 0 0 1 46 0v23zM19 56h34M26 62h20"/><path d="M25 36a11 11 0 0 1 22 0M30 36a6 6 0 0 1 12 0"/><circle cx="36" cy="38" r="2"/>',
};
function signalState(device, state, { online = true, frozen = false } = {}) {
  const rssi = Number.isFinite(device.rssi) ? device.rssi : null;
  const age = device.last_seen ? (Date.parse(state.server_time) - Date.parse(device.last_seen)) / 1000 : Infinity;
  const current = online && !frozen && state.mode !== "replay" && !device.last_error &&
    device.last_seen && Number.isFinite(age) && age >= 0 && age <= state.stale_after && rssi !== null && rssi < 0;
  const level = current ? (rssi >= -60 ? 4 : rssi >= -75 ? 3 : rssi >= -90 ? 2 : 1) : 0;
  const status = !online ? "主機離線" : frozen ? "畫面暫停" : state.mode === "replay" ? "歷史廣播" :
    device.last_error ? "接收異常" : !device.last_seen ? "等待廣播" : !Number.isFinite(age) || age > state.stale_after ? "廣播過舊" : "近期廣播";
  return { quality: ["unknown", "poor", "weak", "fair", "strong"][level], level, status, rssi, lastSeen: device.last_seen || null };
}
function temperatureSeries(device) {
  return (device?.history?.temperature || [])
    .filter(point => Number.isFinite(point.value) && Number.isFinite(Date.parse(point.timestamp)))
    .slice().sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
}
function qualityForRssi(value) {
  return !Number.isFinite(value) || value >= 0 || value <= -127 ? "unknown" :
    value >= -60 ? "strong" : value >= -75 ? "fair" : value >= -90 ? "weak" : "poor";
}
function filterDevices(devices, criteria, state) {
  const query = (criteria.query || "").trim().toLowerCase();
  return devices.filter(device => {
    if (criteria.model && criteria.model !== "all" && device.model !== criteria.model) return false;
    if (criteria.address && criteria.address !== "all" && device.address !== criteria.address) return false;
    if (query && !`${device.name} ${device.model} ${device.address}`.toLowerCase().includes(query)) return false;
    const age = device.last_seen ? (Date.parse(state.server_time) - Date.parse(device.last_seen)) / 1000 : Infinity;
    const recent = Boolean(device.last_seen && age >= 0 && age <= state.stale_after);
    switch (criteria.radio) {
      case "recent": return recent;
      case "weak": return recent && ["weak", "poor"].includes(qualityForRssi(device.rssi));
      case "stale": return Boolean(device.last_seen && age > state.stale_after);
      case "waiting": return !device.last_seen;
      case "error": return Boolean(device.last_error);
      default: return true;
    }
  });
}
function controllerFor(state, device) {
  if (!state || !device) return null;
  if (state.direct_devices && Object.keys(state.direct_devices).length) return state.direct_devices[device.address] || null;
  if (state.devices.find(item => item.model === device.model)?.address !== device.address) return null;
  return device.model === "ES5BB" ? state.direct : state.sensor_direct?.[device.model];
}
function radioSelection(device, state, requested = "auto") {
  const now = Date.parse(state.server_time);
  const history = (device.radio?.history || []).filter(point => qualityForRssi(point.rssi) !== "unknown" && Number.isFinite(Date.parse(point.timestamp)) && Date.parse(point.timestamp) <= now)
    .slice().sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  const receiver = requested === "auto" ? (history.at(-1)?.receiver || null) : requested;
  const points = history.filter(point => point.receiver === receiver && now >= Date.parse(point.timestamp) && now - Date.parse(point.timestamp) <= 300000);
  const summary = device.radio?.receivers?.[receiver] || { count: 0, latest: null, median: null, min: null, max: null, last_sample: null, last_sample_age_seconds: null, max_gap_seconds: null };
  return { receiver, points, summary, lastObserved: history.filter(point => point.receiver === receiver).at(-1)?.timestamp || null };
}
function csvCell(value) {
  let text = String(value ?? "");
  if (typeof value !== "number" && /^(?:[\t\r\n]|\s*[=+@-])/.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}
function es5FieldView(device, state, key) {
  const entry = key === "occupancy_close_range" ? device.binary_sensors?.[key] : device.measurements?.[key];
  const direct = controllerFor(state, device);
  const enabled = direct?.properties?.interest_enable;
  const disabled = key === "occupancy_close_range" && direct?.authenticated && enabled?.status === "read" && enabled.value === false;
  const notes = { illuminance: "照度獨立上報，不隨每次有人狀態變更更新。", battery: "電量可能低頻上報；供電模式不代表電池百分比。", occupancy_close_range: "需啟用近距關注範圍；此值表示區間有人，不是距離公尺數。" };
  return { entry, missing: disabled ? "未啟用" : "本輪未上報", detail: disabled ? "本次直連讀值：近距關注範圍已停用。" : notes[key] };
}
function canDisconnect(direct) { return Boolean(direct && (direct.authenticated || direct.status === "error")); }
let latest = null;
let shown = null;
let paused = false;
let connected = false;
let filter = "all";
let lastAnnouncedId = 0;
let initializedSession = null;
let selectedRadioDevice = null;
const advancedPanels = new Map(), temperaturePanels = new Map(), radioRowNodes = new Map();
let drawerTarget = null, drawerReturnFocus = null, managementSession = null;
function domId(address, id) { return `device-${address.replace(/[^a-zA-Z0-9]/g, "")}-${id}`; }
function clonePanel(templateId, address) {
  const content = $(templateId).content.cloneNode(true);
  content.querySelectorAll("[id]").forEach(node => { node.id = domId(address, node.id); });
  content.querySelectorAll("[for]").forEach(node => { node.htmlFor = domId(address, node.htmlFor); });
  content.querySelectorAll("[aria-describedby], [aria-labelledby]").forEach(node => {
    for (const attr of ["aria-describedby", "aria-labelledby"]) if (node.hasAttribute(attr))
      node.setAttribute(attr, node.getAttribute(attr).split(/\s+/).map(id => domId(address, id)).join(" "));
  });
  const element = content.firstElementChild;
  $("panel-staging").append(content); return element;
}
function filterCriteria() { return { query: $("device-search").value, model: $("model-filter").value, address: $("instance-filter").value, radio: $("radio-filter").value }; }
function visibleDevices(state) { return filterDevices(state.devices, filterCriteria(), state); }
function deviceLabel(device) { return `${device.name} · ${device.model} · ${device.address.slice(-8)}`; }
function syncOptions(select, choices) {
  const signature = JSON.stringify(choices);
  if (select.dataset.options === signature) return;
  const original = select.value;
  select.replaceChildren(...choices.map(([value, label]) => new Option(label, value)));
  if (choices.some(([value]) => value === original)) select.value = original;
  select.dataset.options = signature;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function timeText(timestamp) { return timestamp ? timeFormat.format(new Date(timestamp)) : "尚未收到"; }
function fmt(value) { return typeof value === "number" ? numberFormat.format(value) : String(value ?? "—"); }
function metric(device, key) { return device?.measurements?.[key]; }
function binary(device, key) { return device?.binary_sensors?.[key]; }
function updated(entry) { return entry ? `${entry.source === "gatt" ? "直連 · " : ""}${timeText(entry.updated_at)} 更新` : "尚未收到此欄位"; }
function unconfirmed(entry, state) { return entry && state.mode !== "replay" && (entry.freshness === "stale" || (new Date(state.server_time) - new Date(entry.updated_at)) / 1000 > state.stale_after); }
function ago(timestamp, now) {
  if (!timestamp) return "尚未收到";
  const seconds = Math.max(0, Math.floor((new Date(now) - new Date(timestamp)) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分前`;
  return `${Math.floor(seconds / 3600)} 小時前`;
}
function reading(label, entry, unit, valueOverride, absent) {
  const node = el("div", "reading");
  if (absent?.detail) node.title = absent.detail;
  node.append(el("span", "reading-label", label), el("span", "reading-value", entry ? (valueOverride ?? `${fmt(entry.value)}${unit ? " " + unit : ""}`) : (absent?.missing || "本輪未上報")), el("span", "reading-age", entry ? `${entry.source === "gatt" ? "直連" : "廣播"} · ${timeText(entry.updated_at)}` : "本次啟動尚無資料"));
  return node;
}
function cardFront(device, index, state) {
  const front = el("div", "card-front");
  const top = el("div", "card-top");
  const title = el("div");
  const model = el("p", "model-line", device.model);
  model.prepend(el("span", "sensor-number", String(index + 1).padStart(2, "0")));
  title.append(el("h3", "device-name", device.name), model, el("p", "card-type", `${TYPES[device.model] || "感測器"} · ${device.address.slice(-8)}`));
  title.title = device.name;
  const signal = signalState(device, state, { online: connected, frozen: paused });
  const freshness = device.last_error ? "failed" : (signal.level ? "recent" : "waiting");
  top.append(title, el("span", `freshness ${freshness}`, signal.status));
  const main = el("div", "card-main");
  const primary = el("div");
  let source = null, value = "等待資料", unit = "";
  if (device.model === "ES5BB") { source = binary(device, "occupancy"); if (source) value = source.value ? "有人" : "無人"; }
  if (device.model === "MS1BB") { source = binary(device, "door") || binary(device, "opening"); if (source) value = source.value ? "開啟" : "關閉"; }
  if (device.model === "KS2BB") { source = metric(device, "temperature"); if (source) { value = fmt(source.value); unit = "°C"; } }
  if (device.model === "HS3BB") { source = binary(device, "motion"); if (source) value = source.value ? "偵測到移動" : "未偵測到移動"; }
  const lastValue = value;
  const unknown = ["ES5BB", "HS3BB"].includes(device.model) && unconfirmed(source, state);
  if (unknown) value = "未確認";
  const big = el("p", `primary-value${source && !unknown ? "" : " missing"}`, value);
  big.dataset.state = !source || unknown ? "unknown" : typeof source.value === "boolean" ? (source.value ? "active" : "clear") : "clear";
  if (unit) big.append(el("span", "value-unit", unit));
  primary.append(big, el("p", "primary-updated", unknown ? `最後回報：${lastValue} · ${timeText(source.updated_at)}` : updated(source)));
  const art = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  art.setAttribute("viewBox", "0 0 72 72"); art.setAttribute("class", "sensor-art"); art.setAttribute("aria-hidden", "true");
  art.setAttribute("fill", "none"); art.setAttribute("stroke", "currentColor"); art.setAttribute("stroke-width", "1.3"); art.setAttribute("stroke-linecap", "round");
  art.innerHTML = ART[device.model] || ""; // Static drawings only; received data uses textContent.
  main.append(primary, art);
  const readings = el("div", "card-readings");
  if (device.model === "ES5BB") {
    const luxView = es5FieldView(device, state, "illuminance"), nearView = es5FieldView(device, state, "occupancy_close_range");
    const near = nearView.entry;
    const nearValue = near ? `${unconfirmed(near, state) ? "最後：" : ""}${near.value ? "有人" : "無人"}` : undefined;
    readings.append(reading("環境照度", luxView.entry, "lux", undefined, luxView), reading("近距區間", near, "", nearValue, nearView));
  } else if (device.model === "KS2BB") {
    readings.append(reading("相對濕度", metric(device, "humidity"), "%"));
    const action = device.last_event;
    const eventReading = el("div", "reading");
    eventReading.append(el("span", "reading-label", "最近按鍵"), el("span", "reading-value", action?.label || "尚未觸發"), el("span", "reading-age", action ? timeText(action.timestamp) : "—"));
    readings.append(eventReading);
  } else if (device.model === "HS3BB") {
    readings.append(reading("環境照度", metric(device, "illuminance"), "lux"), reading("電池電量", metric(device, "battery"), "%"));
  } else {
    readings.append(reading("電池電量", metric(device, "battery"), "%"));
  }
  if (["ES5BB", "KS2BB"].includes(device.model)) readings.append(reading("電池電量", metric(device, "battery"), "%", undefined, device.model === "ES5BB" ? es5FieldView(device, state, "battery") : undefined));
  // Show additional reported fields too, without manufacturing unreported data.
  const visible = new Set(["temperature", "occupancy", "door", "opening", "motion", "illuminance", "humidity", "occupancy_close_range", "battery"]);
  for (const entry of Object.values(device.measurements)) {
    if (!visible.has(entry.key)) readings.append(reading(FIELD_NAMES[entry.key] || entry.key, entry, entry.unit === "lx" ? "lux" : entry.unit));
  }
  for (const entry of Object.values(device.binary_sensors)) {
    if (!visible.has(entry.key)) readings.append(reading(FIELD_NAMES[entry.key] || entry.key, entry, "", entry.value ? "觸發" : "未觸發"));
  }
  const reception = el("div", "signal-indicator"); reception.dataset.quality = signal.quality;
  const bars = el("span", "signal-bars"); bars.setAttribute("aria-hidden", "true");
  for (let i = 1; i <= 4; i++) bars.append(el("i", i <= signal.level ? "lit" : ""));
  const copy = el("div", "signal-copy");
  const strength = { strong: "訊號強", fair: "訊號穩定", weak: "訊號偏弱", poor: "訊號微弱", unknown: signal.status }[signal.quality];
  copy.append(el("strong", "signal-label", strength), el("span", "signal-age", signal.lastSeen ? `最後廣播 ${ago(signal.lastSeen, state.server_time)}` : "尚未收到廣播"));
  const number = el("span", "signal-number", signal.rssi === null ? "— dBm" : `${signal.rssi} dBm${signal.level ? "" : " · 上次"}`);
  reception.title = "RSSI 為最後一筆藍牙廣播的接收強度；不代表感測值新鮮度或直連訊號。";
  reception.append(bars, copy, number);
  front.append(top, main, readings, reception);
  if (device.last_error) front.append(el("p", "card-error", device.last_error));
  return front;
}
const cardNodes = new Map();
let cardLayout = null;
function ensureCards(state) {
  const grid = $("sensor-grid");
  const layout = state.devices.map(device => `${device.address}:${device.model}:${device.name}`).join("|");
  if (layout !== cardLayout) {
    // Keep the original controls and their event bindings even when a host
    // restarts with a different device list. Never discard mounted panels.
    for (const [address, panel] of advancedPanels) {
      if (!state.devices.some(device => device.address === address && device.model === panel.model)) {
        if (drawerTarget === address) $("device-drawer").close();
        (panel.host || panel.element).remove(); advancedPanels.delete(address); sensorOperations.delete(address);
      }
    }
    for (const [address, panel] of temperaturePanels) {
      if (state.devices.some(device => device.address === address && device.model === "KS2BB")) $("panel-staging").append(panel);
      else { panel.remove(); temperaturePanels.delete(address); }
    }
    grid.replaceChildren(); cardNodes.clear(); cardLayout = layout;
  }
  const retained = new Set();
  state.devices.forEach((device, index) => {
    retained.add(device.address);
    let card = cardNodes.get(device.address);
    if (!card) {
      card = el("article", "sensor-card"); card.dataset.model = device.model;
      card.setAttribute("aria-label", `${device.model} ${TYPES[device.model] || device.name}`);
      card.append(cardFront(device, index, state));
      if (device.model === "KS2BB") {
        if (!temperaturePanels.has(device.address)) temperaturePanels.set(device.address, clonePanel("temperature-template", device.address));
        card.append(temperaturePanels.get(device.address));
      }
      const details = el("div", "device-advanced"); details.id = domId(device.address, "advanced");
      const summary = el("button", "device-advanced-trigger"); summary.type = "button";
      summary.setAttribute("aria-haspopup", "dialog"); summary.setAttribute("aria-controls", "device-drawer");
      const label = el("span", "advanced-label", "進階設定");
      label.append(el("span", "visually-hidden", ` · ${deviceLabel(device)}`));
      const status = el("span", "advanced-status", "未直連");
      summary.append(label, status);
      if (!advancedPanels.has(device.address)) advancedPanels.set(device.address,
        device.model === "ES5BB" ? createDirectPanel(device) : createSensorPanel(device));
      const panel = advancedPanels.get(device.address);
      if (!panel.host) {
        const host = el("div", "advanced-content"), management = el("div", "drawer-device-actions");
        const edit = el("button", "button quiet", "名稱與憑證設定"); edit.type = "button";
        edit.addEventListener("click", () => openManagement(device.address, summary)); management.append(edit);
        host.append(management, panel.element);
        if (device.model === "ES5BB") { const observations = el("section", "management-section es5-observations"); observations.setAttribute("aria-label", "ES5BB 廣播欄位與來源"); host.prepend(observations); }
        const diagnostic = el("details", "card-decode"); diagnostic.append(el("summary", "", "廣播解析明細"));
        diagnostic.append(el("div", "decode-device")); host.append(diagnostic); panel.host = host;
        $("panel-staging").append(host);
      }
      summary.addEventListener("click", () => openAdvanced(device.address, summary));
      details.append(summary); card.append(details);
      grid.append(card); cardNodes.set(device.address, card);
    }
  });
  for (const [address, node] of cardNodes) if (!retained.has(address)) { node.remove(); cardNodes.delete(address); }
}
function releaseDrawerContent() {
  if (drawerTarget && advancedPanels.has(drawerTarget)) $("panel-staging").append(advancedPanels.get(drawerTarget).host);
  if (managementSession) {
    clearInterval(managementSession.poll);
    managementSession.element.querySelectorAll('input[type="password"], input[data-secret]').forEach(input => { input.value = ""; });
    if (managementSession.scanning) void managementRequest("/api/discovery", { action: "stop" }).catch(() => {});
    managementSession = null;
  }
  drawerTarget = null;
}
function showDrawer(title, subtitle, element, trigger) {
  releaseDrawerContent();
  if (trigger) drawerReturnFocus = trigger;
  setText("drawer-title", title); setText("drawer-subtitle", subtitle);
  $("drawer-content").replaceChildren(element);
  if (!$("device-drawer").open) $("device-drawer").showModal();
  $("drawer-content").scrollTop = 0; $("drawer-close").focus();
}
function openAdvanced(address, trigger) {
  const device = latest?.devices.find(item => item.address === address), panel = advancedPanels.get(address);
  if (!device || !panel) return;
  showDrawer(device.name, `${device.model} · ${device.address}`, panel.host, trigger);
  drawerTarget = address;
}
async function managementRequest(url, body) {
  const response = await fetch(url, { method: body ? "POST" : "GET", cache: "no-store", headers: body ? { "Content-Type": "application/json", "X-Linptech-Control": "1" } : {}, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(15000) });
  let result;
  try { result = await response.json(); } catch { throw new Error("本機程式未回覆管理結果，請重新整理後確認。"); }
  if (!response.ok) throw new Error(result.error || "無法完成裝置管理操作。");
  return result;
}
function managementUi(session, id) { return session.element.querySelector(`#${id}`); }
function managementFeedback(session, text, error = false) {
  const node = managementUi(session, "management-feedback"); node.textContent = text; node.dataset.state = error ? "error" : "success";
}
function updateManagementAvailability() {
  const unavailable = !connected || paused || latest?.mode === "replay" || latest?.management?.enabled === false;
  $("add-device").disabled = unavailable;
  if (!managementSession) return;
  const session = managementSession, locked = unavailable || session.posting || session.loading;
  managementUi(session, "management-form").querySelectorAll("input, select").forEach(input => { input.disabled = locked; });
  managementUi(session, "management-model").disabled = locked || session.editing;
  managementUi(session, "management-save").disabled = locked;
  managementUi(session, "discovery-start").disabled = locked;
  managementUi(session, "management-remove").disabled = locked || !managementUi(session, "management-remove-confirm").checked;
}
function fillManagement(session, device) {
  session.editing = Boolean(device); session.address = device?.address || null;
  managementUi(session, "discovery-section").hidden = session.editing;
  managementUi(session, "management-title").textContent = session.editing ? "名稱與憑證" : "加入感測器";
  managementUi(session, "management-model").value = device?.model || "";
  managementUi(session, "management-model").disabled = session.editing;
  managementUi(session, "management-address").value = device?.address || "";
  managementUi(session, "management-address").readOnly = session.editing;
  managementUi(session, "management-name").value = device?.name || "";
  managementUi(session, "management-miot-field").hidden = device?.model !== "HS3BB";
  managementUi(session, "management-miot-confirm").checked = device?.miot_model === "linp.motion.hs1bb2";
  managementUi(session, "management-bindkey").value = ""; managementUi(session, "management-token").value = "";
  managementUi(session, "management-remove-confirm").checked = false;
  managementUi(session, "management-danger").hidden = !session.editing;
  managementUi(session, "management-back").hidden = !session.editing;
  managementUi(session, "management-save").textContent = session.editing ? "儲存變更" : "加入裝置";
  managementUi(session, "credential-status").textContent = session.editing ? `bindkey：${device.key_configured ? "已保存" : "未設定"} · token：${device.token_configured ? "已保存" : "未設定"} · 已存憑證不會回傳到網頁。` : "先加入裝置即可查看 RSSI；解密數值需要 bindkey，進階直連另需 token。";
  if (session.editing) { setText("drawer-title", device.name); setText("drawer-subtitle", `${device.model} · ${device.address}`); }
  updateManagementAvailability();
}
function renderDiscovery(session, discovery) {
  if (managementSession !== session) return;
  session.scanning = ["scanning", "running"].includes(discovery.status);
  const status = managementUi(session, "discovery-status");
  status.textContent = discovery.error || (session.scanning ? "掃描中，請喚醒感測器並稍候…" : discovery.status === "complete" ? "掃描完成。選取裝置後填寫名稱與憑證。" : "選取已發現的裝置，或在下方手動填寫 MAC。");
  managementUi(session, "discovery-start").textContent = session.scanning ? "停止掃描" : "掃描 60 秒";
  const devices = discovery.devices || [], signature = JSON.stringify(devices.map(device => [device.address, device.model, device.name, device.rssi, device.configured]));
  if (signature === session.discoverySignature) return;
  session.discoverySignature = signature;
  const nodes = devices.map(candidate => {
    const row = el("div", "discovery-device"), identity = el("div");
    identity.append(el("strong", "", candidate.model || candidate.name || "未支援的 MiBeacon 裝置"), el("span", "subtle", `${candidate.address} · ${Number.isFinite(candidate.rssi) ? `${candidate.rssi} dBm` : "RSSI 未知"}`));
    const button = el("button", "button quiet discovery-choice", candidate.configured ? "已加入 · 管理" : "選取"); button.type = "button";
    button.disabled = !Object.hasOwn(TYPES, candidate.model) || session.posting;
    button.addEventListener("click", () => {
      if (candidate.configured) { void openManagement(candidate.address, drawerReturnFocus); return; }
      managementUi(session, "management-address").value = candidate.address;
      managementUi(session, "management-model").value = candidate.model;
      managementUi(session, "management-miot-field").hidden = candidate.model !== "HS3BB";
      managementUi(session, "management-miot-confirm").checked = false;
      if (!managementUi(session, "management-name").value) managementUi(session, "management-name").value = candidate.model;
      managementUi(session, "management-name").focus();
      managementFeedback(session, `已選取 ${candidate.model}，請設定名稱與憑證後加入。`);
    });
    row.append(identity, button); return row;
  });
  if (!nodes.length) nodes.push(el("p", "empty-discovery", session.scanning ? "尚未發現裝置。可短按感測器按鍵喚醒。" : "尚無掃描結果。"));
  managementUi(session, "discovery-list").replaceChildren(...nodes);
}
async function pollDiscovery(session) {
  if (session.polling || managementSession !== session || session.editing || !connected) return;
  session.polling = true;
  try { renderDiscovery(session, await managementRequest("/api/discovery")); }
  catch { if (managementSession === session) managementUi(session, "discovery-status").textContent = "無法讀取掃描結果，請確認本機程式仍在執行。"; }
  finally { session.polling = false; }
}
async function openManagement(address = null, trigger = null) {
  const element = $("management-template").content.firstElementChild.cloneNode(true);
  showDrawer(address ? "裝置管理" : "新增感測器", address || "掃描附近裝置，或手動輸入藍牙 MAC。", element, trigger);
  const session = { element, editing: Boolean(address), address, posting: false, loading: true, scanning: false, poll: null };
  managementSession = session;
  const ui = id => managementUi(session, id);
  ui("management-model").addEventListener("change", () => { ui("management-miot-field").hidden = ui("management-model").value !== "HS3BB"; ui("management-miot-confirm").checked = false; });
  ["management-bindkey", "management-token"].forEach(id => { ui(id).dataset.secret = "true"; });
  ui("management-reveal").addEventListener("change", () => { ["management-bindkey", "management-token"].forEach(id => { ui(id).type = ui("management-reveal").checked ? "text" : "password"; }); });
  ui("management-remove-confirm").addEventListener("change", updateManagementAvailability);
  ui("management-back").addEventListener("click", () => openAdvanced(session.address, drawerReturnFocus));
  ui("discovery-start").addEventListener("click", async () => {
    if (ui("discovery-start").disabled) return;
    session.posting = true; updateManagementAvailability();
    try { renderDiscovery(session, await managementRequest("/api/discovery", { action: session.scanning ? "stop" : "start" })); }
    catch (error) { if (managementSession === session) ui("discovery-status").textContent = error.name === "TimeoutError" ? "掃描請求逾時，請稍候確認結果。" : error.message; }
    finally { session.posting = false; updateManagementAvailability(); }
  });
  ui("management-form").addEventListener("submit", event => { event.preventDefault(); if (!ui("management-save").disabled && ui("management-form").reportValidity()) void saveManagedDevice(session); });
  ui("management-remove").addEventListener("click", () => { if (!ui("management-remove").disabled) void saveManagedDevice(session, true); });
  fillManagement(session, address ? latest?.devices.find(item => item.address === address) : null);
  try {
    const result = await managementRequest("/api/devices");
    if (managementSession !== session) return;
    if (address) {
      const device = result.devices.find(item => item.address === address);
      if (!device) throw new Error("此裝置已移除，請重新整理裝置清單。");
      fillManagement(session, device);
    }
    session.loading = false;
    if (!address) { await pollDiscovery(session); session.poll = setInterval(() => { void pollDiscovery(session); }, 1500); }
  } catch (error) { if (managementSession === session) managementFeedback(session, error.message, true); }
  finally { updateManagementAvailability(); }
}
async function saveManagedDevice(session, remove = false) {
  if (managementSession !== session || session.posting) return;
  const ui = id => managementUi(session, id);
  const address = ui("management-address").value.trim().toUpperCase().replaceAll("-", ":").replace(/^([0-9A-F]{2})([0-9A-F]{2})([0-9A-F]{2})([0-9A-F]{2})([0-9A-F]{2})([0-9A-F]{2})$/, "$1:$2:$3:$4:$5:$6");
  const command = { action: remove ? "remove" : session.editing ? "update" : "add", address };
  if (!remove) {
    command.name = ui("management-name").value.trim();
    if (!session.editing) command.model = ui("management-model").value;
    if (ui("management-model").value === "HS3BB" && ui("management-miot-confirm").checked) command.miot_model = "linp.motion.hs1bb2";
    for (const [id, key] of [["management-bindkey", "bindkey"], ["management-token", "auth_token"]]) if (ui(id).value.trim()) command[key] = ui(id).value.trim();
  }
  session.posting = true; updateManagementAvailability(); managementFeedback(session, "正在儲存並同步監聽設定…");
  try {
    await managementRequest("/api/devices", command);
    ui("management-bindkey").value = ""; ui("management-token").value = "";
    const response = await fetch("/api/state", { cache: "no-store" }); if (response.ok) receive(await response.json());
    if (managementSession !== session) return;
    if (remove) { $("device-drawer").close(); $("announcer").textContent = "裝置已從本機設定移除。"; return; }
    const result = await managementRequest("/api/devices");
    if (managementSession !== session) return;
    fillManagement(session, result.devices.find(item => item.address === address));
    clearInterval(session.poll); managementFeedback(session, "已儲存並套用到監聽程式，不需重新啟動。");
  } catch (error) {
    if (managementSession === session) managementFeedback(session, error.name === "TimeoutError" ? "請求逾時；請先關閉此視窗並確認裝置清單，再決定是否重試。" : error.message, true);
  } finally { session.posting = false; updateManagementAvailability(); }
}
function renderEs5Observations(device, state) {
  const section = advancedPanels.get(device.address)?.host?.querySelector(".es5-observations"); if (!section) return;
  const readings = el("div", "card-readings");
  for (const [key, label, unit] of [["illuminance", "廣播照度", "lux"], ["occupancy_close_range", "近距區間", ""], ["battery", "電池電量", "%"]]) {
    const view = es5FieldView(device, state, key);
    readings.append(reading(label, view.entry, unit, key === "occupancy_close_range" && view.entry ? (view.entry.value ? "有人" : "無人") : undefined, view));
  }
  if (!section.querySelector(".card-readings")) {
    const notes = el("details", "connection-help"); notes.append(el("summary", "", "欄位上報與米家設定"));
    notes.append(el("p", "", "照度、近距區間與電量各自上報，不保證隨有人事件更新。本輪未收到不代表 0、無人或不支援。照度與近距區間未開放直接讀取；電量直讀能力未確認。"));
    notes.append(el("p", "", "近距區間需啟用近距關注範圍。供電模式與電池百分比不同；直連的光照判斷值也不等同廣播照度。"));
    notes.append(el("p", "", "米家與本工具共用裝置內的設定。米家改過校正、感應距離或延遲後，會影響裝置實際回報；離開米家裝置頁並關閉手機藍牙，再重新連線／讀取即可核對，不必只能在此設定。"));
    section.replaceChildren(el("h3", "", "感測資料與來源"), readings, notes);
  } else section.querySelector(".card-readings").replaceWith(readings);
}
function renderCards(state) {
  ensureCards(state);
  syncOptions($("instance-filter"), [["all", "所有裝置"], ...state.devices.filter(device => $("model-filter").value === "all" || device.model === $("model-filter").value).map(device => [device.address, deviceLabel(device)])]);
  const visible = new Set(visibleDevices(state).map(device => device.address));
  $("device-count").textContent = `${visible.size} / ${state.devices.length} 台`;
  $("device-empty").hidden = visible.size !== 0;
  $("device-empty").textContent = state.devices.length ? "沒有符合條件的裝置" : "尚未設定裝置，按「新增感測器」開始。";
  state.devices.forEach((device, index) => {
    const card = cardNodes.get(device.address);
    card.hidden = !visible.has(device.address);
    card.querySelector(".card-front").replaceWith(cardFront(device, index, state));
    const d = controllerFor(state, device);
    const text = state.mode === "replay" ? "重播模式" : device.model === "MS1BB" ? "廣播資料" :
      d?.calibration?.restore_required ? "需要還原" : !connected ? "主機離線" :
      d?.busy ? "處理中" : d?.authenticated ? (d.streaming?.radar_active ? "雷達即時" : "已直連") : "未直連";
    const badge = card.querySelector(".advanced-status"); badge.textContent = text;
    badge.dataset.state = d?.calibration?.restore_required ? "error" : d?.authenticated && connected ? "active" : "idle";
    if (drawerTarget === device.address) { setText("drawer-title", device.name); setText("drawer-subtitle", `${device.model} · ${device.address}`); }
    if (device.model === "ES5BB") renderEs5Observations(device, state);
  });
}
function renderFeed(state) {
  const kind = $("event-kind").value;
  const visible = new Set(visibleDevices(state).map(device => device.address));
  const entries = state.events.filter((event) => visible.has(event.address) && (filter === "all" || event.model === filter) && (kind === "all" || event.kind === kind));
  const nodes = entries.map((event) => {
    const item = el("li"); item.dataset.kind = event.kind;
    const details = el("div");
    const translated = event.text.replace(/^([a-z_]+) · /, (match, key) => FIELD_NAMES[key] ? `${FIELD_NAMES[key]} · ` : match);
    details.append(el("span", "event-model", `${event.name} · ${event.model} · ${event.address.slice(-8)}`), el("span", "event-text", translated));
    const time = el("time", "event-time", timeText(event.timestamp));
    time.dateTime = event.timestamp; time.title = new Date(event.timestamp).toLocaleString("zh-TW", { timeZone: "Asia/Taipei" });
    item.append(el("span", "event-mark"), details, time);
    return item;
  });
  if (!nodes.length) nodes.push(el("li", "empty", state.events.length ? "目前篩選條件下沒有紀錄。" : "等待第一筆廣播。可從「測試操作」查看觸發方式。"));
  $("event-feed").replaceChildren(...nodes);
  $("feed-retained").textContent = `${state.events.length} / 300`;
}
function svgEl(name, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  if (text !== undefined) node.textContent = text;
  return node;
}

const DIRECT_STATUS = { disconnected: "尚未連線", connecting: "建立藍牙連線中", authenticating: "驗證直連憑證中", reading: "讀取感測器屬性中", writing: "送出設定並回讀中", ready: "已連線", error: "操作未完成" };
DIRECT_STATUS.stream_switching = "切換訊號串流中";
function setText(id, value) { if ($(id).textContent !== value) $(id).textContent = value; }
function propertyText(prop) {
  if (prop?.status === "invalid_value") return `未定義值：${fmt(prop.value)}`;
  if (prop?.status === "unavailable" && prop.value === null) return "裝置回覆空值";
  if (!prop || prop.value === null) return "尚未讀取";
  if (prop.choices?.length && Number.isInteger(prop.value) && prop.choices[prop.value]) return prop.choices[prop.value];
  if (typeof prop.value === "boolean") return prop.value ? "啟用" : "停用";
  return fmt(prop.value);
}
function createDirectPanel(device) {
  const element = clonePanel("direct-template", device.address);
  const $ = id => document.getElementById(domId(device.address, id));
let directSession = null;
let directCatalog = "";
let settingSignature = "";
let settingDirty = false;
let controlPosting = false;
let pendingControl = null;
let pendingControlSince = 0, pendingControlSnapshot = 0;
let controlFeedback = "";
let completedControl = null;
let zoneDirty = false;
let zoneSignature = "";
let previewSignature = "";

function setText(id, value) { if ($(id).textContent !== value) $(id).textContent = value; }
function propertyText(prop) {
  if (prop?.status === "invalid_value") return `未定義值：${fmt(prop.value)}`;
  if (prop?.status === "unavailable" && prop.value === null) return "裝置回覆空值";
  if (!prop || prop.value === null) return "尚未讀取";
  if (prop.choices?.length && Number.isInteger(prop.value) && prop.choices[prop.value]) return prop.choices[prop.value];
  if (typeof prop.value === "boolean") return prop.value ? "啟用" : "停用";
  return fmt(prop.value);
}
function directFresh(prop, direct) {
  return Boolean(connected && !paused && direct.authenticated && prop?.status === "read" && prop.updated_at && Date.now() - new Date(prop.updated_at).getTime() < 8000);
}
function renderSignals(d) {
  const bitmap = d.properties.human_sensor_bitmaps, pir = d.bitmaps?.pir;
  const fresh = directFresh(bitmap, d);
  const radarOnly = d.properties.detect_mode?.value === 0;
  const hasPir = Number.isInteger(pir) && pir >= 0;
  setText("direct-pir", radarOnly ? (d.authenticated && d.properties.detect_mode.status === "read" ? "僅雷達模式" : "上次為僅雷達模式") : (!hasPir ? "尚未取得" : `${fresh ? "" : "最後："}${pir > 0 ? "觸發" : "未觸發"}`));
  $("direct-pir").dataset.state = hasPir && fresh && !radarOnly ? (pir > 0 ? "active" : "clear") : "unknown";
  setText("direct-pir-caption", radarOnly ? "此模式未使用紅外線偵測。" : (hasPir ? `${timeText(bitmap.updated_at)} · ${fresh ? "即時回報" : "最後讀值，等待更新"} · 整體觸發狀態` : "此封包尚未提供 PIR 欄位；不視為未觸發。"));
  const power = d.properties.power_mode;
  setText("direct-power", propertyText(power));
  setText("direct-power-caption", power?.updated_at ? `${timeText(power.updated_at)} · ${d.authenticated && power.status === "read" ? "本次連線讀值" : "上次讀值"} · 電池百分比見上方卡片` : "供電模式與電池百分比分別顯示");
}
function renderPirEnergy(d) {
  const pir = d.pir || {}, stream = d.streaming || {};
  const names = { radar: "雷達", pir: "PIR", off: "停止" };
  const fresh = pir.active && directFresh(d.properties.pir_energy, d);
  setText("stream-status", `${names[stream.mode] || "尚未啟動"}${stream.mode === "pir" ? (fresh ? " · 已收到即時能量" : " · 等待 PIR 資料") : ""}`);
  setText("pir-energy-value", pir.value === null || pir.value === undefined ? "尚未取得" : `${fresh ? "" : "最後："}${fmt(pir.value)}`);
  setText("pir-energy-caption", `${pir.updated_at ? `${timeText(pir.updated_at)} · ${fresh ? "即時" : "最後讀值"} · ` : ""}整體 PIR 原始值，非逐距離或 dBm；切換 PIR 時暫停雷達能量串流。${pir.threshold === null || pir.threshold === undefined ? "" : ` 閾值：${fmt(pir.threshold)}。`}`);
  const chart = $("pir-energy-chart"); chart.replaceChildren();
  const samples = (pir.samples || []).filter(p => Number.isFinite(p.value)).slice(-128);
  chart.setAttribute("aria-label", `PIR 能量${samples.length ? `，最近 ${samples.length} 筆，最新 ${fmt(samples.at(-1).value)}` : "，尚無資料"}`);
  if (!samples.length) { chart.append(svgEl("text", { x: 210, y: 76, "text-anchor": "middle", fill: "#687169", "font-size": 12 }, "選擇 PIR 串流後接收能量")); return; }
  const extent = Math.max(50, ...samples.map(p => Math.abs(p.value)));
  chart.append(svgEl("path", { d: "M28 75H412", stroke: "#aab6a6", "stroke-dasharray": "4 4" }));
  const points = samples.map((p, i) => `${28 + i * 384 / Math.max(1, samples.length - 1)},${75 - p.value * 60 / extent}`);
  chart.append(svgEl("polyline", { points: points.join(" "), fill: "none", stroke: "#3e6951", "stroke-width": 2 }));
  chart.append(svgEl("text", { x: 0, y: 16, fill: "#687169", "font-size": 10 }, fmt(extent)), svgEl("text", { x: 0, y: 140, fill: "#687169", "font-size": 10 }, fmt(-extent)));
}
function renderRadarTable(d) {
  const names = ["motion_energy", "presence_energy", "human_sensor_bitmaps"];
  const labels = ["觸發", "維持", "區段"];
  setText("radar-detail-age", names.map((name, i) => `${labels[i]}：${d.properties[name]?.updated_at ? `${timeText(d.properties[name].updated_at)}（${directFresh(d.properties[name], d) ? "即時" : "最後讀值"}）` : "尚未收到"}`).join(" · "));
  const body = $("radar-bin-values");
  if (!body.children.length) {
    for (let i = 0; i < 32; i++) {
      const row = el("tr"), range = el("th", "", `${(i * .25).toFixed(2)}–${((i + 1) * .25).toFixed(2)}`);
      range.scope = "row"; row.append(range, el("td"), el("td"), el("td"), el("td")); body.append(row);
    }
  }
  [...body.children].forEach((row, i) => {
    ["motion", "presence"].forEach((kind, column) => {
      const prop = d.properties[`${kind}_energy`], threshold = d.properties[`${kind}_threshold`];
      const value = prop?.series?.[i], limit = threshold?.series?.[i];
      const fresh = directFresh(prop, d), above = Number.isFinite(value) && Number.isFinite(limit) && value > limit;
      const cell = row.cells[column + 1];
      const text = `${Number.isFinite(value) ? value : "—"} / ${Number.isFinite(limit) ? limit : "—"}${above ? " ↑超閾值" : ""}`;
      if (cell.textContent !== text) cell.textContent = text;
      cell.className = fresh ? (above ? "above-threshold" : "") : "cached-value";
    });
    const enabled = d.regions?.detect_region?.[i];
    const active = d.bitmaps ? Boolean((d.bitmaps.occupied >>> i) & 1) : null;
    const fresh = directFresh(d.properties.human_sensor_bitmaps, d);
    row.cells[3].textContent = enabled === undefined ? "—" : (enabled ? "啟用" : "停用");
    row.cells[4].textContent = active === null ? "尚未收到" : `${fresh ? "" : "最後："}${active ? "有人" : "未標記"}`;
    row.cells[4].className = fresh ? "" : "cached-value";
  });
}
function renderEnergy(prefix, prop, threshold, direct) {
  const chart = $(`${prefix}-energy-chart`);
  chart.replaceChildren();
  const values = prop?.series;
  const label = prefix === "motion" ? "觸發有人能量" : "維持有人能量";
  if (!values?.length || !values.every(Number.isFinite)) {
    const text = prop?.status === "unavailable" ? "裝置回覆空值" : (prop?.value !== null && prop?.value !== undefined ? "原始資料編碼待確認" : "尚未取得資料");
    chart.append(svgEl("text", { x: 210, y: 76, "text-anchor": "middle", fill: "#687169", "font-size": 12 }, text));
    chart.setAttribute("aria-label", `${label}，${text}`);
    setText(`${prefix}-energy-caption`, prop?.updated_at ? `${timeText(prop.updated_at)} · 可在下方查看原始讀值` : "完成直連並取得能量後更新");
    return;
  }
  const limits = threshold?.series?.length === values.length ? threshold.series : [];
  const low = 0, high = 255;
  const x = (i) => values.length === 1 ? 225 : 36 + i / (values.length - 1) * 366;
  const y = (v) => 122 - (v - low) / (high - low) * 108;
  [14, 68, 122].forEach((line) => chart.append(svgEl("line", { x1: 36, x2: 402, y1: line, y2: line, stroke: "#dce0d3", "stroke-dasharray": "3 5" })));
  values.forEach((v, i) => {
    const enabled = direct.regions?.detect_region?.[i] !== false;
    const line = svgEl("line", { x1: x(i), x2: x(i), y1: y(low), y2: y(v), stroke: !enabled ? "#c3c8be" : (limits.length && v > limits[i] ? "#b05236" : "#58795b"), "stroke-width": Math.min(14, 220 / values.length) });
    line.append(svgEl("title", {}, `${(i * .25).toFixed(2)}–${((i + 1) * .25).toFixed(2)} m · 能量 ${fmt(v)}${limits.length ? ` · 閾值 ${limits[i]}` : ""}${!enabled ? " · 未啟用" : ""}`)); chart.append(line);
  });
  if (limits.length) chart.append(svgEl("polyline", { points: limits.map((v, i) => `${x(i)},${y(v)}`).join(" "), fill: "none", stroke: "#ad8b49", "stroke-width": 1.5 }));
  chart.append(svgEl("text", { x: 3, y: 19, fill: "#687169", "font-size": 10 }, fmt(high)), svgEl("text", { x: 3, y: 125, fill: "#687169", "font-size": 10 }, fmt(low)), svgEl("text", { x: 36, y: 142, fill: "#687169", "font-size": 10 }, "0.25 m"), svgEl("text", { x: 402, y: 142, "text-anchor": "end", fill: "#687169", "font-size": 10 }, "8 m"));
  const fresh = directFresh(prop, direct);
  setText(`${prefix}-energy-caption`, `${timeText(prop.updated_at)} · 32 格 / 0–8 m${limits.length ? " · 金線為閾值" : ""}${!fresh ? " · 最後讀值，等待更新" : " · 即時回報"}`);
  chart.setAttribute("aria-label", `${label}，32 個距離區段，每格 0.25 公尺，最低 ${fmt(Math.min(...values))}，最高 ${fmt(Math.max(...values))}${fresh ? "" : "，最後讀值"}`);
}

function renderZones(d) {
  const kind = $("zone-kind").value;
  const prop = d.properties[kind];
  const fresh = directFresh(d.properties.human_sensor_bitmaps, d);
  if (!$("zone-grid").querySelector("input")) {
    for (let i = 0; i < 32; i++) {
      const cell = el("label", "zone-cell");
      const input = el("input"); input.type = "checkbox"; input.dataset.bin = i;
      input.setAttribute("aria-label", `${(i * .25).toFixed(2)} 到 ${((i + 1) * .25).toFixed(2)} 公尺`);
      cell.append(input, el("span", "", `${((i + 1) * .25).toFixed(2)} m`));
      $("zone-grid").append(cell);
    }
  }
  if (zoneSignature !== kind) { zoneSignature = kind; zoneDirty = false; }
  $("zone-grid").querySelectorAll("input").forEach((input, i) => {
    if (!zoneDirty) input.checked = Boolean(d.regions?.[kind]?.[i]);
    input.closest("label").classList.toggle("hit", fresh && Boolean((d.bitmaps?.occupied >>> i) & 1));
  });
  const count = $("zone-grid").querySelectorAll("input:checked").length;
  setText("zone-summary", `${count} / 32 格${zoneDirty ? " · 尚未套用" : (prop.status !== "read" ? " · 等待本次連線讀值" : " · 裝置目前設定")}`);
}
function calibrationHasChanges(d) {
  const proposed = d?.calibration?.proposed;
  return Boolean(proposed && ["motion_threshold", "presence_threshold"].some(name =>
    proposed[name]?.split(",").some((value, i) => parseInt(value, 16) !== d.properties[name].series?.[i])));
}
function renderCalibration(d) {
  const c = d.calibration, active = ["countdown", "waiting_empty", "scanning", "radar_wait"].includes(c.phase);
  const timer = c.phase === "countdown" || c.phase === "radar_wait" ? `剩餘 ${c.remaining} 秒。` : (c.phase === "scanning" ? `${c.elapsed} / ${c.duration} 秒。` : "");
  const message = c.phase === "preview" && !calibrationHasChanges(d) ? "空房掃描完成，32 格建議與目前閾值相同，可保留原設定。" : (c.message || "連線並取得即時能量後可開始。");
  setText("calibration-reason", `${timer}${message}`);
  $("calibration-progress").hidden = !active;
  $("calibration-progress").value = c.phase === "scanning" ? c.elapsed : 0;
  $("calibration-apply").hidden = c.phase !== "preview" || !calibrationHasChanges(d);
  $("calibration-cancel").hidden = !active && c.phase !== "preview";
  $("calibration-restore").hidden = !c.backup_available && !c.restore_required;
  $("calibration-preview").hidden = !c.proposed || !["preview", "applied"].includes(c.phase);
  const signature = JSON.stringify(c.proposed || null);
  if (signature !== previewSignature) {
    previewSignature = signature;
    const series = (name) => c.proposed?.[name]?.split(",").map(v => parseInt(v, 16));
    const motion = series("motion_threshold"), presence = series("presence_threshold");
    $("calibration-thresholds").replaceChildren(...(motion ? motion.map((value, i) => {
      const row = el("tr");
      row.append(el("td", "", `${(i * .25).toFixed(2)}–${((i + 1) * .25).toFixed(2)} m`), el("td", "", `${d.properties.motion_threshold.series?.[i] ?? "—"} → ${value}`), el("td", "", `${d.properties.presence_threshold.series?.[i] ?? "—"} → ${presence[i]}`));
      return row;
    }) : []));
  }
}
function settingField(prop, name) {
  if (!prop) return;
  const signature = `${name}:${prop.format}:${JSON.stringify(prop.choices)}`;
  if (signature !== settingSignature) {
    settingSignature = signature; settingDirty = false;
    const mask = prop.allowed_mask !== null && prop.allowed_mask !== undefined;
    const choice = mask || prop.choices?.length || prop.format === "bool";
    const control = el(choice ? "select" : "input");
    control.id = domId(device.address, "direct-value"); control.required = true;
    control.setAttribute("aria-describedby", `${domId(device.address, "direct-value-error")} ${domId(device.address, "direct-current")}`);
    if (choice) {
      if (mask) {
        control.multiple = true; control.size = 6; control.required = false;
        [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120].forEach((minute, bit) => control.append(new Option(`${minute} 分鐘`, String(1 << bit))));
      } else control.append(new Option("請選擇", ""));
      if (mask) { /* Minute choices above represent the documented mask bits. */ }
      else if (prop.format === "bool") control.append(new Option("停用", "false"), new Option("啟用", "true"));
      else prop.choices.forEach((label, index) => control.append(new Option(label, String(index))));
    } else { control.type = "number"; control.step = "1"; if (prop.minimum !== null) control.min = String(prop.minimum); if (prop.maximum !== null) control.max = String(prop.maximum); }
    $("direct-value-slot").replaceChildren(control);
    setText("direct-value-label", mask ? "持續時間上報門檻（可複選；全部取消為停用）" : `新設定值${name === "no_one_determine_time" ? "（秒，必填）" : "（必填）"}`);
    $("direct-value-error").hidden = true;
  }
  if (!settingDirty && document.activeElement !== $("direct-value")) {
    if ($("direct-value").multiple) for (const option of $("direct-value").options) option.selected = Boolean((prop.value || 0) & Number(option.value));
    else $("direct-value").value = prop.value === null ? "" : String(prop.value);
  }
}
function directAvailability() {
  const d = controllerFor(latest, device);
  const locked = !connected || paused || latest?.mode === "replay" || controlPosting || Boolean(pendingControl) || d?.busy || !d;
  $("direct-connect").disabled = locked || !d.token_configured || d.authenticated;
  $("direct-refresh").disabled = locked || !d.authenticated;
  $("direct-disconnect").disabled = locked || !canDisconnect(d);
  const prop = d?.properties?.[$("direct-property").value];
  const calibrating = ["countdown", "waiting_empty", "scanning", "radar_wait"].includes(d?.calibration?.phase);
  const ready = !locked && d?.authenticated && d.status === "ready" && !calibrating;
  $("stream-apply").disabled = !ready;
  $("stream-mode").disabled = !ready;
  const writable = ready && prop?.status === "read" && !(prop.allowed_mask !== null && prop.allowed_mask !== undefined && (prop.value & ~prop.allowed_mask));
  $("direct-value").disabled = !writable;
  $("direct-property").disabled = Boolean(d?.busy) || controlPosting || Boolean(pendingControl) || paused;
  $("direct-apply").disabled = !writable || !settingDirty;
  $("direct-refresh").disabled ||= calibrating;
  const zonesWritable = ready && d?.properties?.[$("zone-kind").value]?.status === "read";
  $("zone-grid").disabled = !zonesWritable;
  $("zone-kind").disabled = Boolean(locked || calibrating);
  $("zone-apply").disabled = !zonesWritable || !zoneDirty;
  $("zone-reset").disabled = !zoneDirty;
  const empty = $("calibration-empty").checked;
  $("calibration-start").disabled = !ready || !d?.calibration?.available || !empty;
  $("calibration-radar").disabled = !ready || !empty;
  $("calibration-apply").disabled = !ready || d?.calibration?.phase !== "preview" || !calibrationHasChanges(d);
  $("calibration-cancel").disabled = locked || !d?.authenticated;
  $("calibration-restore").disabled = !ready;
}
function renderDirect(state) {
  const d = controllerFor(state, device);
  if (directSession !== state.started_at) { directSession = state.started_at; pendingControl = null; completedControl = null; controlFeedback = ""; settingDirty = false; zoneDirty = false; }
  if (!d) {
    setText("direct-status", state.mode === "replay" ? "歷史重播，直連已停用" : "未啟用直連");
    setText("direct-auth", ""); setText("direct-message", "即時模式才可連接 ES5BB。此頁的歷史讀值不會送到裝置。");
    directAvailability(); return;
  }
  if (pendingControl && d.last_operation?.id === pendingControl) {
    if (d.last_operation.status !== "pending") { pendingControl = null; controlFeedback = ""; }
    if (d.last_operation.action === "set" && d.last_operation.status === "done") settingDirty = false;
  }
  if (pendingControl && d.last_operation?.id !== pendingControl && !d.busy &&
      Date.now() - pendingControlSince > 2000 && new Date(state.server_time).getTime() > pendingControlSnapshot) {
    pendingControl = null; controlFeedback = "操作紀錄已被較新的操作取代，請重新讀取設定確認結果。";
  }
  setText("direct-status", DIRECT_STATUS[d.status] || d.status);
  setText("direct-auth", d.authenticated ? "會話認證已通過" : (d.token_configured ? "直連憑證已設定 · 尚未認證" : "未設定直連憑證"));
  $("direct-dot").className = `dot${d.status === "error" ? " error" : (d.authenticated ? "" : " pending")}`;
  const operation = d.last_operation;
  if (operation?.status === "done" && completedControl !== operation.id) {
    completedControl = operation.id;
    if (operation.action === "set") settingDirty = false;
    if (operation.action === "set") zoneDirty = false;
  }
  const action = { connect: "連線與讀取", disconnect: "中斷連線", refresh: "讀取屬性", set: "設定並回讀", stream: "切換訊號串流", calibrate: "啟動空房掃描", radar_calibrate: "啟動雷達校正", calibration_cancel: "取消校正", calibration_apply: "套用校正", calibration_restore: "還原備份" }[operation?.action] || "";
  const result = { pending: "進行中", done: "完成", error: "失敗" }[operation?.status] || "";
  setText("direct-operation", operation ? `${action} · ${result} · ${timeText(operation.at)}` : "—");
  const busyText = d.busy ? `${DIRECT_STATUS[d.status] || "處理中"}，請稍候。` : "";
  const readCount = Object.values(d.properties).filter(p => p.status === "read").length;
  const emptyCount = Object.values(d.properties).filter(p => p.status === "unavailable").length;
  const verifiedText = d.authenticated && readCount ? `已完成直連認證，讀取 ${readCount} 個屬性${emptyCount ? `；${emptyCount} 個欄位目前回覆空值` : ""}。設定需另外按下套用。` : "連線時讀取資料，設定需另外按下套用。首次使用需確認實機相容性。";
  const message = controlFeedback || busyText || d.error || (operation?.action === "set" && operation.status === "done" ? "設定值已由裝置回讀確認。" : verifiedText);
  setText("direct-message", message);
  $("direct-message").className = `direct-message${d.error && !controlFeedback && !d.busy ? " error" : ""}`;
  setText("direct-firmware", d.firmware || "尚未讀取");
  const timeout = d.properties.no_one_determine_time;
  setText("direct-timeout", timeout?.value === null ? "尚未讀取" : `${fmt(timeout.value)} 秒${timeout.status === "cached" ? "（上次讀值）" : ""}`);
  renderEnergy("motion", d.properties.motion_energy, d.properties.motion_threshold, d);
  renderEnergy("presence", d.properties.presence_energy, d.properties.presence_threshold, d);
  const region = d.properties.detect_region;
  const bitmapFresh = directFresh(d.properties.human_sensor_bitmaps, d);
  const occupied = d.bitmaps ? Array.from({length: 32}, (_, i) => i).filter(i => Boolean((d.bitmaps.occupied >>> i) & 1)) : [];
  setText("direct-region", d.bitmaps ? `${bitmapFresh ? "目前回報" : "最後回報"}：${occupied.length ? occupied.map(i => `${(i * .25).toFixed(2)}–${((i + 1) * .25).toFixed(2)} m`).join("、") : "沒有區段回報有人"}${region.value !== null ? ` · 啟用 ${d.regions.detect_region.filter(Boolean).length} 格` : ""}` : "等待即時距離位圖");
  const bitmap = d.properties.human_sensor_bitmaps;
  setText("direct-bitmap", d.bitmaps ? `${d.bitmaps.noise_health}（${d.bitmaps.floor_noise}） · ${timeText(bitmap.updated_at)}${!bitmapFresh ? " · 最後讀值" : ""}` : "尚未取得底噪資料");
  renderSignals(d); renderPirEnergy(d); renderRadarTable(d); renderZones(d); renderCalibration(d);
  const catalog = Object.entries(d.properties).filter(([name, prop]) => prop.writable && !["detect_region", "interest_region", "motion_threshold", "presence_threshold"].includes(name));
  const catalogKey = JSON.stringify(catalog.map(([name, prop]) => [name, prop.label]));
  if (directCatalog !== catalogKey) {
    const selected = $("direct-property").value; directCatalog = catalogKey;
    $("direct-property").replaceChildren(...catalog.map(([name, prop]) => new Option(prop.label, name)));
    if (d.properties[selected]?.writable) $("direct-property").value = selected;
  }
  const prop = d.properties[$("direct-property").value];
  settingField(prop, $("direct-property").value);
  setText("direct-current", `裝置目前值：${propertyText(prop)}${prop?.updated_at ? ` · ${timeText(prop.updated_at)}${prop.status !== "read" ? "（上次讀值）" : ""}` : ""}${$("direct-property").value === "pir_threshold" ? " · 調整前請先將紅外線靈敏度設為自訂。" : ""}`);
  const rows = Object.entries(d.properties).map(([name, item]) => {
    const advertised = ["illuminance", "occupancy_close_range"].includes(name);
    const missing = item.value === null || item.value === undefined;
    const row = el("div"), description = el("dd", "", missing && advertised ? "由廣播上報，未開放直讀" : missing && name === "battery_level" ? "優先查看廣播，直讀能力未確認" : propertyText(item));
    description.append(el("small", "", `${item.siid}.${item.piid} · ${item.status === "read" ? timeText(item.updated_at) : ({ unread: "尚未讀取", unavailable: "裝置回覆空值", cached: "上次連線讀值", error: "讀取失敗" }[item.status] || item.status)}`));
    if (item.error) description.append(el("small", "", item.error));
    row.append(el("dt", "", item.label), description); return row;
  });
  $("direct-properties").replaceChildren(...rows);
  $("direct-diagnostics").replaceChildren(...(d.diagnostics.length ? d.diagnostics.map((item) => el("li", "", `${timeText(item.at)} · ${item.message || ({ phase: "連線階段", refresh: "讀取完成", setting_verified: "設定回讀一致" }[item.kind] || item.kind)}${item.phase ? ` · ${DIRECT_STATUS[item.phase] || item.phase}` : ""}`)) : [el("li", "", "尚無連線紀錄")]));
  directAvailability();
}
async function directRequest(command) {
  if (controlPosting || pendingControl || paused || !connected || latest?.mode === "replay") return;
  const requestSession = latest?.started_at;
  controlPosting = true; controlFeedback = "正在送出操作要求…";
  renderDirect(latest);
  try {
    const response = await fetch("/api/es5/direct", { method: "POST", headers: { "Content-Type": "application/json", "X-Linptech-Control": "1" }, body: JSON.stringify({ ...command, address: device.address }), signal: AbortSignal.timeout(5000) });
    const body = await response.json();
    if (latest?.started_at !== requestSession) return;
    if (!response.ok) throw new Error(body.error || "本機程式未接受此操作。");
    pendingControl = body.id; pendingControlSince = Date.now(); pendingControlSnapshot = new Date(latest.server_time).getTime();
    controlFeedback = "本機程式已接受要求，等待裝置回覆。";
  } catch (error) { if (latest?.started_at === requestSession) controlFeedback = error.name === "TimeoutError" ? "送出要求逾時，請查看最近操作結果再重試。" : error.message; }
  finally { controlPosting = false; if (element.isConnected) renderDirect(latest); }
}
$("direct-connect").addEventListener("click", () => directRequest({ action: "connect" }));
$("direct-disconnect").addEventListener("click", () => directRequest({ action: "disconnect" }));
$("direct-refresh").addEventListener("click", () => directRequest({ action: "refresh" }));
$("stream-settings").addEventListener("submit", event => { event.preventDefault(); if (!$("stream-apply").disabled) directRequest({ action: "stream", mode: $("stream-mode").value }); });
$("zone-kind").addEventListener("change", () => { zoneDirty = false; if (latest) renderDirect(latest); });
$("zone-grid").addEventListener("change", () => { zoneDirty = true; if (latest) renderDirect(latest); });
$("zone-reset").addEventListener("click", () => { zoneDirty = false; if (latest) renderDirect(latest); });
$("zone-settings").addEventListener("submit", event => {
  event.preventDefault();
  if ($("zone-apply").disabled) return;
  const value = [...$("zone-grid").querySelectorAll("input")].reduce((mask, input, i) => mask + (input.checked ? 2 ** i : 0), 0);
  if ($("zone-kind").value === "detect_region" && value === 0) { controlFeedback = "請至少選擇一個雷達感應區段。"; renderDirect(latest); return; }
  directRequest({ action: "set", property: $("zone-kind").value, value });
});
$("calibration-empty").addEventListener("change", directAvailability);
$("calibration-start").addEventListener("click", () => directRequest({ action: "calibrate", empty_room: $("calibration-empty").checked }));
$("calibration-radar").addEventListener("click", () => directRequest({ action: "radar_calibrate", empty_room: $("calibration-empty").checked }));
$("calibration-cancel").addEventListener("click", () => directRequest({ action: "calibration_cancel" }));
$("calibration-apply").addEventListener("click", () => directRequest({ action: "calibration_apply" }));
$("calibration-restore").addEventListener("click", () => directRequest({ action: "calibration_restore" }));
$("direct-property").addEventListener("change", () => { settingSignature = ""; if (latest) renderDirect(latest); });
function settingValidation(control) {
  // checkValidity() dispatches another invalid event; inspect ValidityState here.
  const invalid = !control.validity.valid;
  if (invalid) control.setAttribute("aria-invalid", "true"); else control.removeAttribute("aria-invalid");
  $("direct-value-error").hidden = !invalid;
}
$("direct-value-slot").addEventListener("input", () => { settingDirty = true; if ($("direct-value").hasAttribute("aria-invalid")) settingValidation($("direct-value")); directAvailability(); });
$("direct-value-slot").addEventListener("blur", (event) => { if (!event.target.disabled) settingValidation(event.target); }, true);
$("direct-settings").addEventListener("invalid", (event) => settingValidation(event.target), true);
$("direct-settings").addEventListener("submit", (event) => {
  event.preventDefault();
  const control = $("direct-value"), prop = controllerFor(latest, device)?.properties[$("direct-property").value];
  if (control.disabled || !settingDirty || !prop) return;
  settingValidation(control); if (!control.reportValidity()) return;
  const value = control.multiple ? Array.from(control.selectedOptions).reduce((mask, option) => mask | Number(option.value), 0) : prop.format === "bool" ? control.value === "true" : Number(control.value);
  directRequest({ action: "set", property: $("direct-property").value, value });
});
  return { element, model: device.model, render: renderDirect, connection() {
    directAvailability();
    const d = controllerFor(paused && shown ? shown : latest, device);
    if (d) { renderPirEnergy(d); renderSignals(d); }
    const proxy = latest?.transport?.backend === "esphome";
    setText("direct-guide", `保持供電並放近${proxy ? "ESPHome 代理" : "電腦"}。從手機設定關閉藍牙，短按裝置按鍵後連線；請勿長按重設。`);
  } };
}
function renderTrend(device, panel) {
  const $ = id => panel.querySelector(`[id="${domId(device.address, id)}"]`);
  $("trend-latest").textContent = `${fmt(metric(device, "temperature")?.value)} °C`;

  const points = temperatureSeries(device);
  const chart = $("trend-chart");
  chart.replaceChildren();
  [20, 65, 110].forEach((y) => chart.append(svgEl("line", { x1: 40, y1: y, x2: 780, y2: y, stroke: "#dce0d3", "stroke-dasharray": "3 5" })));
  if (!points.length) {
    chart.append(svgEl("text", { x: 410, y: 70, "text-anchor": "middle", fill: "#687169", "font-size": 13 }, "等待第一筆溫度廣播"));
    chart.setAttribute("aria-label", "本次溫度趨勢，尚無資料");
    $("trend-range").textContent = "本次監聽";
    $("trend-caption").textContent = "等待溫度廣播";
    return;
  }
  const low = Math.min(...points.map((p) => p.value)), high = Math.max(...points.map((p) => p.value));
  const min = low - .3, max = high + .3;
  const firstTime = new Date(points[0].timestamp).getTime(), lastTime = new Date(points.at(-1).timestamp).getTime();
  const mapped = points.map((point) => ({ x: lastTime === firstTime ? 410 : 40 + (new Date(point.timestamp).getTime() - firstTime) / (lastTime - firstTime) * 740, y: 110 - (point.value - min) / (max - min) * 90 }));
  if (mapped.length > 1) chart.append(svgEl("polyline", { points: mapped.map((p) => `${p.x},${p.y}`).join(" "), fill: "none", stroke: "#3b6850", "stroke-width": 2, "stroke-linejoin": "round" }));
  mapped.forEach((p, i) => { const circle = svgEl("circle", { cx: p.x, cy: p.y, r: i === mapped.length - 1 ? 4 : 2, fill: "#3b6850" }); circle.append(svgEl("title", {}, `${timeText(points[i].timestamp)} · ${fmt(points[i].value)} °C`)); chart.append(circle); });
  chart.append(svgEl("text", { x: 2, y: 24, fill: "#687169", "font-size": 10 }, fmt(max)), svgEl("text", { x: 2, y: 114, fill: "#687169", "font-size": 10 }, fmt(min)));
  $("trend-range").textContent = `${timeText(points[0].timestamp)} — ${timeText(points.at(-1).timestamp)}`;
  $("trend-caption").textContent = `${fmt(low)}–${fmt(high)} °C · ${points.length} 筆實際讀值`;
  chart.setAttribute("aria-label", `溫度趨勢：${points.length} 筆讀值，最低 ${fmt(low)}，最高 ${fmt(high)} 攝氏度；最新 ${fmt(points.at(-1).value)} 攝氏度`);
}
let sensorSession = null;
const sensorOperations = new Map();
function sensorOperation(model) {
  if (!sensorOperations.has(model)) sensorOperations.set(model, { posting: false, pending: null, pendingSince: 0, pendingSnapshot: 0, completed: null, feedback: "", signature: "", dirty: false });
  return sensorOperations.get(model);
}
function sensorUi(address, id) { return $(domId(address, id)); }
function renderSensorSettings(state) {
  if (sensorSession !== state.started_at) { sensorSession = state.started_at; sensorOperations.clear(); }
  state.devices.filter(device => device.model !== "ES5BB").forEach(device => renderSensorPanel(device, state));
}
function renderSensorPanel(device, state) {
  const { address, model } = device;
  const ui = id => sensorUi(address, id), d = controllerFor(state, device), op = sensorOperation(address);
  if (op.pending && d?.last_operation?.id === op.pending && d.last_operation.status !== "pending") { op.pending = null; op.feedback = ""; }
  if (op.pending && d?.last_operation?.id !== op.pending && !d?.busy &&
      Date.now() - op.pendingSince > 2000 && new Date(state.server_time).getTime() > op.pendingSnapshot) {
    op.pending = null; op.feedback = "操作紀錄已被較新操作取代，請重新讀取設定確認。";
  }
  if (d?.last_operation?.status === "done" && d.last_operation.id !== op.completed) { op.completed = d.last_operation.id; op.dirty = false; }
  const locked = !connected || paused || state.mode === "replay" || op.posting || Boolean(op.pending) || d?.busy || !d;
  ui("sensor-connect").disabled = locked || !d?.supported || !d.token_configured || d.authenticated;
  ui("sensor-refresh").disabled = locked || !d?.authenticated;
  ui("sensor-disconnect").disabled = locked || !canDisconnect(d);
  ui("sensor-panel").querySelector(".stream-settings").hidden = model === "MS1BB";
  ui("sensor-settings-form").hidden = model === "MS1BB";
  ui("sensor-settings-status").textContent = op.feedback || d?.error || (model === "MS1BB" ? "僅廣播 · 無開放的直連設定" : d ? `${DIRECT_STATUS[d.status] || d.status}${d.last_operation ? ` · ${{ pending: "操作進行中", done: "操作完成", error: "操作失敗" }[d.last_operation.status] || ""}` : ""}` : "重播模式 · 直連停用");
  ui("sensor-settings-note").textContent = d ? `${model === "HS3BB" ? "短按裝置按鈕後立即連線。 " : ""}${d.note}${!d.token_configured && d.supported ? " 缺少直連 token，請使用 set-token 保存。" : ""}${d.model_confirmed === false ? " 寫入前須核對協定型號 linp.motion.hs1bb2。" : ""}` : "重播資料不會寫入裝置。";
  const catalog = Object.entries(d?.properties || {});
  const catalogSignature = `${model}:${catalog.map(([name]) => name).join(",")}`;
  if (ui("sensor-property").dataset.catalog !== catalogSignature) {
    ui("sensor-property").dataset.catalog = catalogSignature;
    ui("sensor-property").replaceChildren(...catalog.map(([name, p]) => new Option(p.label, name)));
    op.signature = ""; op.dirty = false;
  }
  const name = ui("sensor-property").value, prop = d?.properties?.[name];
  const signature = `${model}:${name}`;
  if (signature !== op.signature) {
    op.signature = signature; op.dirty = false;
    const choice = prop?.choices?.length || prop?.format === "bool", input = el(choice ? "select" : "input");
    input.id = domId(address, "sensor-value"); input.name = "value"; input.required = true;
    input.setAttribute("aria-describedby", domId(address, "sensor-current"));
    if (choice) {
      input.append(new Option("請選擇", ""));
      if (prop.format === "bool") input.append(new Option("停用", "false"), new Option("啟用", "true"));
      else prop.choices.forEach((label, index) => input.append(new Option(label, String(index))));
    } else { input.type = "number"; input.step = String(prop?.step || 1); if (prop?.minimum !== null && prop?.minimum !== undefined) input.min = String(prop.minimum); if (prop?.maximum !== null && prop?.maximum !== undefined) input.max = String(prop.maximum); }
    ui("sensor-value-slot").replaceChildren(input);
  }
  const input = ui("sensor-value"), ready = !locked && d?.authenticated && d.status === "ready" && d.model_confirmed !== false;
  if (!op.dirty && document.activeElement !== input) input.value = prop?.value === null || prop?.value === undefined ? "" : String(prop.value);
  input.disabled = !ready || prop?.status !== "read";
  ui("sensor-property").disabled = locked || !catalog.length;
  ui("sensor-apply").disabled = input.disabled || !op.dirty;
  ui("sensor-current").textContent = prop ? `目前值：${propertyText(prop)}${prop.unit ? ` ${prop.unit}` : ""} · ${prop.updated_at ? timeText(prop.updated_at) : "尚未讀取"}${prop.status !== "read" ? ` · ${prop.status}` : ""}` : "沒有可調整的直連設定";
  ui("sensor-properties").replaceChildren(...catalog.map(([, p]) => {
    const row = el("div"), value = el("dd", "", `${propertyText(p)}${p.unit ? ` ${p.unit}` : ""}`);
    value.append(el("small", "", `${p.siid}.${p.piid} · ${p.status}${p.error ? ` · ${p.error}` : ""}`)); row.append(el("dt", "", p.label), value); return row;
  }));
}
async function sensorRequest(device, command) {
  const { address, model } = device;
  const op = sensorOperation(address);
  if (op.posting || op.pending || paused || !connected) return;
  const requestSession = latest?.started_at;
  op.posting = true; op.feedback = "正在送出要求…"; renderSensorSettings(latest);
  try {
    const response = await fetch("/api/sensor/direct", { method: "POST", headers: { "Content-Type": "application/json", "X-Linptech-Control": "1" }, body: JSON.stringify({ ...command, model, address }), signal: AbortSignal.timeout(5000) });
    const body = await response.json(); if (latest?.started_at !== requestSession) return;
    if (!response.ok) throw new Error(body.error || "本機程式未接受操作。");
    op.pending = body.id; op.pendingSince = Date.now(); op.pendingSnapshot = new Date(latest.server_time).getTime();
    op.feedback = "已接受要求，等待裝置認證或回讀。";
  } catch (error) { op.feedback = error.name === "TimeoutError" ? "送出逾時，請先查看操作結果。" : error.message; }
  finally { op.posting = false; renderSensorSettings(latest); }
}
function createSensorPanel(device) {
  const { address } = device;
  const element = clonePanel("sensor-settings-template", address);
  const ui = id => sensorUi(address, id);
  ui("sensor-property").addEventListener("change", () => { sensorOperation(address).signature = ""; if (latest) renderSensorSettings(latest); });
  ui("sensor-value-slot").addEventListener("input", () => { sensorOperation(address).dirty = true; if (latest) renderSensorSettings(latest); });
  ui("sensor-connect").addEventListener("click", () => sensorRequest(device, { action: "connect" }));
  ui("sensor-refresh").addEventListener("click", () => sensorRequest(device, { action: "refresh" }));
  ui("sensor-disconnect").addEventListener("click", () => sensorRequest(device, { action: "disconnect" }));
  ui("sensor-settings-form").addEventListener("submit", event => {
    event.preventDefault(); const input = ui("sensor-value"), prop = controllerFor(latest, device)?.properties?.[ui("sensor-property").value];
    if (ui("sensor-apply").disabled || !prop || !input.reportValidity()) return;
    sensorRequest(device, { action: "set", property: ui("sensor-property").value, value: prop.format === "bool" ? input.value === "true" : Number(input.value) });
  });
  return { element, model: device.model };
}
function renderObjects(state) {
  const statusLabels = { decoded: "已解析", unknown_object: "未知物件", unknown_enum: "未知列舉", outside_spec_range: "超出規格", unauthenticated: "未認證" };
  state.devices.forEach(device => {
    const section = advancedPanels.get(device.address).host.querySelector(".decode-device"); section.replaceChildren();
    const objects = Object.values(device.objects || {});
    if (!objects.length) section.append(el("p", "", "本輪尚未收到可列出的物件。"));
    for (const item of objects) {
      const value = item.value === undefined ? item.raw_hex || "空值" : typeof item.value === "object" ? JSON.stringify(item.value) : String(item.value);
      const miot = item.miot ? ` · ${item.miot.siid}.${item.miot.piid ?? `e${item.miot.eiid}`}` : "";
      section.append(el("p", "decode-object", `${item.object_id_hex} · ${item.label || item.key || "未識別欄位"}${miot} · ${value} · ${statusLabels[item.status] || item.status} · ${timeText(item.updated_at)}`));
    }
  });
}
function radioQualityText(quality) { return { strong: "強", fair: "穩定", weak: "偏弱", poor: "微弱", unknown: "無近期樣本" }[quality]; }
function renderRadio(state) {
  const devices = visibleDevices(state);
  const receivers = [...new Set(state.devices.flatMap(device => Object.keys(device.radio?.receivers || {})))].sort();
  syncOptions($("receiver-filter"), [["auto", "各裝置最近的接收器"], ...receivers.map(receiver => [receiver, receiver === "local" ? "本機藍牙" : receiver])]);
  const requested = $("receiver-filter").value;
  const rows = devices.map(device => ({ device, ...radioSelection(device, state, requested) }));
  setText("radio-total", String(rows.length));
  setText("radio-observed", String(rows.filter(row => row.summary.count > 0).length));
  setText("radio-good", String(rows.filter(row => row.summary.count > 0 && row.summary.median >= -75).length));
  setText("radio-poor", String(rows.filter(row => row.summary.count > 0 && row.summary.median < -90).length));
  setText("radio-mode", !connected ? "主機離線 · 保留最後讀值" : paused ? "畫面暫停" : state.mode === "replay" ? "歷史資料 · 非現場訊號" : "持續接收 · 5 分鐘觀察窗");
  if (!devices.some(device => device.address === selectedRadioDevice))
    selectedRadioDevice = rows.find(row => row.points.length)?.device.address || devices[0]?.address || null;
  syncOptions($("signal-device"), devices.map(device => [device.address, deviceLabel(device)]));
  if (selectedRadioDevice) $("signal-device").value = selectedRadioDevice;
  $("signal-export").disabled = !rows.some(row => row.points.length);
  const tableRows = rows.map(row => {
    const { device, summary, receiver } = row;
    let tr = radioRowNodes.get(device.address);
    if (!tr) {
      tr = el("tr");
      const identity = el("th"); identity.scope = "row";
      const button = el("button", "sensor-select"); button.type = "button";
      button.addEventListener("click", () => { selectedRadioDevice = device.address; renderRadio(shown || state); });
      identity.append(button, el("small"));
      tr.append(identity, ...Array.from({ length: 7 }, () => el("td")));
      radioRowNodes.set(device.address, tr);
    }
    tr.className = device.address === selectedRadioDevice ? "signal-row-selected" : "";
    const button = tr.querySelector("button"); button.textContent = device.name;
    button.setAttribute("aria-pressed", String(device.address === selectedRadioDevice));
    tr.querySelector("small").textContent = device.address.slice(-8);
    const latestCell = tr.cells[2], quality = qualityForRssi(summary.latest); latestCell.replaceChildren();
    latestCell.append(el("strong", "", summary.latest === null ? "—" : `${fmt(summary.latest)} dBm`));
    const badge = el("span", "quality-tag", radioQualityText(quality)); badge.dataset.quality = quality;
    latestCell.append(badge);
    const values = { 1: device.model, 3: summary.median === null ? "—" : `${fmt(summary.median)} dBm`,
      4: summary.min === null ? "—" : `${fmt(summary.min)}…${fmt(summary.max)}`, 5: String(summary.count),
      6: row.lastObserved ? ago(row.lastObserved, state.server_time) : "尚未收到", 7: receiver === "local" ? "本機藍牙" : receiver || "—" };
    Object.entries(values).forEach(([index, value]) => { tr.cells[index].textContent = value; });
    return tr;
  });
  const body = $("signal-rows"), retained = new Set(tableRows);
  [...body.children].forEach(node => { if (!retained.has(node)) node.remove(); });
  tableRows.forEach((node, index) => { if (body.children[index] !== node) body.insertBefore(node, body.children[index] || null); });
  if (!rows.length) {
    const tr = el("tr"), td = el("td", "empty", "沒有符合條件的裝置"); td.colSpan = 8; tr.append(td); $("signal-rows").append(tr);
  }
  renderRadioChart(rows.find(row => row.device.address === selectedRadioDevice), state);
}
function renderRadioChart(row, state) {
  const chart = $("rssi-chart"); chart.replaceChildren();
  const points = row?.points || [];
  setText("radio-chart-title", row ? `${row.device.name} · RSSI` : "RSSI 走勢");
  if (!points.length) {
    chart.append(svgEl("text", { x: 400, y: 120, "text-anchor": "middle", fill: "#68746b", "font-size": 14 }, "此接收器近 5 分鐘尚無樣本"));
    chart.setAttribute("aria-label", "RSSI 走勢，近 5 分鐘尚無樣本");
    setText("rssi-caption", "等待實際廣播；不補值或推算距離。"); return;
  }
  const start = Date.parse(state.server_time) - 300000, low = Math.min(-110, ...points.map(point => point.rssi - 5));
  const high = Math.max(-30, ...points.map(point => point.rssi + 5));
  const x = timestamp => 50 + (Date.parse(timestamp) - start) / 300000 * 730;
  const y = value => 200 - (value - low) / (high - low) * 180;
  [-60, -75, -90].forEach(level => {
    chart.append(svgEl("line", { x1: 50, x2: 780, y1: y(level), y2: y(level), stroke: level === -75 ? "#8eaa78" : "#dce2d8", "stroke-dasharray": "4 5" }),
      svgEl("text", { x: 4, y: y(level) + 4, fill: "#68746b", "font-size": 11 }, String(level)));
  });
  // Only join neighboring observed samples. Long silent periods remain gaps.
  let segment = [];
  const flush = () => { if (segment.length > 1) chart.append(svgEl("polyline", { points: segment.join(" "), fill: "none", stroke: "#356b4b", "stroke-width": 2 })); segment = []; };
  points.forEach((point, i) => {
    if (i && Date.parse(point.timestamp) - Date.parse(points[i - 1].timestamp) > 30000) flush();
    segment.push(`${x(point.timestamp)},${y(point.rssi)}`);
    const dot = svgEl("circle", { cx: x(point.timestamp), cy: y(point.rssi), r: 2.5, fill: "#356b4b" });
    dot.append(svgEl("title", {}, `${timeText(point.timestamp)} · ${point.rssi} dBm · ${point.receiver}`)); chart.append(dot);
  }); flush();
  chart.append(svgEl("text", { x: 50, y: 228, fill: "#68746b", "font-size": 11 }, timeText(new Date(start).toISOString())),
    svgEl("text", { x: 780, y: 228, "text-anchor": "end", fill: "#68746b", "font-size": 11 }, timeText(state.server_time)));
  setText("rssi-caption", `${row.receiver === "local" ? "本機藍牙" : row.receiver} · ${points.length} 筆實際樣本 · 中位數 ${fmt(row.summary.median)} dBm · 最大樣本間隔 ${fmt(row.summary.max_gap_seconds)} 秒`);
  chart.setAttribute("aria-label", `${row.device.name}，接收器 ${row.receiver}，${points.length} 筆 RSSI 樣本，最低 ${fmt(row.summary.min)}、最高 ${fmt(row.summary.max)} dBm`);
}
function exportRadio() {
  const state = paused && shown ? shown : latest;
  if (!state) return;
  const rows = [["timestamp", "name", "model", "address", "receiver", "rssi_dbm"]];
  for (const device of visibleDevices(state)) for (const point of radioSelection(device, state, $("receiver-filter").value).points)
    rows.push([point.timestamp, device.name, device.model, device.address, point.receiver, point.rssi]);
  const url = URL.createObjectURL(new Blob(["\ufeff" + rows.map(row => row.map(csvCell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" }));
  const link = el("a"); link.href = url; link.download = "linptech-rssi.csv"; link.click(); URL.revokeObjectURL(url);
}
function renderView() {
  const signals = window.location.hash === "#signals";
  $("device-view").hidden = signals; $("signal-view").hidden = !signals;
  for (const [id, active] of [["devices-link", !signals], ["signals-link", signals]]) {
    if (active) $(id).setAttribute("aria-current", "page"); else $(id).removeAttribute("aria-current");
  }
}
function render(state) {
  shown = state;
  $("total-devices").textContent = String(state.devices.length).padStart(2, "0");
  $("verified-devices").textContent = `${state.totals.verified} / ${state.devices.length}`;
  $("packet-count").textContent = state.totals.packets.toLocaleString("zh-TW");
  $("event-count").textContent = state.totals.events.toLocaleString("zh-TW");
  renderCards(state);
  renderFeed(state);
  state.devices.forEach(device => {
    if (device.model === "ES5BB") advancedPanels.get(device.address).render(state);
    if (device.model === "KS2BB") renderTrend(device, temperaturePanels.get(device.address));
  });
  renderSensorSettings(state); renderObjects(state); renderRadio(state);
  $("recording-info").textContent = state.mode === "replay" ? "歷史重播" : "紀錄中 · captures/";
  const newest = state.events[0];
  if (initializedSession !== state.started_at) { initializedSession = state.started_at; lastAnnouncedId = newest?.id || 0; }
  if (newest && newest.id > lastAnnouncedId) { $("announcer").textContent = `${newest.model}，${newest.text}`; lastAnnouncedId = newest.id; }
}
function renderConnection() {
  const status = latest?.scanner.status;
  const labels = { starting: "正在啟動藍牙接收", listening: "藍牙廣播監聽中", stopped: "藍牙監聽已停止", replaying: "離線重播中", replay_complete: "離線重播完成", error: "接收程式發生異常" };
  $("connection-text").textContent = connected ? (labels[status] || "已連接本機監控台") : "連線中斷，正在重新連線";
  $("connection-dot").className = `dot${!connected || status === "error" ? " error" : (status !== "listening" ? " pending" : "")}`;
  const viaProxy = latest?.transport?.backend === "esphome";
  $("mode-label").textContent = latest?.mode === "replay" ? "歷史資料 · 非即時感測" : (viaProxy ? "ESPHome 網路代理 · 無需小米網關" : "本機藍牙 · 無需小米網關");
  const mqtt = latest?.mqtt;
  const mqttStatus = { connecting: "連線中", connected: "已連線", reconnecting: "重連中", error: "連線異常", stopped: "已停止" }[mqtt?.status] || "等待連線";
  const bridgeText = [viaProxy ? `接收代理：${latest.transport.proxy_names?.join("、") || "等待設定"} · ES5BB 直連：${latest.transport.gatt_proxy || "請指定代理"}` : "本機藍牙接收", `HA MQTT：${mqtt?.enabled ? mqttStatus : "未啟用"}`].join(" ｜ ");
  $("bridge-status").hidden = !viaProxy && !mqtt?.enabled;
  setText("bridge-status", `${!connected ? "主機連線中斷 · 最後設定：" : ""}${bridgeText}${mqtt?.error ? ` · ${mqtt.error}` : ""}`);
  const message = !connected ? "網頁與本機程式的連線中斷；畫面保留最後收到的資料，連線恢復後會自動同步。" : (latest?.scanner.error || (paused ? "畫面已暫停；後台持續接收並保存廣播。按「繼續顯示」即可同步最新狀態。" : (latest?.mode === "replay" ? "這是歷史廣播的離線重播，時間與數值不代表裝置目前狀態。" : "")));
  $("notice").textContent = message; $("notice").hidden = !message;
  $("notice").className = `notice${!connected || status === "error" ? " error" : ""}`;
  advancedPanels.forEach(panel => panel.connection?.());
  if (shown || latest) renderCards(paused && shown ? shown : latest);
  if (latest) renderSensorSettings(paused && shown ? shown : latest);
  if (shown || latest) renderRadio(paused && shown ? shown : latest);
  updateManagementAvailability();
}
function receive(state) { latest = state; connected = true; ensureCards(state); renderConnection(); if (!paused) render(state); }
$("pause-button").addEventListener("click", () => {
  paused = !paused;
  $("pause-button").setAttribute("aria-pressed", String(paused));
  $("pause-button").textContent = paused ? "繼續顯示 ▶" : "暫停畫面 Ⅱ";
  if (!paused && latest) render(latest);
  renderConnection();
});
$("device-filters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  filter = button.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((node) => node.setAttribute("aria-pressed", String(node === button)));
  if (shown) renderFeed(shown);
});
$("event-kind").addEventListener("change", () => { if (shown) renderFeed(shown); });
$("device-toolbar").addEventListener("submit", event => event.preventDefault());
for (const id of ["device-search", "model-filter", "instance-filter", "radio-filter"])
  $(id).addEventListener(id === "device-search" ? "input" : "change", () => { if (shown) render(shown); });
$("clear-filters").addEventListener("click", () => { $("device-search").value = ""; ["model-filter", "instance-filter", "radio-filter"].forEach(id => { $(id).value = "all"; }); if (shown) render(shown); });
$("receiver-filter").addEventListener("change", () => { if (shown) renderRadio(shown); });
$("signal-device").addEventListener("change", () => { selectedRadioDevice = $("signal-device").value; if (shown) renderRadio(shown); });
$("signal-export").addEventListener("click", exportRadio);
$("add-device").addEventListener("click", () => { if (!$("add-device").disabled) void openManagement(null, $("add-device")); });
$("drawer-close").addEventListener("click", () => $("device-drawer").close());
$("device-drawer").addEventListener("close", () => {
  releaseDrawerContent(); $("drawer-content").replaceChildren();
  if (drawerReturnFocus?.isConnected) drawerReturnFocus.focus(); else $("add-device").focus();
});
window.addEventListener("hashchange", renderView); renderView();
$("help-button").addEventListener("click", () => $("help-dialog").showModal());
$("close-help").addEventListener("click", () => $("help-dialog").close());
function tick() { const now = new Date(); $("clock").textContent = timeFormat.format(now); $("clock").dateTime = now.toISOString(); $("today").textContent = dateFormat.format(now); }
tick(); setInterval(tick, 1000);
if ("EventSource" in window) {
  let source = null;
  let lastHeartbeat = Date.now();
  function openStream() {
    source?.close();
    lastHeartbeat = Date.now();
    source = new EventSource("/api/stream");
    source.addEventListener("snapshot", (event) => { try { receive(JSON.parse(event.data)); lastHeartbeat = Date.now(); } catch { connected = false; renderConnection(); } });
    source.addEventListener("error", () => { connected = false; renderConnection(); });
  }
  openStream();
  // A stalled connection or an HTTP failure can leave EventSource closed.
  // Heartbeats arrive even when all sensors are quiet; sensor silence is separate.
  const watchdog = setInterval(() => {
    if (Date.now() - lastHeartbeat > 7000 || source.readyState === EventSource.CLOSED) {
      connected = false; renderConnection(); openStream();
    }
  }, 3000);
  window.addEventListener("pagehide", () => { clearInterval(watchdog); source.close(); }, { once: true });
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
} else {
  const poll = async () => { try { const response = await fetch("/api/state", { cache: "no-store" }); if (!response.ok) throw new Error("state"); receive(await response.json()); } catch { connected = false; renderConnection(); } };
  poll(); setInterval(poll, 2000);
}
