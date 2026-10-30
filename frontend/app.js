/* Smart EV Energy Management – dashboard logic (no build step; Chart.js and Leaflet are bundled locally). */
"use strict";

// ================================================================ helpers
const $ = (id) => document.getElementById(id);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const fmt = (n, d = 0) => (n === null || n === undefined || Number.isNaN(Number(n))) ? "—"
  : Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const hh = (h) => String(h).padStart(2, "0") + ":00";
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const store = {
  get(k, d) { try { const v = localStorage.getItem("ev." + k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("ev." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } if (SYNCED.includes(k)) savePrefsSoon(); },
};

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  } catch {
    throw new Error("Cannot reach the backend. Is the server running (uvicorn app.main:app)?");
  }
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (res.status === 401 && !path.startsWith("/api/auth/")) { showAuth(); }
  if (!res.ok) {
    let msg = body && body.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0] || "input"}: ${d.msg}`).join("; ");
    const err = new Error(msg || `Request failed (${res.status})`); err.status = res.status; throw err;
  }
  return body;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}

function table(cols, rows, opts = {}) {
  const head = cols.map((c) => `<th class="${c.n ? "n" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => `<tr class="${opts.hl && opts.hl(r, i) ? "hl" : ""}${r._total ? " total" : ""}">` +
    cols.map((c) => `<td class="${c.n ? "n" : ""}">${c.html ? c.html(r) : esc(c.get ? c.get(r) : r[c.key])}</td>`).join("") + "</tr>").join("");
  return `<div class="tbl-wrap"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

const STATUS = {
  normal: { cls: "good", icon: "✓", label: "Normal" },
  warning: { cls: "warning", icon: "!", label: "Warning" },
  anomalous: { cls: "critical", icon: "✕", label: "Anomalous" },
  check_inputs: { cls: "neutral", icon: "?", label: "Check inputs" },
  baseline_saved: { cls: "neutral", icon: "i", label: "Reference saved" },
  not_checked: { cls: "neutral", icon: "–", label: "Not checked" },
};
const badge = (cls, label) => `<span class="badge ${cls}">${esc(label)}</span>`;
const levelChip = (lv) => `<span class="level ${esc(lv)}">${esc(lv[0].toUpperCase() + lv.slice(1))}</span>`;

// ================================================================ charts
const charts = {};
function chart(id, cfg) {
  if (charts[id]) charts[id].destroy();
  const el = $(id);
  if (!el || typeof Chart === "undefined") return;
  charts[id] = new Chart(el, cfg);
}
function chartTheme() {
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = css("--text-2");
  Chart.defaults.borderColor = css("--grid");
}
function baseOptions({ xTitle, yTitle, xType = "category", yMin, yMax, legend = true, stacked = false } = {}) {
  const grid = { color: css("--grid") }, ticks = { color: css("--muted") };
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: legend, position: "top", align: "start", labels: { boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: "rectRounded", color: css("--text-2") } },
      tooltip: { backgroundColor: css("--surface"), titleColor: css("--text"), bodyColor: css("--text"), borderColor: css("--axis"), borderWidth: 1, padding: 10, boxPadding: 4 },
    },
    scales: {
      x: { type: xType, grid: { display: false }, ticks: { ...ticks, maxRotation: 0, autoSkipPadding: 12 }, border: { color: css("--axis") },
        title: { display: !!xTitle, text: xTitle, color: css("--text-2") }, stacked },
      y: { grid, ticks, border: { display: false }, min: yMin, max: yMax, stacked,
        title: { display: !!yTitle, text: yTitle, color: css("--text-2") } },
    },
  };
}
const alpha = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };

// ================================================================ vehicle profile (shared by RUL + anomaly forms)
const YEAR_NOW = new Date().getFullYear();
let profile = store.get("profile", { vehicle_type: "4W", vehicle_name: "", registration_year: "", odometer_km: "",
  km_per_year: "", readings: [{ soc_pct: "", range_km: "" }], new_full_range_km: "" });

function vehicleFieldsHTML(p) {
  return `
    <label class="field"><span>Vehicle type</span>
      <select data-f="vehicle_type"><option value="4W">Car (4-wheeler)</option><option value="3W">3-wheeler</option><option value="2W">2-wheeler</option></select></label>
    <label class="field"><span>Vehicle name – optional</span><input type="text" data-f="vehicle_name" maxlength="120" placeholder="e.g. Tata Nexon EV"></label>
    <div class="row-2">
      <label class="field"><span>Registration year</span><input type="number" data-f="registration_year" min="2010" max="${YEAR_NOW}" step="1" inputmode="numeric"></label>
      <label class="field"><span>Odometer (km)</span><input type="number" data-f="odometer_km" min="1" step="1" inputmode="numeric"></label>
    </div>
    <label class="field"><span>Distance you drive per year (km)</span><input type="number" data-f="km_per_year" min="1" step="100" inputmode="numeric"></label>
    <div class="readings">
      <div class="readings-head"><span>Dashboard readings (1–3)</span><button type="button" class="btn small ghost" data-add>+ Add reading</button></div>
      <div data-rows></div>
    </div>`;
}
function renderReadings(form) {
  const rows = form.querySelector("[data-rows]");
  rows.innerHTML = profile.readings.map((r, i) => `
    <div class="reading-row">
      <label class="field"><span>Battery % (SOC)</span><input type="number" data-r="${i}" data-k="soc_pct" min="20" max="100" step="1" inputmode="numeric" value="${esc(r.soc_pct)}" placeholder="20–100"></label>
      <label class="field"><span>Range shown (km)</span><input type="number" data-r="${i}" data-k="range_km" min="1" step="1" inputmode="numeric" value="${esc(r.range_km)}" placeholder="km"></label>
      <button type="button" class="icon-btn" data-del="${i}" aria-label="Remove reading ${i + 1}" ${profile.readings.length === 1 ? "disabled" : ""}>×</button>
    </div>`).join("");
  form.querySelector("[data-add]").disabled = profile.readings.length >= 3;
}
function fillVehicleForm(form) {
  form.querySelectorAll("[data-f]").forEach((el) => { el.value = profile[el.dataset.f] ?? ""; });
  renderReadings(form);
}
function setupVehicleForm(form) {
  form.querySelector(".vehicle-fields").innerHTML = vehicleFieldsHTML(profile);
  fillVehicleForm(form);
  form.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset.f) profile[t.dataset.f] = t.value;
    if (t.dataset.r !== undefined) profile.readings[+t.dataset.r][t.dataset.k] = t.value;
    store.set("profile", profile);
  });
  form.addEventListener("click", (e) => {
    if (e.target.matches("[data-add]") && profile.readings.length < 3) { profile.readings.push({ soc_pct: "", range_km: "" }); renderReadings(form); }
    if (e.target.matches("[data-del]")) { profile.readings.splice(+e.target.dataset.del, 1); renderReadings(form); }
    store.set("profile", profile);
  });
}
function vehicleId() {
  // id from before user accounts; with an account the server stores readings under the account instead
  let id = store.get("vehicleId", null);
  if (!id) {
    id = "v" + Math.random().toString(36).slice(2, 12) + Date.now().toString(36);
    store.set("vehicleId", id);
  }
  return id;
}
function vehiclePayload() {
  const n = (v) => (v === "" || v === null || v === undefined) ? NaN : Number(v);
  const errs = [];
  const yr = n(profile.registration_year), odo = n(profile.odometer_km), kpy = n(profile.km_per_year);
  if (!(yr >= 2010 && yr <= YEAR_NOW)) errs.push(`Registration year must be between 2010 and ${YEAR_NOW}.`);
  if (!(odo > 0)) errs.push("Enter the odometer reading.");
  if (!(kpy > 0)) errs.push("Enter the distance you drive per year.");
  const readings = profile.readings.map((r) => ({ soc_pct: n(r.soc_pct), range_km: n(r.range_km) }));
  readings.forEach((r, i) => {
    if (!(r.soc_pct >= 20 && r.soc_pct <= 100)) errs.push(`Reading ${i + 1}: battery % must be 20–100.`);
    if (!(r.range_km > 0)) errs.push(`Reading ${i + 1}: enter the range shown.`);
  });
  if (errs.length) throw new Error(errs.join(" "));
  return { vehicle_type: profile.vehicle_type, vehicle_name: profile.vehicle_name || null, registration_year: yr,
    odometer_km: odo, km_per_year: kpy, readings, vehicle_id: vehicleId() };
}

// ================================================================ router
const loaded = {};
const pages = ["overview", "plan", "battery", "anomaly", "demand", "pricing", "stations"];
const loaders = {};
const drawers = {};
function route() {
  const page = pages.includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  pages.forEach((p) => { $("page-" + p).hidden = p !== page; });
  document.querySelectorAll(".sidebar a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  $("sidebar").classList.remove("open"); $("menu-btn").setAttribute("aria-expanded", "false");
  document.title = `${document.querySelector(`.sidebar a[data-page=${page}]`).textContent.trim()} – Smart EV Energy Management`;
  window.scrollTo(0, 0);
  const always = page === "overview";
  if ((!loaded[page] || always) && loaders[page]) { loaded[page] = true; loaders[page]().catch((e) => toast(e.message)); }
  if (page === "plan" && plMap) setTimeout(() => plMap.invalidateSize(), 60);
  if (page === "stations" && maps["st-map"]) setTimeout(() => maps["st-map"].invalidateSize(), 60);
}

// ================================================================ shared data
const cache = {};
async function getStatus() { cache.status = await api("/api/status"); return cache.status; }
async function getDemand() { if (!cache.demand) cache.demand = await api("/api/demand?history_days=60"); return cache.demand; }
async function getModelInfo() { if (!cache.models) cache.models = await api("/api/model-info"); return cache.models; }

// ================================================================ OVERVIEW
loaders.overview = async () => {
  const h = new Date().getHours();
  const hello = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  $("ov-hello").textContent = account && account.name ? `${hello}, ${account.name.split(" ")[0]}` : hello;
  const [dm, pr] = await Promise.all([
    getDemand().catch(() => null),
    api(`/api/pricing?hour=${h}`).catch(() => null),
  ]);
  const lastRul = store.get("lastRul", null), lastAn = store.get("lastAnomaly", null);
  const tile = (k, v, r) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="r">${r}</div></div>`;
  $("ov-tiles").innerHTML = [
    lastRul ? tile(`Battery life left${lastRul.vehicle ? " · " + esc(lastRul.vehicle) : ""}`, esc(lastRul.years), esc(lastRul.detail))
      : tile("Battery life left", "—", `<a href="#battery">Predict it →</a>`),
    lastAn ? tile("Battery health check", badge(STATUS[lastAn.status].cls, STATUS[lastAn.status].label), esc(lastAn.vehicle || "your vehicle"))
      : tile("Battery health check", "—", `<a href="#anomaly">Check your battery →</a>`),
    dm ? tile("Charging demand today", levelChip(dm.next_day.level), `<a href="#demand">Stations near you →</a>`)
      : tile("Charging demand today", "—", `<a href="#demand">Stations near you →</a>`),
    pr ? tile(`Charging now · ${hh(pr.quote.hour)}`, `${esc(pr.quote.time_band)} <small>hour</small>`, `<a href="#pricing">Prices near you →</a>`)
      : tile("Charging now", "—", `<a href="#pricing">Prices near you →</a>`),
  ].join("");
  drawers.overview = null;
};

