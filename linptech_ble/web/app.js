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
let latest = null;
let shown = null;
let paused = false;
let connected = false;
let filter = "all";
let lastAnnouncedId = 0;
let initializedSession = null;

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
function reading(label, entry, unit, valueOverride) {
  const node = el("div", "reading");
  node.append(el("span", "reading-label", label), el("span", "reading-value", entry ? (valueOverride ?? `${fmt(entry.value)}${unit ? " " + unit : ""}`) : "尚未上報"), el("span", "reading-age", entry ? timeText(entry.updated_at) : "—"));
  return node;
}
function card(device, index, state) {
  const card = el("article", `sensor-card${device.model === "ES5BB" ? " featured" : ""}`);
  card.dataset.model = device.model;
  card.setAttribute("aria-label", `${device.model} ${TYPES[device.model] || device.name}`);
  const top = el("div", "card-top");
  const title = el("div");
  const model = el("h3", "model-line", device.model);
  model.prepend(el("span", "sensor-number", String(index + 1).padStart(2, "0")));
  title.append(model, el("p", "card-type", TYPES[device.model] || device.name));
  title.title = device.name;
  const freshness = device.last_error ? "failed" : device.freshness;
  const freshText = device.last_error ? "接收異常" : ({ recent: "近期有廣播", waiting: "等待廣播", stale: "較久未更新" }[freshness]);
  top.append(title, el("span", `freshness ${freshness}`, freshText));
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
  if (unit) big.append(el("span", "value-unit", unit));
  primary.append(big, el("p", "primary-updated", unknown ? `最後回報：${lastValue} · ${timeText(source.updated_at)}` : updated(source)));
  const art = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  art.setAttribute("viewBox", "0 0 72 72"); art.setAttribute("class", "sensor-art"); art.setAttribute("aria-hidden", "true");
  art.setAttribute("fill", "none"); art.setAttribute("stroke", "currentColor"); art.setAttribute("stroke-width", "1.3"); art.setAttribute("stroke-linecap", "round");
  art.innerHTML = ART[device.model] || ""; // Static drawings only; received data uses textContent.
  main.append(primary, art);
  const readings = el("div", "card-readings");
  if (device.model === "ES5BB") {
    const near = binary(device, "occupancy_close_range");
    const nearValue = near ? `${unconfirmed(near, state) ? "最後：" : ""}${near.value ? "有人" : "無人"}` : undefined;
    readings.append(reading("環境照度", metric(device, "illuminance"), "lux"), reading("近距區間", near, "", nearValue));
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
    const verification = el("div", "reading");
    verification.append(el("span", "reading-label", "本次解密"), el("span", "reading-value", device.verified ? "已驗證" : (device.key_configured ? "等待驗證" : "未設定密鑰")), el("span", "reading-age", device.last_decoded ? timeText(device.last_decoded) : "—"));
    readings.append(verification);
  }
  if (["ES5BB", "KS2BB"].includes(device.model)) readings.append(reading("電池電量", metric(device, "battery"), "%"));
  // Show additional reported fields too, without manufacturing unreported data.
  const visible = new Set(["temperature", "occupancy", "door", "opening", "motion", "illuminance", "humidity", "occupancy_close_range", "battery"]);
  for (const entry of Object.values(device.measurements)) {
    if (!visible.has(entry.key)) readings.append(reading(FIELD_NAMES[entry.key] || entry.key, entry, entry.unit === "lx" ? "lux" : entry.unit));
  }
  for (const entry of Object.values(device.binary_sensors)) {
    if (!visible.has(entry.key)) readings.append(reading(FIELD_NAMES[entry.key] || entry.key, entry, "", entry.value ? "觸發" : "未觸發"));
  }
  const meta = el("div", "card-meta");
  meta.append(el("span", "", `RSSI ${device.rssi === null ? "—" : device.rssi + " dBm"}`), el("span", "", `廣播 ${ago(device.last_seen, state.server_time)}`));
  meta.title = `MAC ${device.address}；已接收 ${device.packets} 包，成功解析 ${device.decoded} 包`;
  card.append(top, main, readings, meta);
  if (device.model === "ES5BB") card.append(el("p", "card-caption", "離開後需等待裝置回報無人；雷達與設定請見下方直連工作台。"));
  if (device.last_error) card.append(el("p", "card-error", device.last_error));
  return card;
}
function renderFeed(state) {
  const kind = $("event-kind").value;
  const entries = state.events.filter((event) => (filter === "all" || event.model === filter) && (kind === "all" || event.kind === kind));
  const nodes = entries.map((event) => {
    const item = el("li"); item.dataset.kind = event.kind;
    const details = el("div");
    const translated = event.text.replace(/^([a-z_]+) · /, (match, key) => FIELD_NAMES[key] ? `${FIELD_NAMES[key]} · ` : match);
    details.append(el("span", "event-model", event.model), el("span", "event-text", translated));
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
    control.id = "direct-value"; control.required = true;
    control.setAttribute("aria-describedby", "direct-value-error direct-current");
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
  const d = latest?.direct;
  const locked = !connected || paused || controlPosting || Boolean(pendingControl) || d?.busy || !d;
  $("direct-connect").disabled = locked || !d.token_configured || d.authenticated;
  $("direct-refresh").disabled = locked || !d.authenticated;
  $("direct-disconnect").disabled = locked || !d.authenticated;
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
  const d = state.direct;
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
  const rows = Object.entries(d.properties).map(([, item]) => {
    const row = el("div"), description = el("dd", "", propertyText(item));
    description.append(el("small", "", `${item.siid}.${item.piid} · ${item.status === "read" ? timeText(item.updated_at) : ({ unread: "尚未讀取", unavailable: "裝置回覆空值", cached: "上次連線讀值", error: "讀取失敗" }[item.status] || item.status)}`));
    if (item.error) description.append(el("small", "", item.error));
    row.append(el("dt", "", item.label), description); return row;
  });
  $("direct-properties").replaceChildren(...rows);
  $("direct-diagnostics").replaceChildren(...(d.diagnostics.length ? d.diagnostics.map((item) => el("li", "", `${timeText(item.at)} · ${item.message || ({ phase: "連線階段", refresh: "讀取完成", setting_verified: "設定回讀一致" }[item.kind] || item.kind)}${item.phase ? ` · ${DIRECT_STATUS[item.phase] || item.phase}` : ""}`)) : [el("li", "", "尚無連線紀錄")]));
  directAvailability();
}
async function directRequest(command) {
  if (controlPosting || paused || !connected) return;
  const requestSession = latest?.started_at;
  controlPosting = true; controlFeedback = "正在送出操作要求…";
  renderDirect(latest);
  try {
    const response = await fetch("/api/es5/direct", { method: "POST", headers: { "Content-Type": "application/json", "X-Linptech-Control": "1" }, body: JSON.stringify(command), signal: AbortSignal.timeout(5000) });
    const body = await response.json();
    if (latest?.started_at !== requestSession) return;
    if (!response.ok) throw new Error(body.error || "本機程式未接受此操作。");
    pendingControl = body.id; pendingControlSince = Date.now(); pendingControlSnapshot = new Date(latest.server_time).getTime();
    controlFeedback = "本機程式已接受要求，等待裝置回覆。";
  } catch (error) { if (latest?.started_at === requestSession) controlFeedback = error.name === "TimeoutError" ? "送出要求逾時，請查看最近操作結果再重試。" : error.message; }
  finally { controlPosting = false; renderDirect(latest); }
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
  const control = $("direct-value"), prop = latest?.direct?.properties[$("direct-property").value];
  if (control.disabled || !settingDirty || !prop) return;
  settingValidation(control); if (!control.reportValidity()) return;
  const value = control.multiple ? Array.from(control.selectedOptions).reduce((mask, option) => mask | Number(option.value), 0) : prop.format === "bool" ? control.value === "true" : Number(control.value);
  directRequest({ action: "set", property: $("direct-property").value, value });
});
function renderTrend(state) {
  const device = state.devices.find((item) => item.model === "KS2BB");
  $("trend-temp").replaceChildren(document.createTextNode(fmt(metric(device, "temperature")?.value)), el("small", "", " °C"));
  $("trend-humidity").replaceChildren(document.createTextNode(fmt(metric(device, "humidity")?.value)), el("small", "", " %"));
  const points = (device?.history.temperature || []).filter((point) => Number.isFinite(point.value));
  const chart = $("trend-chart");
  chart.replaceChildren();
  [20, 65, 110].forEach((y) => chart.append(svgEl("line", { x1: 40, y1: y, x2: 780, y2: y, stroke: "#dce0d3", "stroke-dasharray": "3 5" })));
  if (!points.length) {
    chart.append(svgEl("text", { x: 410, y: 70, "text-anchor": "middle", fill: "#687169", "font-size": 13 }, "等待第一筆溫度廣播"));
    chart.setAttribute("aria-label", "本次溫度趨勢，尚無資料");
    $("trend-range").textContent = "等待 KS2BB 溫度廣播";
    $("trend-caption").textContent = "收到實際溫度資料後繪製；不補值、不推算。";
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
  $("trend-caption").textContent = `溫度範圍 ${fmt(low)}–${fmt(high)} °C · 最近 ${points.length} 筆廣播讀值（最多 180 筆）。數值更新也可在即時紀錄中查閱。`;
  chart.setAttribute("aria-label", `溫度趨勢：${points.length} 筆讀值，最低 ${fmt(low)}，最高 ${fmt(high)} 攝氏度；最新 ${fmt(points.at(-1).value)} 攝氏度`);
}
let sensorSignature = "", sensorDirty = false, sensorSession = null;
const sensorOperations = new Map();
function sensorOperation(model) {
  if (!sensorOperations.has(model)) sensorOperations.set(model, { posting: false, pending: null, pendingSince: 0, pendingSnapshot: 0, completed: null, feedback: "" });
  return sensorOperations.get(model);
}
function renderSensorSettings(state) {
  if (sensorSession !== state.started_at) { sensorSession = state.started_at; sensorOperations.clear(); sensorSignature = ""; sensorDirty = false; }
  const model = $("sensor-model").value, d = state.sensor_direct?.[model], op = sensorOperation(model);
  if (op.pending && d?.last_operation?.id === op.pending && d.last_operation.status !== "pending") { op.pending = null; op.feedback = ""; }
  if (op.pending && d?.last_operation?.id !== op.pending && !d?.busy &&
      Date.now() - op.pendingSince > 2000 && new Date(state.server_time).getTime() > op.pendingSnapshot) {
    op.pending = null; op.feedback = "操作紀錄已被較新操作取代，請重新讀取設定確認。";
  }
  if (d?.last_operation?.status === "done" && d.last_operation.id !== op.completed) { op.completed = d.last_operation.id; sensorDirty = false; }
  const locked = !connected || paused || state.mode === "replay" || op.posting || Boolean(op.pending) || d?.busy || !d;
  $("sensor-model").disabled = op.posting || Boolean(op.pending);
  $("sensor-connect").disabled = locked || !d?.supported || !d.token_configured || d.authenticated;
  $("sensor-refresh").disabled = locked || !d?.authenticated;
  $("sensor-disconnect").disabled = locked || !d?.authenticated;
  setText("sensor-settings-status", op.feedback || d?.error || (d ? `${DIRECT_STATUS[d.status] || d.status}${d.last_operation ? ` · ${{ pending: "操作進行中", done: "操作完成", error: "操作失敗" }[d.last_operation.status] || ""}` : ""}` : "離線重播或尚未設定此感測器"));
  setText("sensor-settings-note", d ? `${d.note}${!d.token_configured && d.supported ? " 尚未設定直連 token，可用 set-token 指令保存；bindkey 無法代替。" : ""}${d.model_confirmed === false ? " 寫入前須確認協定型號 linp.motion.hs1bb2，並在本機設定 miot_model。" : ""}` : "即時模式才可連接；不會將歷史資料寫回裝置。");
  const catalog = Object.entries(d?.properties || {});
  const catalogSignature = `${model}:${catalog.map(([name]) => name).join(",")}`;
  if ($("sensor-property").dataset.catalog !== catalogSignature) {
    $("sensor-property").dataset.catalog = catalogSignature;
    $("sensor-property").replaceChildren(...catalog.map(([name, p]) => new Option(p.label, name)));
    sensorSignature = ""; sensorDirty = false;
  }
  const name = $("sensor-property").value, prop = d?.properties?.[name];
  const signature = `${model}:${name}`;
  if (signature !== sensorSignature) {
    sensorSignature = signature; sensorDirty = false;
    const choice = prop?.choices?.length || prop?.format === "bool", input = el(choice ? "select" : "input");
    input.id = "sensor-value"; input.name = "value"; input.required = true;
    input.setAttribute("aria-describedby", "sensor-current");
    if (choice) {
      input.append(new Option("請選擇", ""));
      if (prop.format === "bool") input.append(new Option("停用", "false"), new Option("啟用", "true"));
      else prop.choices.forEach((label, index) => input.append(new Option(label, String(index))));
    } else { input.type = "number"; input.step = String(prop?.step || 1); if (prop?.minimum !== null && prop?.minimum !== undefined) input.min = String(prop.minimum); if (prop?.maximum !== null && prop?.maximum !== undefined) input.max = String(prop.maximum); }
    $("sensor-value-slot").replaceChildren(input);
  }
  const input = $("sensor-value"), ready = !locked && d?.authenticated && d.status === "ready" && d.model_confirmed !== false;
  if (!sensorDirty && document.activeElement !== input) input.value = prop?.value === null || prop?.value === undefined ? "" : String(prop.value);
  input.disabled = !ready || prop?.status !== "read";
  $("sensor-property").disabled = locked || !catalog.length;
  $("sensor-apply").disabled = input.disabled || !sensorDirty;
  setText("sensor-current", prop ? `目前值：${propertyText(prop)}${prop.unit ? ` ${prop.unit}` : ""} · ${prop.updated_at ? timeText(prop.updated_at) : "尚未讀取"}${prop.status !== "read" ? ` · ${prop.status}` : ""}` : "正式版規格沒有可開放的直連設定。");
  $("sensor-properties").replaceChildren(...catalog.map(([, p]) => {
    const row = el("div"), value = el("dd", "", `${propertyText(p)}${p.unit ? ` ${p.unit}` : ""}`);
    value.append(el("small", "", `${p.siid}.${p.piid} · ${p.status}${p.error ? ` · ${p.error}` : ""}`)); row.append(el("dt", "", p.label), value); return row;
  }));
}
async function sensorRequest(command) {
  const model = $("sensor-model").value, op = sensorOperation(model);
  if (op.posting || op.pending || paused || !connected) return;
  const requestSession = latest?.started_at;
  op.posting = true; op.feedback = "正在送出要求…"; renderSensorSettings(latest);
  try {
    const response = await fetch("/api/sensor/direct", { method: "POST", headers: { "Content-Type": "application/json", "X-Linptech-Control": "1" }, body: JSON.stringify({ ...command, model }), signal: AbortSignal.timeout(5000) });
    const body = await response.json(); if (latest?.started_at !== requestSession) return;
    if (!response.ok) throw new Error(body.error || "本機程式未接受操作。");
    op.pending = body.id; op.pendingSince = Date.now(); op.pendingSnapshot = new Date(latest.server_time).getTime();
    op.feedback = "已接受要求，等待裝置認證或回讀。";
  } catch (error) { op.feedback = error.name === "TimeoutError" ? "送出逾時，請先查看操作結果。" : error.message; }
  finally { op.posting = false; renderSensorSettings(latest); }
}
$("sensor-model").addEventListener("change", () => { sensorSignature = ""; if (latest) renderSensorSettings(latest); });
$("sensor-property").addEventListener("change", () => { sensorSignature = ""; if (latest) renderSensorSettings(latest); });
$("sensor-value-slot").addEventListener("input", () => { sensorDirty = true; if (latest) renderSensorSettings(latest); });
$("sensor-connect").addEventListener("click", () => sensorRequest({ action: "connect" }));
$("sensor-refresh").addEventListener("click", () => sensorRequest({ action: "refresh" }));
$("sensor-disconnect").addEventListener("click", () => sensorRequest({ action: "disconnect" }));
$("sensor-settings-form").addEventListener("submit", event => {
  event.preventDefault(); const input = $("sensor-value"), prop = latest?.sensor_direct?.[$("sensor-model").value]?.properties?.[$("sensor-property").value];
  if ($("sensor-apply").disabled || !prop || !input.reportValidity()) return;
  sensorRequest({ action: "set", property: $("sensor-property").value, value: prop.format === "bool" ? input.value === "true" : Number(input.value) });
});
function renderObjects(state) {
  const statusLabels = { decoded: "已解析", unknown_object: "未知物件", unknown_enum: "未知列舉", outside_spec_range: "超出規格", unauthenticated: "未認證" };
  $("decode-objects").replaceChildren(...state.devices.map(device => {
    const section = el("section", "decode-device"); section.append(el("h3", "", device.model));
    const objects = Object.values(device.objects || {});
    if (!objects.length) section.append(el("p", "", "本輪尚未收到可列出的物件。"));
    for (const item of objects) {
      const value = item.value === undefined ? item.raw_hex || "空值" : typeof item.value === "object" ? JSON.stringify(item.value) : String(item.value);
      const miot = item.miot ? ` · ${item.miot.siid}.${item.miot.piid ?? `e${item.miot.eiid}`}` : "";
      section.append(el("p", "decode-object", `${item.object_id_hex} · ${item.label || item.key || "未識別欄位"}${miot} · ${value} · ${statusLabels[item.status] || item.status} · ${timeText(item.updated_at)}`));
    }
    return section;
  }));
}
function render(state) {
  shown = state;
  $("total-devices").textContent = String(state.devices.length).padStart(2, "0");
  $("verified-devices").textContent = `${state.totals.verified} / ${state.devices.length}`;
  $("packet-count").textContent = state.totals.packets.toLocaleString("zh-TW");
  $("event-count").textContent = state.totals.events.toLocaleString("zh-TW");
  $("sensor-grid").replaceChildren(...state.devices.map((device, index) => card(device, index, state)));
  renderFeed(state); renderTrend(state); renderDirect(state); renderSensorSettings(state); renderObjects(state);
  $("recording-info").textContent = state.mode === "replay" ? "離線重播 · 保留原始封包時間" : "完整廣播與事件持續存入 captures 資料夾";
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
  setText("direct-guide", `將 ES5BB 放近${viaProxy ? "選定的 ESPHome 代理" : "電腦"}，離開手機米家的裝置頁並暫時關閉手機藍牙；iPhone 請從「設定 → 藍牙」關閉。按「連接 ES5BB」後立即單擊感測器實體按鍵，保持供電；請勿長按重設。`);
  const message = !connected ? "網頁與本機程式的連線中斷；畫面保留最後收到的資料，連線恢復後會自動同步。" : (latest?.scanner.error || (paused ? "畫面已暫停；後台持續接收並保存廣播。按「繼續顯示」即可同步最新狀態。" : (latest?.mode === "replay" ? "這是歷史廣播的離線重播，時間與數值不代表裝置目前狀態。" : "")));
  $("notice").textContent = message; $("notice").hidden = !message;
  $("notice").className = `notice${!connected || status === "error" ? " error" : ""}`;
  directAvailability();
  if (latest) renderSensorSettings(paused && shown ? shown : latest);
  const direct = paused && shown ? shown.direct : latest?.direct;
  if (direct) { renderPirEnergy(direct); renderSignals(direct); }
}
function receive(state) { latest = state; connected = true; renderConnection(); if (!paused) render(state); }
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