// ================================================================ PLAN MY CHARGE (whole workflow)
const PL_COLOR = { distance: "--s1", price: "--s2", wait: "--s3", power: "--s4", connector: "--s5", status: "--s6", battery: "--s7" };
let plMap = null, plLayer = null, plCoords = null;
loaders.plan = async () => {
  const o = await api("/api/plan/options");
  cache.planOptions = o;
  $("pl-date").innerHTML = o.forecast_days.map((d) => `<option value="${esc(d.date)}">${esc(dayLabel(d))}</option>`).join("");
  $("pl-scen").innerHTML = Object.entries(o.scenarios).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  const desc = { balanced: "A bit of everything", cheapest: "Lowest price first", nearest: "Shortest distance first", fastest: "Least waiting, highest power" };
  $("pl-pref").innerHTML = Object.entries(o.preferences).map(([k, v], i) => `<label><input type="radio" name="pl-pref" value="${k}" ${i === 0 ? "checked" : ""}>
    <span><b>${esc(v.label)}</b><small>${esc(desc[k] || "")}</small></span></label>`).join("");
};
const SRC = { live: ["good", "Live"], "live-adjusted": ["warning", "Forecast + live"], estimated: ["neutral", "Estimated"], calibrated: ["neutral", "Estimated · learned"] };
const LIVE_NAME = { google: "Google Maps", driver_checkins: "driver report", ocpp: "charger feed" };
const liveName = (s) => s.live_source === "ocpp" && s.live_used && s.live_used.simulated ? "simulated charger feed" : (LIVE_NAME[s.live_source] || "live");
const agoText = (sec) => sec == null ? "" : sec < 90 ? `${fmt(sec)} s ago` : sec < 5400 ? `${fmt(sec / 60)} min ago` : sec < 172800 ? `${fmt(sec / 3600)} h ago` : `${fmt(sec / 86400)} days ago`;
function tariffBadge(t) {
  if (!t) return `<span class="badge neutral tag">Not known</span>`;
  if (t.source === "ocpp") return `<span class="badge good tag">Live</span>`;
  if (t.source === "driver_report") return `<span class="badge warning tag">Drivers</span>`;
  return `<span class="badge neutral tag">Listed</span>`;
}
function tariffText(t) {
  if (!t) return `<span class="muted">not known</span>`;
  const extra = t.source === "driver_report" ? ` · ${t.reports} report${t.reports > 1 ? "s" : ""}, latest ${agoText(t.seconds_ago)}`
    : t.source === "ocpp" ? ` · ${agoText(t.seconds_ago)}` : "";
  return `₹${fmt(t.price_per_kwh, 2)}/kWh<span class="small muted">${extra}</span>`;
}
const vsTariff = (p) => p.vs_tariff_pct == null ? "" : ` <span class="small muted">(${p.vs_tariff_pct > 0 ? "+" : ""}${fmt(p.vs_tariff_pct, 0)}% vs tariff)</span>`;
const srcBadge = (src) => { const x = SRC[src] || SRC.estimated; return `<span class="badge ${x[0]} tag">${x[1]}</span>`; };
function liveBlock(s) {
  const parts = [];
  const g = s.live && s.live.has_live_availability ? s.live : null;
  const r = s.driver_reports;
  if (s.live_source === "ocpp" && s.live_used) {
    const f = s.live_used, usable = f.connector_count - f.out_of_service;
    const cls = (st) => st === "Available" ? "free" : (st === "Faulted" || st === "Unavailable") ? "out" : "busy";
    const pts = f.connectors.map((c) => `<span class="pt ${cls(c.status)}">Point ${c.point}: ${esc(c.status)}</span>`).join("");
    parts.push(`<div class="livebox"><span class="badge good tag">Live · charger feed${f.simulated ? " (simulated)" : " (OCPP)"}</span> <b>${fmt(f.available)} of ${fmt(usable)} points free now</b>${f.out_of_service ? ` · ${fmt(f.out_of_service)} out of service` : ""} · updated ${fmt(f.updated_s_ago)} s ago${f.connected ? "" : " · charger disconnected"}<div class="pts">${pts}</div></div>`);
  }
  if (s.live_source === "google" && g) {
    const rows = g.connectors.map((c) => `${esc(c.type)}${c.max_kw ? " " + fmt(c.max_kw, 0) + " kW" : ""}: ${c.available ?? "?"} free of ${c.count}${c.out_of_service ? `, ${c.out_of_service} out of service` : ""}`).join(" · ");
    parts.push(`<div class="livebox"><span class="badge good tag">Live · Google Maps</span> <b>${fmt(g.available)} of ${fmt(g.connector_count - g.out_of_service)} points free now</b>${g.out_of_service ? ` · ${fmt(g.out_of_service)} out of service` : ""}${g.updated_min_ago !== null ? ` · updated ${fmt(g.updated_min_ago)} min ago` : ""}<div class="small muted">${rows}</div></div>`);
  }
  if (r) {
    const l = r.latest;
    const what = l.status === "free" ? (l.free_points != null ? `${l.free_points}${l.total_points ? " of " + l.total_points : ""} points free` : "Points free")
      : l.status === "queue" ? `All busy, ${l.queue} waiting` : l.label;
    const live = s.live_source === "driver_checkins";
    parts.push(`<div class="livebox ${live ? "" : "old"}"><span class="badge ${live ? "good" : "neutral"} tag">${live ? "Live · driver reports" : r.is_live ? "Driver report" : "Older driver report"}</span> <b>${esc(what)}</b> · ${fmt(l.minutes_ago)} min ago${r.reports_live > 1 ? ` · ${r.agreeing_reports} of ${r.reports_live} recent reports agree` : ""}${!r.is_live ? " · older than 45 min, not used" : ""}</div>`);
  }
  const oc = s.ocm_checkins;
  if (oc && (oc.latest || oc.status_updated_days_ago != null)) {
    parts.push(`<p class="small muted" style="margin:6px 0 0">Open Charge Map: ${oc.latest ? `last check-in “${esc(oc.latest.title)}” ${fmt(oc.latest.days_ago)} days ago · 90 days: ${oc.successes_90d} successful, ${oc.failures_90d} failed` : "no check-ins"}${oc.status_updated_days_ago != null ? ` · details updated ${fmt(oc.status_updated_days_ago)} days ago` : ""}</p>`);
  }
  if (!s.live_source) parts.push(`<p class="small muted" style="margin:6px 0 0">No live data right now – demand, waiting time and price are estimated${s.learned ? `, recalibrated from past live readings at this station (arrivals × ${fmt(s.demand.learned_factor, 2)} at this hour)` : ""}. Source: ${esc(s.data_source || "Open Charge Map")}.</p>`);
  return parts.join("");
}
function reportForm(s) {
  const sid = esc(String(s.id));
  return `<details class="ci"><summary>Report status now (at this station)</summary>
    <form class="ci-form" data-sid="${sid}" data-name="${esc(s.name)}" data-lat="${s.latitude}" data-lon="${s.longitude}" data-points="${s.number_of_points || ""}">
      <div class="ci-opts">
        <label><input type="radio" name="st" value="free" checked> Points free</label>
        <label><input type="radio" name="st" value="busy"> All busy</label>
        <label><input type="radio" name="st" value="queue"> Queue</label>
        <label><input type="radio" name="st" value="not_working"> Not working</label>
      </div>
      <div class="ci-nums">
        <label class="ci-free">Free <input type="number" name="free" min="1" max="100" placeholder="?"> of <input type="number" name="total" min="1" max="100" value="${s.number_of_points || ""}" placeholder="?"></label>
        <label class="ci-queue" hidden>Vehicles waiting <input type="number" name="queue" min="1" max="50" value="1"></label>
        <label class="ci-price">Price paid ₹ <input type="number" name="price" min="1" max="100" step="0.01" placeholder="optional"> /kWh</label>
        <button class="btn small" type="submit">Send report</button>
      </div>
    </form></details>`;
}
function liveLine(s) {
  const f = s.live_used, g = s.live && s.live.has_live_availability ? s.live : null, r = s.driver_reports;
  if (s.live_source === "ocpp" && f) {
    const cls = (st) => st === "Available" ? "free" : (st === "Faulted" || st === "Unavailable") ? "out" : "busy";
    const pts = f.connectors.map((c) => `<span class="pt ${cls(c.status)}">${c.point}: ${esc(c.status === "Available" ? "Free" : c.status === "Charging" ? "In use" : c.status)}</span>`).join("");
    return `<div class="livebox"><b>${fmt(f.available)} of ${fmt(f.connector_count - f.out_of_service)} points free now</b> <span class="small muted">· updated ${agoText(f.updated_s_ago)}</span><div class="pts">${pts}</div></div>`;
  }
  if (s.live_source === "google" && g) return `<div class="livebox"><b>${fmt(g.available)} of ${fmt(g.connector_count - g.out_of_service)} points free now</b></div>`;
  if (s.live_source === "driver_checkins" && r) {
    const l = r.latest;
    const what = l.status === "free" ? (l.free_points != null ? `${l.free_points}${l.total_points ? " of " + l.total_points : ""} points free` : "Points free")
      : l.status === "queue" ? `All busy, ${l.queue} waiting` : l.label;
    return `<div class="livebox"><b>${esc(what)}</b> <span class="small muted">· reported by a driver ${fmt(l.minutes_ago)} min ago</span></div>`;
  }
  return "";
}
function planStationCard(s, isBest) {
  const d = s.demand, p = s.price;
  const ranked = s.recommendation_rank !== null && s.recommendation_rank !== undefined;
  const stack = ranked ? s.score_breakdown.map((b) => `<i style="width:${b.contribution}%;background:${css(PL_COLOR[b.criterion])}" title="${esc(b.label)}: ${fmt(b.contribution, 1)} of ${fmt(b.weight_pct, 0)} points"></i>`).join("") : "";
  const area = stArea(s);
  return `<article class="card station ${isBest ? "best" : ""} ${ranked ? "" : "muted-row"}">
    ${isBest ? `<span class="label">Recommended</span>` : ""}
    <div class="station-head"><div class="rank" ${ranked ? "" : 'style="background:var(--muted)"'}>${ranked ? s.recommendation_rank : "–"}</div>
      <div style="min-width:0"><h3>${esc(s.name)}${accessTag(s)}${s.live_source ? ` <span class="badge good tag">Live</span>` : ""}</h3>
        ${area ? `<div class="st-addr">${esc(area)}</div>` : ""}</div>
      ${ranked ? `<div class="score"><b>${fmt(s.recommendation_score * 100)}</b><span>score</span></div>` : ""}</div>
    <div class="facts kf">
      <div><span>Distance</span>${fmt(s.distance_km, 1)} km</div>
      <div><span>Price now</span><b>₹${fmt(p.price_per_kwh, 2)}</b>/kWh</div>
      <div><span>Waiting</span>${esc(String(d.wait_label || "").replace(/ \(live\)$/, ""))}</div>
      <div><span>Demand</span>${levelChip(d.level)}</div>
      <div><span>Power</span>${s.max_power_kw ? fmt(s.max_power_kw, 0) + " kW" : "—"}</div>
    </div>
    ${liveLine(s)}
    ${ranked ? `<div class="stack" role="img" aria-label="Score breakdown">${stack}</div>
    <p class="explain">${esc(s.why)}</p>` : ""}
    ${reportForm(s)}
  </article>`;
}
function drawPlan() {
  const r = cache.plan; if (!r) return;
  const f = r.final, b = f.battery, dm = f.demand, best = f.recommended;
  const an = STATUS[b.anomaly_status];
  const fits = r.stations.filter((s) => s.compatibility !== "not_compatible");
  const list = showAll.plan ? fits : fits.slice(0, 10);
  const top = fits.find((s) => s.recommendation_rank);
  const liveN = fits.filter((s) => s.live_source).length;
  const more = fits.length > 10 ? `<div><button class="btn small ghost" type="button" data-showall="plan">${showAll.plan ? "Show top 10 only" : `Show all ${fits.length} stations`}</button></div>` : "";
  $("pl-result").innerHTML = `
    <h2 style="margin:4px 0 12px">Your charging plan</h2>
    <div class="final-grid">
      <div class="card"><h2>Battery</h2>
        <div class="big">${b.rul_years.mid === null ? "40+" : fmt(b.rul_years.mid, 1)} <small class="muted" style="font-size:14px">years left</small></div>
        <p class="muted small">Health ${fmt(b.soh_now_pct.mid, 1)}%${b.rul_km.mid === null ? "" : ` · ~${fmt(b.rul_km.mid)} km left`}</p>
        <p class="pl-badges">${an ? badge(an.cls, an.label === "Normal" ? "No anomaly" : "Anomaly: " + an.label) : badge("neutral", "Anomaly not checked")} ${b.condition.status === "healthy" ? badge("good", "Healthy") : badge("warning", "Needs care")}</p></div>
      <div class="card"><h2>Demand</h2>
        <div class="big">${levelChip(dm.city_level)}</div>
        <p class="muted small">${esc(dayLabel(dm))} · ${hh(r.inputs.hour)}</p>
        <p class="muted small">Live data at ${liveN} of ${fits.length} stations</p></div>
      <div class="card ${best ? "best" : ""}"><h2>Recommended station</h2>
        ${best ? `<div class="big" style="font-size:20px">${esc(best.name)}</div>
          <div class="facts" style="margin:10px 0"><div><span>Distance</span>${fmt(best.distance_km, 1)} km</div><div><span>Price</span>₹${fmt(best.price_per_kwh, 2)}/kWh</div>
            <div><span>Waiting</span>${esc(String(best.wait || "").replace(/ \(live\)$/, ""))}</div><div><span>Demand</span>${levelChip(best.demand_level)}</div></div>`
        : `<div class="alert warning"><span class="ico">!</span><span>No suitable station found nearby. Try a larger radius.</span></div>`}</div>
    </div>
    ${fits.length ? `<div class="grid-map"><div class="card map-card"><div id="pl-map" role="region" aria-label="Map of stations"></div></div>
      <div class="st-list">${top ? `<div class="stack-legend sc-key"><b>Score:</b>${top.score_breakdown.filter((x) => x.weight_pct > 0).map((x) => `<span style="--c:${css(PL_COLOR[x.criterion])}">${esc(x.label)}</span>`).join("")}</div>` : ""}${list.map((s) => planStationCard(s, s === top)).join("")}${more}</div></div>` : ""}`;
  $("pl-result").hidden = false;
  plMap = stationMap("pl-map", r.inputs, list);
}
drawers.plan = drawPlan;
async function submitPlan(e) {
  e.preventDefault();
  const btn = $("pl-btn"); $("pl-err").textContent = ""; $("pl-result").hidden = true;
  let payload;
  try {
    payload = vehiclePayload();
    const q = $("pl-q").value.trim();
    if (plCoords && q === "Your current location") Object.assign(payload, { latitude: plCoords[0], longitude: plCoords[1], location: "Your current location" });
    else if (q.length >= 2) payload.location = q;
    else throw new Error("Enter a location (or use ◎ for your current location).");
    const nr = Number(profile.new_full_range_km);
    if (nr > 0) payload.new_full_range_km = nr;
  } catch (err) { $("pl-err").textContent = err.message; return; }
  const pref = document.querySelector('input[name="pl-pref"]:checked');
  Object.assign(payload, { hour: Number($("pl-hour").value), date: $("pl-date").value || null, preference: pref ? pref.value : "balanced",
    connector: $("pl-con").value || null, scenario: $("pl-scen").value || "forecast", distance_km: Number($("pl-rad").value),
    base_price: Number($("pl-base").value) || 10, gst_pct: Number($("pl-gst").value) || 0, save: true });
  btn.disabled = true; btn.textContent = "Planning…";
  try {
    cache.plan = await api("/api/plan", { method: "POST", body: JSON.stringify(payload) });
    drawPlan();
    $("pl-result").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) { $("pl-err").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = "Plan my charge"; }
}

// ================================================================ BATTERY (vehicle RUL)
const yrs = (y) => y === null || y === undefined ? "40+" : fmt(y, 1);
const kms = (k) => k === null || k === undefined ? "—" : fmt(k);
function rangeText(o, f) {
  // high = null means the battery stays above 80% for more than 40 years in the optimistic case
  return o.high === null || o.high === undefined ? `range ${f(o.low)} or more` : `range ${f(o.low)} – ${f(o.high)}`;
}

async function submitRul(e) {
  e.preventDefault();
  const btn = $("rul-btn"); $("rul-err").textContent = ""; $("rul-result").hidden = true;
  let payload;
  try { payload = vehiclePayload(); } catch (err) { $("rul-err").textContent = err.message; return; }
  btn.disabled = true; btn.textContent = "Predicting…";
  try {
    const r = await api("/api/rul", { method: "POST", body: JSON.stringify({ ...payload, save: true }) });
    cache.rul = { ...r, km_per_year: payload.km_per_year }; drawRul();
    if (viewer3d) viewer3d.showBattery(true);
    store.set("lastRul", { vehicle: payload.vehicle_name, years: r.rul_years.mid === null ? "40+ years" : `${fmt(r.rul_years.mid, 1)} years`,
      detail: r.rul_km.mid === null ? "More than 40 years at your driving" : `~${fmt(r.rul_km.mid)} km · ~${fmt(r.rul_cycles.mid)} charge cycles`, evidence: r.evidence });
    $("rul-result").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) { $("rul-err").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = "Predict remaining life"; }
}
let viewer3d = null;
const healthClass = (h) => h >= 85 ? "good" : h >= 75 ? "warning" : "critical";
function healthGauge(h) {
  const r = 52, c = 2 * Math.PI * r, f = Math.max(0, Math.min(100, h)) / 100;
  return `<svg viewBox="0 0 128 128" class="gauge-svg" aria-hidden="true">
      <circle cx="64" cy="64" r="${r}" class="g-track"/>
      <circle cx="64" cy="64" r="${r}" class="g-val" stroke-dasharray="${(c * f).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 64 64)"/></svg>
    <div class="g-text"><b>${fmt(h, 1)}%</b><span>Battery health</span></div>`;
}
function rulSummary(r) {
  const done = r.rul_years.mid === 0, hc = healthClass(r.soh_now_pct.mid);
  return `
    <div class="gauge ${hc}" role="img" aria-label="Battery health ${fmt(r.soh_now_pct.mid, 1)} percent">${healthGauge(r.soh_now_pct.mid)}</div>
    <div class="rul-keys">
      ${done ? `<div class="tile"><div class="k">Status</div><div class="v">End of life</div></div>` : `
      <div class="tile"><div class="k">Time left</div><div class="v">${yrs(r.rul_years.mid)} <small>years</small></div><div class="range-note">${r.rul_years.high === null ? `range ${yrs(r.rul_years.low)} – 40+` : rangeText(r.rul_years, yrs)} years</div></div>
      <div class="tile"><div class="k">Distance left</div><div class="v">${kms(r.rul_km.mid)} <small>km</small></div><div class="range-note">${rangeText(r.rul_km, kms)} km</div></div>
      <div class="tile"><div class="k">Charge cycles left</div><div class="v">${kms(r.rul_cycles.mid)}</div><div class="range-note">${rangeText(r.rul_cycles, kms)} cycles</div></div>`}
    </div>`;
}
function rulChart(r) {
  if (!r.projection || !r.projection.length) { if (charts["rul-chart"]) charts["rul-chart"].destroy(); return; }
  const P = r.projection, s1 = css("--s1"), crit = css("--critical");
  const ymin = Math.max(0, Math.min(75, Math.floor(Math.min(...P.map((p) => p.low)) / 5) * 5));
  chart("rul-chart", {
    type: "line",
    data: { datasets: [
      { label: "_hi", data: P.map((p) => ({ x: p.years_from_now, y: p.high })), borderColor: alpha(s1, .45), borderWidth: 1, pointRadius: 0, fill: false },
      { label: "Range for similar batteries", data: P.map((p) => ({ x: p.years_from_now, y: p.low })), borderColor: alpha(s1, .45), borderWidth: 1, pointRadius: 0, fill: "-1", backgroundColor: alpha(s1, .14) },
      { label: "Most likely", data: P.map((p) => ({ x: p.years_from_now, y: p.mid })), borderColor: s1, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 },
      { label: `End of life (${r.eol_threshold_pct}%)`, data: [{ x: 0, y: r.eol_threshold_pct }, { x: P[P.length - 1].years_from_now, y: r.eol_threshold_pct }], borderColor: crit, borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0 },
    ] },
    options: (() => {
      const o = baseOptions({ xType: "linear", xTitle: "Years from now", yTitle: "Battery health (%)", yMin: ymin, yMax: 100 });
      o.animation = false;
      o.plugins.legend.labels.filter = (l) => !l.text.startsWith("_");
      o.plugins.tooltip.filter = (i) => !i.dataset.label.startsWith("_") && i.datasetIndex !== 3;
      o.plugins.tooltip.callbacks = { title: (it) => `${fmt(it[0].parsed.x, 1)} years from now`,
        label: (i) => i.datasetIndex === 1 ? ` Range: ${fmt(i.parsed.y, 1)} – ${fmt(P[i.dataIndex].high, 1)}%` : ` ${i.dataset.label}: ${fmt(i.parsed.y, 1)}%` };
      return o;
    })(),
  });
  const step = Math.max(1, Math.round(P.length / 12));
  $("rul-table").innerHTML = table([{ label: "Years from now", key: "years_from_now", n: 1 }, { label: "Low %", key: "low", n: 1 },
    { label: "Most likely %", key: "mid", n: 1 }, { label: "High %", key: "high", n: 1 }], P.filter((_, i) => i % step === 0));
}
function drawRul() {
  const r = cache.rul; if (!r) return;
  if (viewer3d) viewer3d.setHealth(r.soh_now_pct ? r.soh_now_pct.mid : null, r.vehicle_type || profile.vehicle_type);
  const base = Math.round(r.km_per_year || Number(profile.km_per_year) || 10000);
  const max = Math.max(40000, Math.ceil(base * 2 / 5000) * 5000);
  $("rul-result").innerHTML = `
    <div class="card">
      <div class="rul-title"><h2>${esc(r.vehicle)}</h2></div>
      <p id="rul-msg">${esc(r.message)}</p>
      <div class="rul-summary" id="rul-summary">${rulSummary(r)}</div>
    </div>
    <div class="card">
      <div class="rul-title"><h2>Projected battery health</h2></div>
      <div class="whatif">
        <label for="rul-kpy">Distance per year</label>
        <input type="range" id="rul-kpy" min="1000" max="${max}" step="500" value="${base}">
        <output id="rul-kpy-v">${fmt(base)} km</output>
        <button type="button" class="btn small ghost" id="rul-kpy-reset" hidden>Reset</button>
      </div>
      <div class="chart-box tall"><canvas id="rul-chart"></canvas></div>
      <details><summary>Table view</summary><div id="rul-table"></div></details>
    </div>`;
  $("rul-result").hidden = false;
  rulChart(r);
  setupWhatIf(base);
}
// live what-if: recompute with a different distance per year (nothing is saved)
function setupWhatIf(base) {
  const sl = $("rul-kpy"), out = $("rul-kpy-v"), reset = $("rul-kpy-reset");
  let timer = null, seq = 0;
  const run = async (km) => {
    const my = ++seq;
    try {
      const payload = { ...vehiclePayload(), km_per_year: km, save: false };
      const r = await api("/api/rul", { method: "POST", body: JSON.stringify(payload) });
      if (my !== seq || !$("rul-summary")) return;
      $("rul-summary").innerHTML = rulSummary(r);
      $("rul-msg").textContent = r.message;
      rulChart(r);
    } catch (err) { /* keep the last result */ }
  };
  sl.addEventListener("input", () => {
    const km = Number(sl.value);
    out.textContent = `${fmt(km)} km`;
    reset.hidden = km === base;
    clearTimeout(timer); timer = setTimeout(() => run(km), 140);
  });
  reset.addEventListener("click", () => { sl.value = base; out.textContent = `${fmt(base)} km`; reset.hidden = true; clearTimeout(timer); run(base); });
}
drawers.battery = drawRul;


// ================================================================ ANOMALY
async function submitAnomaly(e) {
  e.preventDefault();
  const btn = $("an-btn"); $("an-err").textContent = ""; $("an-result").hidden = true;
  let payload;
  try {
    payload = vehiclePayload();
    const nr = Number(profile.new_full_range_km);
    if (nr > 0) payload.new_full_range_km = nr;
    const tv = ["tr-soc1", "tr-range1", "tr-soc2", "tr-km"].map((id) => $(id).value.trim());
    if (tv.some((x) => x !== "")) {
      if (tv.some((x) => x === "")) throw new Error("Trip check: fill in all four trip fields, or leave them all empty.");
      const [s1, r1, s2, km] = tv.map(Number);
      if (!(s1 >= 20 && s1 <= 100)) throw new Error("Trip check: battery % at start must be 20–100.");
      if (!(s2 >= 0 && s2 < s1)) throw new Error("Trip check: battery % at the end must be lower than at the start.");
      if (s1 - s2 < 10) throw new Error(`Trip check: use at least 10% of battery (this trip used ${s1 - s2}%).`);
      if (!(r1 > 0 && km > 0)) throw new Error("Trip check: range at start and km ridden must be greater than 0.");
      if (!$("tr-nocharge").checked) throw new Error("Trip check: confirm you didn't charge during the ride.");
      payload.trip = { soc_start: s1, range_start_km: r1, soc_end: s2, distance_km: km, ride_mode: $("tr-mode").value || null, no_charging: true };
    }
  } catch (err) { $("an-err").textContent = err.message; return; }
  btn.disabled = true; btn.textContent = "Checking…";
  try {
    const r = await api("/api/anomaly", { method: "POST", body: JSON.stringify({ ...payload, save: true }) });
    cache.anomaly = r; drawAnomaly(); drawHistory(r.history || []); drawTrips(r.trips || []);
    store.set("lastAnomaly", { status: r.status, message: r.message, vehicle: payload.vehicle_name });
    $("an-result").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) { $("an-err").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = "Check battery"; }
}
function drawHistory(rows) {
  if (!$("an-history")) return;
  const src = { rul: "RUL check", anomaly: "Battery check", plan: "Charging plan", earlier: "Added by you", check: "Check" };
  $("an-history").innerHTML = rows.length ? table([
    { label: "Date", key: "reading_date" }, { label: "Odometer", get: (r) => fmt(r.odometer_km) + " km", n: 1 },
    { label: "SOC", get: (r) => fmt(r.soc_pct) + "%", n: 1 }, { label: "Range shown", get: (r) => fmt(r.range_km) + " km", n: 1 },
    { label: "Full-charge range", get: (r) => fmt(r.full_charge_km, 1) + " km", n: 1 }, { label: "Source", get: (r) => src[r.source] || r.source },
    { label: "", html: (r) => `<button class="btn small ghost danger" type="button" data-hdel="${r.id}">Delete</button>` }], rows)
    : `<p class="muted">No saved readings yet. Your first battery check will be saved here.</p>`;
}
function drawTrips(rows) {
  if (!$("an-trips")) return;
  $("an-trips").innerHTML = rows.length ? table([
    { label: "Date", key: "trip_date" }, { label: "Start", get: (t) => `${fmt(t.soc_start)}% · ${fmt(t.range_start_km)} km` },
    { label: "End", get: (t) => `${fmt(t.soc_end)}% after ${fmt(t.distance_km, 1)} km` }, { label: "Mode", get: (t) => t.ride_mode || "—" },
    { label: "Promised", get: (t) => fmt(t.promised_km) + " km", n: 1 }, { label: "Delivered", get: (t) => fmt(t.delivered_km) + " km", n: 1 },
    { label: "Gap", get: (t) => (t.gap_pct > 0 ? "−" : "+") + fmt(Math.abs(t.gap_pct), 1) + "%", n: 1 },
    { label: "", html: (t) => `<button class="btn small ghost danger" type="button" data-tdel="${t.id}">Delete</button>` }], rows)
    : `<p class="muted">No trips yet. Fill in the trip check to add one.</p>`;
}
async function loadHistory() {
  const d = await api(`/api/history/${encodeURIComponent(vehicleId())}`);
  drawHistory(d.readings);
  const t = await api(`/api/trips/${encodeURIComponent(vehicleId())}`);
  drawTrips(t.trips);
}
async function addEarlierReading(e) {
  e.preventDefault();
  $("ah-err").textContent = "";
  const body = { vehicle_id: vehicleId(), reading_date: $("ah-date").value, odometer_km: Number($("ah-odo").value),
    readings: [{ soc_pct: Number($("ah-soc").value), range_km: Number($("ah-range").value) }] };
  if (!body.reading_date || !(body.odometer_km > 0) || !(body.readings[0].soc_pct >= 20 && body.readings[0].soc_pct <= 100) || !(body.readings[0].range_km > 0)) {
    $("ah-err").textContent = "Enter the date, odometer, battery % (20–100) and range shown."; return;
  }
  try {
    const d = await api("/api/history", { method: "POST", body: JSON.stringify(body) });
    drawHistory(d.readings); $("an-add-form").reset(); toast("Earlier reading added – press Check battery");
  } catch (err) { $("ah-err").textContent = err.message; }
}
function bandBar(m) {
  if (m.typical_low === null) return "";
  const lo = Math.min(m.typical_low, m.value), hi = Math.max(m.typical_high, m.value);
  const pad = (hi - lo) * 0.15 || 1, a = lo - pad, b = hi + pad, pc = (v) => ((v - a) / (b - a)) * 100;
  return `<div class="band" title="Typical ${fmt(m.typical_low)}–${fmt(m.typical_high)} ${esc(m.unit)}; yours ${fmt(m.value, 1)}">
    <div class="track"></div><div class="typ" style="left:${pc(m.typical_low)}%;width:${pc(m.typical_high) - pc(m.typical_low)}%"></div>
    <div class="val ${m.position !== "typical" ? "out" : ""}" style="left:${pc(m.value)}%"></div></div>`;
}
function statusRing(cls, icon) {
  return `<svg viewBox="0 0 128 128" class="gauge-svg" aria-hidden="true"><circle cx="64" cy="64" r="52" class="g-track"/>
    <circle cx="64" cy="64" r="52" class="g-val" stroke-dasharray="326.7 326.7"/></svg>
    <div class="g-text"><b class="g-icon">${icon}</b></div>`;
}
function drawAnomaly() {
  const r = cache.anomaly; if (!r) return;
  const s = STATUS[r.status] || STATUS.check_inputs;
  const ring = { good: "good", warning: "warning", critical: "critical" }[s.cls] || "neutral";
  const checks = (r.checks || []).filter((c) => c.status !== "not_checked").map((c) => {
    const cs = STATUS[c.status] || STATUS.not_checked;
    return `<li><span>${esc(c.check)}</span>${badge(cs.cls, cs.label)}</li>`;
  }).join("");
  const t = r.trip, alerts = (r.alerts || []).filter((x) => x && x.trim() !== (r.message || "").trim());
  $("an-result").innerHTML = `
    <div class="card">
      <div class="an-head">
        <div class="gauge small ${ring}" role="img" aria-label="Result: ${esc(s.label)}">${statusRing(s.cls, s.icon)}</div>
        <div><p class="an-label ${s.cls}">${esc(s.label)}</p>${r.vehicle ? `<h2>${esc(r.vehicle)}</h2>` : ""}<p class="an-msg">${esc(r.message)}</p></div>
      </div>
      ${alerts.length ? `<ul class="an-alerts">${alerts.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    </div>
    ${checks ? `<div class="card"><h2>What was checked</h2><ul class="checklist">${checks}</ul></div>` : ""}
    ${t ? (() => { const mx = Math.max(t.promised_km, t.delivered_km) * 1.1, ts = STATUS[t.status] || STATUS.not_checked;
      return `<div class="card"><div class="rul-title"><h2>Trip check</h2>${badge(ts.cls, ts.label)}</div>
      <div class="tripbars">
        <span>Promised by dashboard</span><div class="bar"><i style="width:${t.promised_km / mx * 100}%;background:${css("--s1")}"></i></div><b class="num">${fmt(t.promised_km)} km</b>
        <span>Actually delivered</span><div class="bar"><i style="width:${t.delivered_km / mx * 100}%;background:${css("--s2")}"></i></div><b class="num">${fmt(t.delivered_km)} km</b>
      </div>
      <p style="margin:10px 0 0">Range gap <b>${t.gap_pct > 0 ? "−" : "+"}${fmt(Math.abs(t.gap_pct), 1)}%</b> per full charge</p></div>`; })() : ""}`;
  $("an-result").hidden = false;
}
loaders.anomaly = async () => {};

// ================================================================ DEMAND
function demandChart(id, d) {
  const fc = d.forecast;
  chart(id, {
    type: "bar",
    data: { labels: fc.map((f) => `${f.weekday} ${f.date.slice(5)}`), datasets: [
      { label: "Expected sessions (Random Forest)", data: fc.map((f) => f.predicted_sessions), backgroundColor: css("--s1"), borderRadius: 4, maxBarThickness: 48 },
      { label: "Actual on the matching 2019 day", type: "line", data: fc.map((f) => f.actual_on_analogous_day), borderColor: css("--s2"), backgroundColor: css("--s2"), borderWidth: 0, pointRadius: 5, pointHoverRadius: 7, showLine: false },
    ] },
    options: (() => { const o = baseOptions({ yTitle: "Charging sessions per day", yMin: 0 });
      o.plugins.tooltip.callbacks = { label: (i) => i.datasetIndex === 0 ? ` Expected: ${fmt(i.raw, 1)} sessions (${fc[i.dataIndex].level})`
        : ` Actual on ${fc[i.dataIndex].analogous_day}: ${fmt(i.raw)} sessions` };
      return o; })(),
  });
}
loaders.demand = async () => {
  const o = cache.planOptions || (cache.planOptions = await api("/api/plan/options"));
  $("sd-date").innerHTML = o.forecast_days.map((d) => `<option value="${esc(d.date)}">${esc(dayLabel(d))}</option>`).join("");
  $("sd-vt").value = profile.vehicle_type || "4W";
  if (!$("sd-q").value && $("st-q") && $("st-q").value && $("st-q").value !== "Your current location") $("sd-q").value = $("st-q").value;
  loadAdaptive(); adAt = Date.now();
  await getDemand(); drawDemand();
};
// access label for chargers at a car showroom or at a hotel / office / store
const accessTag = (s) => s.access === "dealership" ? ` <span class="badge warning tag" title="${esc(s.access_note || "")}">Dealership</span>`
  : s.access === "venue" ? ` <span class="badge neutral tag" title="${esc(s.access_note || "")}">Venue</span>` : "";
// where the stations come from (Open Charge Map, OpenStreetMap, Google Maps)
function sourceMix(stations, r) {
  const n = {}; stations.forEach((s) => { const k = s.source || "Open Charge Map"; n[k] = (n[k] || 0) + 1; });
  let txt = Object.entries(n).map(([k, v]) => `${v} from ${k}`).join(" · ");
  const o = r && r.station_sources && r.station_sources.openstreetmap;
  if (o && o.ok === false) txt += ` · OpenStreetMap: ${o.error}${o.retry_in_s ? ` – retrying in ${Math.ceil(o.retry_in_s / 60)} min` : ""}`;
  else if (o && o.ok && !n.OpenStreetMap) txt += " · OpenStreetMap: no extra stations here";
  const hid = (r && r.station_sources && r.station_sources.hidden) || [];
  if (hid.length) txt += ` · ${hid.length} left out (residential / private / not a charging station)`;
  const t = r && r.station_sources && r.station_sources.tomtom;
  if (t && t.configured && t.ok === false) txt += ` · ${t.error}`;
  else if (t && t.configured && t.ok && !n.TomTom) txt += " · TomTom: no extra stations here";
  return txt;
}
// nearest stations first; long lists show the nearest 15 with a "show all" switch
const NEAREST_N = 15, showAll = { demand: false, pricing: false, stations: false, plan: false };    // nearest 15 / top 10 first
function moreButton(key, r) {
  if (r.stations.length <= NEAREST_N) return `<p class="small muted">All ${r.stations.length} stations within the radius, nearest first.</p>`;
  return `<p class="small muted">${showAll[key] ? `All ${r.stations.length} stations` : `Nearest ${NEAREST_N} of ${r.stations.length} stations`} within the radius, nearest first.
    <button class="btn small ghost" type="button" data-showall="${key}">${showAll[key] ? `Show nearest ${NEAREST_N} only` : `Show all ${r.stations.length}`}</button></p>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-showall]"); if (!b) return;
  showAll[b.dataset.showall] = !showAll[b.dataset.showall];
  ({ demand: drawStationDemand, pricing: drawStationPrices, stations: drawStations, plan: drawPlan })[b.dataset.showall]();
});
// short area under a station name: first address part that is a real name (not a house number)
function stArea(s) {
  const name = String(s.name || "").toLowerCase();
  const part = String(s.address || "").split(",").map((x) => x.trim())
    .find((x) => x.length >= 3 && !/^[\d\s\-\/#.]+$/.test(x) && !/^(no|plot|h\.?no|door)\b/i.test(x) && x.toLowerCase() !== name);
  return part ? part.slice(0, 48) : "";
}
const dayLabel = (d) => { const t = new Date(d.date + "T00:00:00"); return isNaN(t) ? `${d.weekday} ${d.date}` : t.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" }); };
// ---------------------------------------------------------------- demand at stations near you
let sdCoords = null, sdTyped = "";
const COMPAT = { compatible: ["good", "Compatible"], check: ["warning", "Check connector"], unknown: ["neutral", "Connector unknown"], not_compatible: ["critical", "Not compatible"] };
function drawStationDemand() {
  const r0 = cache.stationDemand; if (!r0) return;
  const fits = r0.stations.filter((s) => s.compatibility !== "not_compatible");     // only stations the vehicle can use
  const r = { ...r0, stations: fits, station_count: fits.length };
  const st = showAll.demand ? r.stations : r.stations.slice(0, NEAREST_N), fh = r.focus_hour;
  const placeName = (sdTyped && !/current location/i.test(sdTyped) ? sdTyped : r.inputs.location).split(",")[0].trim();
  const near = /current location/i.test(r.inputs.location) ? "Stations nearby" : `Stations near ${esc(placeName)}`;
  const area = stArea;
  if (!st.length) { $("sd-result").innerHTML = `<div class="card"><div class="alert warning"><span class="ico">!</span><span>No stations found here. Try a larger radius.</span></div></div>`; $("sd-result").hidden = false; return; }
  const all = st.flatMap((s) => s.profile.map((x) => x.utilisation_pct)), mx = Math.max(...all, 1);
  const ramp = [0, 1, 2, 3, 4, 5, 6].map((i) => css(`--ramp-${i}`));
  const cell = (s, x) => {
    const k = Math.min(6, Math.floor((x.utilisation_pct / mx) * 6.999));
    const live = x.source === "live" || x.source === "live-adjusted";
    const tip = `${s.name} · ${hh(x.hour)}: ${x.level} demand, ${fmt(x.utilisation_pct, 0)}% of points busy · ${x.wait_label}${x.source === "live" ? " · live" : x.source === "live-adjusted" ? " · adjusted to live data" : x.source === "calibrated" ? " · estimate learned from past live readings" : " · estimated"}`;
    return `<div class="c ${x.level === "high" ? "high" : ""} ${x.hour === fh ? "focus" : ""} ${live ? "live" : ""}" style="background:${ramp[k]}" title="${esc(tip)}"></div>`;
  };
  let heat = `<div></div>` + Array.from({ length: 24 }, (_, h) => `<div class="h ${h === fh ? "now" : ""}">${h % 3 === 0 || h === fh ? h : ""}</div>`).join("");
  st.forEach((s) => { heat += `<div class="n" title="${esc(s.name)}">${esc(s.name)} <small>${fmt(s.distance_km, 1)} km</small></div>` + s.profile.map((x) => cell(s, x)).join(""); });
  const isSim = (s) => s.live_source === "ocpp" && s.live_used && s.live_used.simulated;
  const lv = { high: 0, medium: 0, low: 0 }; r.stations.forEach((s) => { lv[s.focus.level] = (lv[s.focus.level] || 0) + 1; });
  const simN = st.filter(isSim).length, liveN = st.filter((s) => s.live_source && !isSim(s)).length;
  const liveTag = (s) => isSim(s) ? ` <span class="badge good tag">Live</span>`
    : s.focus.source === "live" || s.focus.source === "live-adjusted" ? ` <span class="badge good tag">Live</span>`
    : s.focus.source === "learned" ? ` <span class="badge neutral tag">Learned</span>` : "";
  const focusLabel = r.is_today ? `now (${hh(fh)})` : `at ${hh(fh)}`;
  const src = (s) => {
    const x = s.focus;
    if (x.source === "live") return `${srcBadge("live")} <span class="small muted">${liveName(s)}</span>`;
    return srcBadge(x.source);
  };
  $("sd-result").innerHTML = `${autoBar("demand")}
    <div class="tiles">
      <div class="tile"><div class="k">${near}</div><div class="v">${r.station_count}</div><div class="r">${esc(r.inputs.weekday)} ${esc(r.inputs.date)}</div></div>
      <div class="tile"><div class="k">Demand ${esc(focusLabel)}</div><div class="v lv3"><span class="lv high">${lv.high}</span> high <span class="lv medium">${lv.medium}</span> medium <span class="lv low">${lv.low}</span> low</div></div>
      <div class="tile"><div class="k">Live data</div><div class="v">${liveN + simN} <small>of ${r.station_count} stations</small></div>${liveN ? `<div class="r">${liveN} from driver reports</div>` : ""}</div>
      <div class="tile" id="ad-tile">${adaptiveTile()}</div>
    </div>
    <div class="card"><h2>Demand at each station ${esc(focusLabel)}</h2>
      ${table([{ label: "Station", html: (s) => esc(s.name) + accessTag(s) + liveTag(s) + (area(s) ? `<div class="st-addr">${esc(area(s))}</div>` : "") }, { label: "Distance", get: (s) => fmt(s.distance_km, 1) + " km", n: 1 },
        { label: "Demand", html: (s) => `${levelChip(s.focus.level)} <span class="small muted">${fmt(s.focus.utilisation_pct, 0)}% busy</span>` },
        { label: "Wait", get: (s) => String(s.focus.wait_label || "").replace(/ \(live\)$/, "") },
        { label: "Quietest hour", get: (s) => `${hh(s.quietest.hour)} (${fmt(s.quietest.utilisation_pct, 0)}%)` }], st, { hl: (s) => s.focus.level === "high" })}
      ${moreButton("demand", r)}</div>
    <div class="card"><h2>Hour by hour – ${esc(r.inputs.weekday)} ${esc(r.inputs.date)}</h2>
      <div class="heat-wrap"><div class="sd-heat">${heat}</div></div>
      <div class="sd-key"><span><i class="grad"></i>Quiet → busy</span><span><i class="k-high"></i>High demand</span>
        <span><i class="k-live"></i>Live</span><span><i class="k-focus"></i>${r.is_today ? "Now" : "Selected hour"}</span></div></div>`;
  $("sd-result").hidden = false;
}
async function submitStationDemand(e) {
  e.preventDefault();
  const q = $("sd-q").value.trim(), btn = $("sd-btn");
  $("sd-err").textContent = "";
  const p = new URLSearchParams({ vehicle_type: $("sd-vt").value, scenario: "expected", distance_km: $("sd-rad").value });
  if ($("sd-date").value) p.set("date", $("sd-date").value);
  if (sdCoords && q === "Your current location") { p.set("latitude", sdCoords[0]); p.set("longitude", sdCoords[1]); p.set("q", q); }
  else if (q.length >= 2) p.set("q", q);
  else { $("sd-err").textContent = "Enter a place, area or address (or use ◎ for your location)."; return; }
  btn.disabled = true; btn.textContent = "Loading…";
  try { lastQ.demand = "/api/demand/stations?" + p; sdTyped = q; cache.stationDemand = await api(lastQ.demand); lastAt.demand = Date.now(); drawStationDemand(); }
  catch (err) { $("sd-err").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = "Show demand"; }
}
function drawDemand() {
  const d = cache.demand; if (!d || !$("dm-test")) return;
  const T = d.test_predictions;
  chart("dm-test", { type: "line",
    data: { labels: T.map((t) => t.date), datasets: [
      { label: "Actual", data: T.map((t) => t.actual), borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 },
      { label: "Predicted", data: T.map((t) => t.predicted), borderColor: css("--s2"), backgroundColor: css("--s2"), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 }] },
    options: (() => { const o = baseOptions({ yTitle: "Sessions per day", yMin: 0 });
      o.scales.x.ticks.callback = function (v) { return this.getLabelForValue(v).slice(5); }; return o; })() });
}
drawers.demand = () => { drawDemand(); drawStationDemand(); };

// ================================================================ PRICING
let prTimer = null;
function prParams() {
  const p = new URLSearchParams({ hour: $("pr-hour").value, base_price: $("pr-base").value || 10, gst_pct: $("pr-gst").value || 0 });
  if ($("pr-date").value) p.set("date", $("pr-date").value);
  if ($("pr-level").value) p.set("demand_level", $("pr-level").value);
  if (Number($("pr-kwh").value) > 0) p.set("energy_kwh", $("pr-kwh").value);
  return p;
}
async function updatePricing() {
  $("pr-hour-label").textContent = hh($("pr-hour").value);
  $("pr-err").textContent = "";
  try { cache.pricing = await api("/api/pricing?" + prParams()); drawPricing(); }
  catch (err) { $("pr-err").textContent = err.message; }
}
function drawPricing() {
  const P = cache.pricing; if (!P) return;
  if (!$("pr-date").options.length) $("pr-date").innerHTML = P.comparison_by_day.map((d) => `<option value="${esc(d.date)}">${esc(dayLabel(d))}</option>`).join("");
}
drawers.pricing = () => { drawPricing(); drawStationPrices(); };
// ---------------------------------------------------------------- prices at stations near you
let psCoords = null, psTimer = null;
function psParams() {
  const q = $("ps-q").value.trim();
  const p = new URLSearchParams({ vehicle_type: $("ps-vt").value, distance_km: $("ps-rad").value,
    base_price: $("pr-base").value || 10, gst_pct: $("pr-gst").value || 18, hour: $("pr-hour").value });
  if ($("pr-date").value) p.set("date", $("pr-date").value);
  if (psCoords && q === "Your current location") { p.set("latitude", psCoords[0]); p.set("longitude", psCoords[1]); p.set("q", q); }
  else if (q.length >= 2) p.set("q", q);
  else return null;
  return p;
}
async function loadStationPrices() {
  const p = psParams(), btn = $("ps-btn");
  $("ps-err").textContent = "";
  if (!p) { $("ps-err").textContent = "Enter a place, area or address (or use ◎ for your location)."; return; }
  btn.disabled = true; btn.textContent = "Loading…";
  try { lastQ.pricing = "/api/pricing/stations?" + p; cache.stationPrices = await api(lastQ.pricing); lastAt.pricing = Date.now(); drawStationPrices(); }
  catch (err) { $("ps-err").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = "Show prices"; }
}
function drawStationPrices() {
  const r0 = cache.stationPrices; if (!r0) return;
  const fits = r0.stations.filter((s) => s.compatibility !== "not_compatible");
  const r = { ...r0, stations: fits };
  const st = showAll.pricing ? r.stations : r.stations.slice(0, NEAREST_N), h = r.inputs.hour, tb = r.time_band;
  if (!st.length) { $("ps-result").innerHTML = `<div class="card"><div class="alert warning"><span class="ico">!</span><span>No stations found here. Try a larger radius.</span></div></div>`; $("ps-result").hidden = false; return; }
  const cheapest = r.stations.reduce((a, b) => (b.price.price_per_kwh < a.price.price_per_kwh ? b : a));
  const dearest = r.stations.reduce((a, b) => (b.price.price_per_kwh > a.price.price_per_kwh ? b : a));
  const liveN = r.stations.filter((s) => s.live_source).length;
  const isNow = h === new Date().getHours() && r.is_today;
  const area = stArea;
  const liveTag = (s) => s.live_source && isNow ? ` <span class="badge good tag">Live</span>` : "";
  const prices = st.flatMap((s) => s.profile.map((x) => x.price_per_kwh)), mn = Math.min(...prices), mx = Math.max(...prices);
  const ramp = [0, 1, 2, 3, 4, 5, 6].map((i) => css(`--ramp-${i}`));
  let heat = `<div></div>` + Array.from({ length: 24 }, (_, k) => `<div class="h ${k === h ? "now" : ""}">${k % 3 === 0 || k === h ? k : ""}</div>`).join("");
  st.forEach((s) => {
    heat += `<div class="n" title="${esc(s.name)}">${esc(s.name)} <small>${fmt(s.distance_km, 1)} km</small></div>` + s.profile.map((x) => {
      const k = Math.min(6, Math.floor(((x.price_per_kwh - mn) / (mx - mn || 1)) * 6.999));
      const live = x.source === "live" || x.source === "live-adjusted";
      return `<div class="c ${x.level === "high" ? "high" : ""} ${x.hour === h ? "focus" : ""} ${live ? "live" : ""}" style="background:${ramp[k]}" title="${esc(`${s.name} · ${hh(x.hour)}: ₹${fmt(x.price_per_kwh, 2)}/kWh · ${x.level} demand`)}"></div>`;
    }).join("");
  });
  const cols = [
    { label: "Station", html: (s) => esc(s.name) + accessTag(s) + liveTag(s) + (area(s) ? `<div class="st-addr">${esc(area(s))}</div>` : "") },
    { label: "Distance", get: (s) => fmt(s.distance_km, 1) + " km", n: 1 },
    { label: "Demand", html: (s) => `${levelChip(s.focus.level)} <span class="small muted">${fmt(s.focus.utilisation_pct, 0)}% busy</span>` },
    { label: isNow ? "Price now" : `Price at ${hh(h)}`, html: (s) => `<span class="price-big">₹${fmt(s.price.price_per_kwh, 2)}</span> <span class="small muted">/kWh</span>${s.tariff && !s.tariff.simulated ? `<div class="st-addr">${{ open_charge_map: "listed", driver_report: "drivers paid", ocpp: "tariff" }[s.tariff.source] || "tariff"} ₹${fmt(s.tariff.price_per_kwh, 2)}</div>` : ""}` },
    { label: r.is_today ? "Cheapest later today" : "Cheapest hour", html: (s) => `${hh(s.cheapest_hour.hour)} <span class="small muted">₹${fmt(s.cheapest_hour.price_per_kwh, 2)}</span>` }];
  $("ps-result").innerHTML = `${autoBar("pricing")}
    <div class="tiles">
      <div class="tile"><div class="k">Cheapest ${isNow ? "now" : "at " + hh(h)}</div><div class="v">₹${fmt(cheapest.price.price_per_kwh, 2)} <small>/kWh</small></div><div class="r">${esc(cheapest.name)}</div></div>
      <div class="tile"><div class="k">Most expensive</div><div class="v">₹${fmt(dearest.price.price_per_kwh, 2)} <small>/kWh</small></div><div class="r">${esc(dearest.name)}</div></div>
      <div class="tile"><div class="k">Time of day</div><div class="v">${esc(tb.label)}</div><div class="r">${esc(dayLabel(r.inputs))} · ${hh(h)}</div></div>
      <div class="tile"><div class="k">Live data</div><div class="v">${liveN} <small>of ${r.stations.length} stations</small></div></div>
    </div>
    <div class="card"><div class="card-head"><h2>Price at each station ${isNow ? `now (${hh(h)})` : `at ${hh(h)}`}</h2>
      ${isNow ? "" : `<button class="btn small ghost" type="button" data-now="pr-hour">Back to now (${hh(new Date().getHours())})</button>`}</div>
      ${table(cols, st, { hl: (s) => s === cheapest })}
      ${moreButton("pricing", r)}</div>
    <div class="card"><h2>Prices through the day</h2>
      <div class="heat-wrap"><div class="sd-heat">${heat}</div></div>
      <div class="sd-key"><span><i class="grad"></i>Cheaper → dearer</span><span><i class="k-high"></i>High demand</span>
        <span><i class="k-live"></i>Live</span><span><i class="k-focus"></i>Selected hour</span></div></div>`;
  $("ps-result").hidden = false;
}
loaders.pricing = async () => {
  if (followNow["pr-hour"]) $("pr-hour").value = new Date().getHours();   // live: priced for the current hour
  $("ps-vt").value = profile.vehicle_type || "4W";
  const known = ["sd-q", "st-q"].map((id) => $(id) && $(id).value).find((v) => v && v !== "Your current location");
  if (!$("ps-q").value && known) $("ps-q").value = known;
  await updatePricing();
};

// ================================================================ STATION FINDER
const maps = {};
function stationMap(elId, inputs, stations) {
  if (!stations.length || typeof L === "undefined") return null;
  if (maps[elId]) { maps[elId].remove(); }
  const m = L.map(elId, { scrollWheelZoom: false });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" }).addTo(m);
  const me = [inputs.latitude, inputs.longitude], pts = [me];
  L.circleMarker(me, { radius: 9, color: "#fff", weight: 3, fillColor: css("--s2"), fillOpacity: 1 }).bindPopup(`<b>You</b><br>${esc(inputs.location)}`).addTo(m);
  stations.forEach((s) => {
    const rk = s.recommendation_rank;
    const icon = L.divIcon({ className: "", html: `<div class="rank" style="width:28px;height:28px;font-size:13px;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.3);${rk ? "" : "background:var(--muted)"}">${rk || "–"}</div>`, iconSize: [28, 28], iconAnchor: [14, 14] });
    L.marker([s.latitude, s.longitude], { icon }).bindPopup(`<b>${rk ? "#" + rk + " " : ""}${esc(s.name)}</b><br>₹${fmt(s.price.price_per_kwh, 2)}/kWh · ${esc(s.demand.wait_label)}`).addTo(m);
    pts.push([s.latitude, s.longitude]);
  });
  m.fitBounds(pts, { padding: [30, 30], maxZoom: 15 });
  setTimeout(() => m.invalidateSize(), 60);
  maps[elId] = m;
  return m;
}
let stCoords = null;
loaders.stations = async () => {
  const o = cache.planOptions || (cache.planOptions = await api("/api/plan/options"));
  $("st-date").innerHTML = o.forecast_days.map((d) => `<option value="${esc(d.date)}">${esc(dayLabel(d))}</option>`).join("");
  $("st-scen").innerHTML = Object.entries(o.scenarios).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("st-vt").value = profile.vehicle_type || "4W";
};
const liveSummary = (lv) => !lv ? "" : `<div class="tile"><div class="k">Live data</div><div class="v">${lv.stations_with_live} <small>of ${lv.stations_total} stations</small></div>
  <div class="r">${lv.stations_from_ocpp ? `${lv.stations_from_ocpp} from charger feeds · ` : ""}${lv.stations_from_checkins ? `${lv.stations_from_checkins} from driver reports · ` : ""}${lv.stations_learned ? `${lv.stations_learned} learned · ` : ""}Google: ${lv.google === "on" ? "on" : lv.google === "error" ? "error" : "off (no key)"} · ${lv.is_now ? "used for the current hour" : "adjusts the forecast for your time"}</div></div>`;
function demandSummary(stations) {
  if (!stations.length) return "";
  const src = (s) => {
    const d = s.demand;
    if (d.source === "live") {
      const lu = s.live_used || {};
      const ago = s.live_source === "ocpp" ? `, ${fmt(lu.updated_s_ago)} s ago`
        : (s.live_source === "driver_checkins" ? s.driver_reports && s.driver_reports.latest.minutes_ago : lu.updated_min_ago) != null
          ? `, ${fmt(s.live_source === "driver_checkins" ? s.driver_reports.latest.minutes_ago : lu.updated_min_ago)} min ago` : "";
      return `${srcBadge("live")} <span class="small muted">${liveName(s)}${ago}</span>`;
    }
    return srcBadge(d.source);
  };
  const best = stations.find((s) => s.recommendation_rank === 1);
  return `<div class="card"><h2>Station demand at a glance</h2>
    ${table([{ label: "#", get: (s) => s.recommendation_rank || "–" }, { label: "Station", key: "name" },
      { label: "Distance", get: (s) => fmt(s.distance_km, 1) + " km", n: 1 },
      { label: "Demand", html: (s) => `${levelChip(s.demand.level)} <span class="small muted">${fmt(s.demand.utilisation_pct, 0)}% busy</span>` },
      { label: "Wait", get: (s) => s.demand.wait_label }, { label: "Tariff → dynamic price", html: (s) => `<span class="small muted">${s.price.station_tariff ? "₹" + fmt(s.price.station_tariff.price_per_kwh, 2) : "–"} →</span> <b>₹${fmt(s.price.price_per_kwh, 2)}</b>`, n: 1 },
      { label: "Source", html: src }], stations, { hl: (s) => s.recommendation_rank === 1 })}
    ${best ? `<p style="margin-top:10px"><b>Best overall: ${esc(best.name)}</b> <span class="muted small">– ${esc(best.why)}</span></p>` : ""}
    <p class="small muted">Demand = points in use ÷ total points. <b>Live</b>: from the charger's own feed (OCPP), a driver report in the last 45 min or Google Maps. <b>Estimated</b>: queueing model (charging patterns learned from real sessions × expected demand for the day × the station's points and power); <b>learned</b> when the station's past live readings have recalibrated it. Report a station's status on its card below to make it live.</p></div>`;
}
function drawStations() {
  const r0 = cache.stations; if (!r0) return;
  const fits = r0.stations.filter((s) => s.compatibility !== "not_compatible");
  const best = fits.find((s) => s.recommendation_rank);
  const list = showAll.stations ? fits : fits.slice(0, 10), liveN = fits.filter((s) => s.live_source).length;
  const more = fits.length > 10 ? `<button class="btn small ghost" type="button" data-showall="stations">${showAll.stations ? "Show top 10 only" : `Show all ${fits.length} stations`}</button>` : "";
  $("st-result").innerHTML = `${autoBar("stations")}
    <div class="tiles">
      <div class="tile"><div class="k">Stations found</div><div class="v">${fits.length}</div><div class="r">within ${esc($("st-rad").value)} km</div></div>
      <div class="tile"><div class="k">Live data</div><div class="v">${liveN} <small>of ${fits.length} stations</small></div></div>
      ${best ? `<div class="tile top-pick"><div class="k">Top pick · ${esc(r0.inputs.preference_label)}</div><div class="v" style="font-size:18px">${esc(best.name)}</div>
        <div class="r">₹${fmt(best.price.price_per_kwh, 2)}/kWh · ${esc(String(best.demand.wait_label || "").replace(/ \(live\)$/, ""))} · ${fmt(best.distance_km, 1)} km</div></div>` : ""}
    </div>
    ${fits.length ? `<div class="grid-map"><div class="card map-card"><div id="st-map" class="st-map" role="region" aria-label="Map of stations"></div></div>
      <div class="st-list">${best ? `<div class="stack-legend sc-key"><b>Score:</b>${best.score_breakdown.filter((b) => b.weight_pct > 0).map((b) => `<span style="--c:${css(PL_COLOR[b.criterion])}">${esc(b.label)}</span>`).join("")}</div>` : ""}${list.map((s) => planStationCard(s, s === best)).join("")}${more ? `<div>${more}</div>` : ""}</div></div>`
      : `<div class="card"><div class="alert warning"><span class="ico">!</span><span>No stations for this vehicle here. Try a larger radius.</span></div></div>`}`;
  $("st-result").hidden = false;
  stationMap("st-map", r0.inputs, list);
}
drawers.stations = drawStations;
async function submitStations(e) {
  e.preventDefault();
  const q = $("st-q").value.trim(), btn = $("st-btn");
  $("st-err").textContent = "";
  const p = new URLSearchParams({ vehicle_type: $("st-vt").value, hour: $("st-hour").value, preference: $("st-pref").value,
    scenario: $("st-scen").value || "expected", distance_km: $("st-rad").value });
  if ($("st-date").value) p.set("date", $("st-date").value);
  if ($("st-con").value) p.set("connector", $("st-con").value);
  if (stCoords && q === "Your current location") { p.set("latitude", stCoords[0]); p.set("longitude", stCoords[1]); p.set("q", q); }
  else if (q.length >= 2) p.set("q", q);
  else { $("st-err").textContent = "Enter a place, area or address (or use ◎ for your location)."; return; }
  btn.disabled = true; btn.textContent = "Searching…";
  try { lastQ.stations = "/api/recommendations?" + p; cache.stations = await api(lastQ.stations); lastAt.stations = Date.now(); drawStations(); }
  catch (err) { $("st-err").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = "Find stations"; }
}
// ================================================================ live updates (every 30 s)
const AUTO_S = 30, lastQ = {}, lastAt = {};
let autoOn = store.get("auto", true), autoBusy = false, adAt = 0;
function autoBar(key) {
  return `<div class="autobar ${autoOn ? "" : "off"}"><label><input type="checkbox" class="auto-toggle" ${autoOn ? "checked" : ""}>
    <span class="pulse" aria-hidden="true"></span> Live updates every ${AUTO_S} s</label><span class="small muted" data-au="${key}">updated just now</span></div>`;
}
const AUTO_TARGET = {
  stations: ["st-result", (r) => {
    const m = maps["st-map"], view = m ? { c: m.getCenter(), z: m.getZoom() } : null;
    cache.stations = r; drawStations();
    if (view && maps["st-map"]) maps["st-map"].setView(view.c, view.z, { animate: false });
  }],
  demand: ["sd-result", (r) => { cache.stationDemand = r; drawStationDemand(); }],
  pricing: ["ps-result", (r) => { cache.stationPrices = r; drawStationPrices(); }],
};
document.addEventListener("change", (e) => {
  if (!e.target.classList.contains("auto-toggle")) return;
  autoOn = e.target.checked; store.set("auto", autoOn);
  document.querySelectorAll(".auto-toggle").forEach((x) => { x.checked = autoOn; x.closest(".autobar").classList.toggle("off", !autoOn); });
});
// the hour follows the clock (live) until the user picks another hour; "Back to now" returns to live
const followNow = { "pr-hour": true, "st-hour": true };
function goNow(id) {
  followNow[id] = true;
  $(id).value = new Date().getHours();
  if (id === "pr-hour") { updatePricing(); if (lastQ.pricing) loadStationPrices(); }
  if (id === "st-hour" && lastQ.stations) $("st-form").requestSubmit();
}
document.addEventListener("click", (e) => { const b = e.target.closest("[data-now]"); if (b) goNow(b.dataset.now); });
["pr-hour", "st-hour"].forEach((id) => document.addEventListener("input", (e) => {
  if (e.target.id === id) followNow[id] = Number(e.target.value) === new Date().getHours();
}));
setInterval(async () => {
  const nowH = new Date().getHours();
  Object.keys(followNow).forEach((id) => { if (followNow[id] && $(id) && Number($(id).value) !== nowH) goNow(id); });
  document.querySelectorAll("[data-au]").forEach((el) => {
    const a = lastAt[el.dataset.au];
    el.textContent = !autoOn ? "paused" : a ? `updated ${Math.max(0, Math.round((Date.now() - a) / 1000))} s ago` : "";
  });
  const page = location.hash.slice(1);
  if (page === "demand" && !document.hidden && Date.now() - adAt > AUTO_S * 1000) { adAt = Date.now(); loadAdaptive(); }
  const t = AUTO_TARGET[page];
  if (!t || !autoOn || document.hidden || autoBusy || !lastQ[page] || Date.now() - (lastAt[page] || 0) < AUTO_S * 1000) return;
  const box = $(t[0]);
  if (!box || box.hidden || box.contains(document.activeElement) || box.querySelector("details.ci[open]")) return;   // don't disturb typing
  autoBusy = true;
  try { const r = await api(lastQ[page]); lastAt[page] = Date.now(); t[1](r); }
  catch { /* keep the last result; try again next time */ }
  finally { autoBusy = false; }
}, 1000);

// ---------------------------------------------------------------- adaptive learning card
async function loadAdaptive() {
  try { cache.adaptive = await api("/api/adaptive"); drawAdaptive(); } catch { /* tile keeps its last value */ }
}
function adaptiveTile() {
  const a = cache.adaptive;
  if (!a) return `<div class="k">Adaptive learning</div><div class="v">–</div>`;
  if (!a.observations) return `<div class="k">Adaptive learning</div><div class="v">0 <small>driver reports</small></div>
    <div class="r">Report a station in <a href="#stations">Station finder</a> to start learning</div>`;
  return `<div class="k">Adaptive learning</div><div class="v">${fmt(a.observations)} <small>driver reports</small></div>
    <div class="r">${fmt(a.stations_calibrated)} station${a.stations_calibrated === 1 ? "" : "s"} learned</div>`;
}
function drawAdaptive() { if ($("ad-tile")) $("ad-tile").innerHTML = adaptiveTile(); }

// ================================================================ account + theme
const SYNCED = ["profile", "auto", "theme", "places", "lastRul", "lastAnomaly"];   // saved to the user's account (same on every device)
let account = null, prefsTimer = null;
function savePrefsSoon() {
  if (!account) return;
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(() => {
    const prefs = {}; SYNCED.forEach((k) => { const v = store.get(k, undefined); if (v !== undefined) prefs[k] = v; });
    api("/api/auth/preferences", { method: "PUT", body: JSON.stringify({ preferences: prefs }) }).catch(() => {});
  }, 700);
}
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
  document.querySelectorAll("[data-theme-set]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.themeSet === (t || "system"))));
  const page = location.hash.slice(1);
  if (drawers[page] && !document.body.classList.contains("auth-mode")) setTimeout(() => { try { drawers[page](); } catch { /* not loaded yet */ } }, 0);   // charts read the colours when drawn
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-theme-set]"); if (!b) return;
  store.set("theme", b.dataset.themeSet); applyTheme(b.dataset.themeSet);
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (store.get("theme", "system") === "system") applyTheme("system"); });
function showAuth() {
  if (document.body.classList.contains("auth-mode")) return;
  account = null;
  document.body.classList.add("auth-mode"); $("auth").hidden = false; $("user-box").hidden = true;
  document.title = "Log in – Smart EV Energy Management";
  setTimeout(() => $("auth-email").focus(), 50);
}
function showApp(user) {
  account = user;
  document.body.classList.remove("auth-mode"); $("auth").hidden = true;
  $("user-box").hidden = false; $("user-name").textContent = user.name || user.email;
  $("user-avatar").textContent = (user.name || user.email).trim()[0].toUpperCase();
}
function applyAccountPrefs(user) {
  // the account's settings win over this browser's; returns true if the page reloads to use them
  const p = user.preferences || {};
  let changed = false;
  SYNCED.forEach((k) => {
    if (p[k] === undefined) return;
    if (JSON.stringify(store.get(k, undefined)) !== JSON.stringify(p[k])) { try { localStorage.setItem("ev." + k, JSON.stringify(p[k])); } catch { /* ignore */ } changed = true; }
  });
  if (!Object.keys(p).length) { account = user; savePrefsSoon(); }      // first login: keep this browser's settings
  if (changed && !sessionStorage.getItem("ev.prefsApplied")) { sessionStorage.setItem("ev.prefsApplied", "1"); location.reload(); return true; }
  sessionStorage.removeItem("ev.prefsApplied");
  return false;
}
let authMode = "login";
function setAuthMode(m) {
  authMode = m;
  $("tab-login").setAttribute("aria-selected", String(m === "login")); $("tab-register").setAttribute("aria-selected", String(m === "register"));
  $("auth-name-field").hidden = m !== "register"; $("auth-pw-hint").hidden = m !== "register";
  $("auth-password").autocomplete = m === "register" ? "new-password" : "current-password";
  $("auth-btn").textContent = m === "register" ? "Create account" : "Log in"; $("auth-err").textContent = "";
}
document.addEventListener("click", (e) => { const t = e.target.closest("[data-auth-tab]"); if (t) setAuthMode(t.dataset.authTab); });
async function submitAuth(e) {
  e.preventDefault();
  const email = $("auth-email").value.trim(), password = $("auth-password").value, name = $("auth-name").value.trim(), btn = $("auth-btn");
  $("auth-err").textContent = "";
  if (!email || !password || (authMode === "register" && !name)) { $("auth-err").textContent = "Fill in every field."; return; }
  if (authMode === "register" && password.length < 8) { $("auth-err").textContent = "Use a password of at least 8 characters."; return; }
  btn.disabled = true;
  try {
    const body = authMode === "register" ? { email, name, password, legacy_vehicle_id: store.get("vehicleId", null) || undefined } : { email, password };
    await api(`/api/auth/${authMode}`, { method: "POST", body: JSON.stringify(body) });
    location.reload();
  } catch (err) { $("auth-err").textContent = err.message; btn.disabled = false; }
}
async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* already logged out */ }
  try { Object.keys(localStorage).filter((k) => k.startsWith("ev.") && k !== "ev.theme").forEach((k) => localStorage.removeItem(k)); } catch { /* ignore */ }
  location.hash = ""; location.reload();
}
async function boot() {
  applyTheme(store.get("theme", "system"));
  $("auth-form").addEventListener("submit", submitAuth);
  $("logout-btn").addEventListener("click", logout);
  api("/api/health").then(() => {  })
    .catch(() => { toast("Cannot reach the server – is it running?"); });
  try {
    const { user } = await api("/api/auth/me");
    if (applyAccountPrefs(user)) return;
    applyTheme(store.get("theme", "system"));
    showApp(user);
    init();
  } catch (err) {
    if (err.status === 401) showAuth();
    else { toast("Cannot reach the server – is it running?"); toast(err.message); }
  }
}

// ================================================================ startup
function init() {
  if (typeof Chart !== "undefined") chartTheme();
  // the places searched on each page follow the account to other devices
  const places = store.get("places", {});
  ["sd-q", "ps-q", "st-q", "pl-q"].forEach((id) => {
    if (places[id] && !$(id).value) $(id).value = places[id];
    $(id).addEventListener("change", () => {
      const v = $(id).value.trim();
      if (v && v !== "Your current location") store.set("places", { ...store.get("places", {}), [id]: v });
    });
  });
  setupVehicleForm($("rul-form")); setupVehicleForm($("an-form")); setupVehicleForm($("pl-form"));
  try { viewer3d = window.Vehicle3D ? Vehicle3D.mount($("rul-3d"), () => ({ type: profile.vehicle_type, name: profile.vehicle_name })) : null; }
  catch (err) { viewer3d = null; }
  if (!viewer3d) $("rul-3d").hidden = true;          // no WebGL: the form works as before
  $("pl-newrange").value = profile.new_full_range_km || "";
  $("pl-newrange").addEventListener("input", (e) => { profile.new_full_range_km = e.target.value; store.set("profile", profile); $("an-newrange").value = e.target.value; });
  $("pl-hour").innerHTML = Array.from({ length: 24 }, (_, h) => `<option value="${h}">${hh(h)}</option>`).join("");
  $("pl-hour").value = new Date().getHours();
  $("pl-form").addEventListener("submit", submitPlan);
  $("pl-q").addEventListener("input", () => { plCoords = null; });
  $("pl-geo").addEventListener("click", () => {
    if (!navigator.geolocation) { $("pl-err").textContent = "Location is not available in this browser."; return; }
    navigator.geolocation.getCurrentPosition((pos) => { plCoords = [pos.coords.latitude, pos.coords.longitude]; $("pl-q").value = "Your current location"; },
      () => { $("pl-err").textContent = "Could not get your location. Type a place instead."; }, { timeout: 10000 });
  });
  $("an-newrange").value = profile.new_full_range_km || "";
  $("an-newrange").addEventListener("input", (e) => { profile.new_full_range_km = e.target.value; store.set("profile", profile); $("pl-newrange").value = e.target.value; });
  $("rul-form").addEventListener("submit", submitRul);
  document.addEventListener("change", (e) => {
    const f = e.target.closest(".ci-form"); if (!f || e.target.name !== "st") return;
    f.querySelector(".ci-free").hidden = e.target.value !== "free";
    f.querySelector(".ci-queue").hidden = e.target.value !== "queue";
  });
  document.addEventListener("submit", async (e) => {
    const f = e.target.closest(".ci-form"); if (!f) return;
    e.preventDefault();
    const st = f.querySelector("input[name=st]:checked").value;
    const num = (n) => { const v = f.querySelector(`input[name=${n}]`).value; return v === "" ? null : Number(v); };
    const body = { station_id: f.dataset.sid, station_name: f.dataset.name, latitude: Number(f.dataset.lat), longitude: Number(f.dataset.lon), status: st };
    if (st === "free") { body.free_points = num("free"); body.total_points = num("total"); }
    if (st === "queue") body.queue = num("queue") || 1;
    if (num("price") !== null) body.price_per_kwh = num("price");
    try {
      await api("/api/checkins", { method: "POST", body: JSON.stringify(body) });
      toast(body.price_per_kwh ? "Thanks – status and price reported. Updating…" : "Thanks – status reported. Updating…");
      const page = location.hash.slice(1);
      if (page === "stations") $("st-form").requestSubmit(); else if (page === "plan") $("pl-form").requestSubmit();
    } catch (err) { toast(err.message); }
  });
  $("an-form").addEventListener("submit", submitAnomaly);
  // keep the two copies of the vehicle form in step when switching pages
  window.addEventListener("hashchange", () => {
    ["rul-form", "an-form", "pl-form"].forEach((id) => fillVehicleForm($(id)));
    $("an-newrange").value = $("pl-newrange").value = profile.new_full_range_km || "";
  });

  ["pr-hour", "pr-date", "pr-level", "pr-base", "pr-gst", "pr-kwh"].forEach((id) => $(id).addEventListener("input", () => {
    $("pr-hour-label").textContent = hh($("pr-hour").value);
    clearTimeout(prTimer); prTimer = setTimeout(updatePricing, 250);
    if (cache.stationPrices) { clearTimeout(psTimer); psTimer = setTimeout(loadStationPrices, 600); }
  }));

  $("st-hour").innerHTML = Array.from({ length: 24 }, (_, h) => `<option value="${h}">${hh(h)}</option>`).join("");
  $("pr-hour").innerHTML = $("st-hour").innerHTML; $("pr-hour").value = new Date().getHours();
  $("st-hour").value = new Date().getHours();
  $("st-form").addEventListener("submit", submitStations);
  $("sd-form").addEventListener("submit", submitStationDemand);
  $("ps-form").addEventListener("submit", (e) => { e.preventDefault(); loadStationPrices(); });
  $("ps-q").addEventListener("input", () => { psCoords = null; });
  $("ps-geo").addEventListener("click", () => {
    if (!navigator.geolocation) { $("ps-err").textContent = "Location is not available in this browser."; return; }
    navigator.geolocation.getCurrentPosition((pos) => { psCoords = [pos.coords.latitude, pos.coords.longitude]; $("ps-q").value = "Your current location"; },
      () => { $("ps-err").textContent = "Could not get your location. Type a place instead."; }, { timeout: 10000 });
  });
  $("ps-vt").addEventListener("change", (e) => { profile.vehicle_type = e.target.value; store.set("profile", profile); });
  $("sd-q").addEventListener("input", () => { sdCoords = null; });
  $("sd-geo").addEventListener("click", () => {
    if (!navigator.geolocation) { $("sd-err").textContent = "Location is not available in this browser."; return; }
    navigator.geolocation.getCurrentPosition((pos) => { sdCoords = [pos.coords.latitude, pos.coords.longitude]; $("sd-q").value = "Your current location"; },
      () => { $("sd-err").textContent = "Could not get your location. Type a place instead."; }, { timeout: 10000 });
  });
  $("sd-vt").addEventListener("change", (e) => { profile.vehicle_type = e.target.value; store.set("profile", profile); });
  $("st-q").addEventListener("input", () => { stCoords = null; });
  $("st-geo").addEventListener("click", () => {
    if (!navigator.geolocation) { $("st-err").textContent = "Location is not available in this browser."; return; }
    navigator.geolocation.getCurrentPosition((pos) => { stCoords = [pos.coords.latitude, pos.coords.longitude]; $("st-q").value = "Your current location"; },
      () => { $("st-err").textContent = "Could not get your location. Type a place instead."; }, { timeout: 10000 });
  });
  $("st-vt").addEventListener("change", (e) => { profile.vehicle_type = e.target.value; store.set("profile", profile); });
  $("menu-btn").addEventListener("click", () => {
    const open = $("sidebar").classList.toggle("open"); $("menu-btn").setAttribute("aria-expanded", String(open));
  });
  window.addEventListener("hashchange", route);
  route();

  api("/api/health").then(() => {  })
    .catch(() => { toast("Cannot reach the server – is it running?"); });

  // redraw charts when the OS theme changes
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  (mq.addEventListener ? mq.addEventListener.bind(mq, "change") : mq.addListener.bind(mq))(() => {
    chartTheme();
    const page = location.hash.slice(1) || "overview";
    if (drawers[page]) drawers[page]();
    if (page === "plan" && cache.plan) drawPlan();
  });
}
boot();
