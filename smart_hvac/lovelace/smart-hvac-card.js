/* Smart HVAC native Lovelace card (0.4.1). No iframe, no ingress: reads hass state + recorder history,
 * NEVER calls HA climate services itself. Every write is a POST to the Smart HVAC add-on API through
 * Supervisor ingress, so the add-on's gates apply (off / dry run 409, manual hold, vacation 409,
 * radiant setpoint cap from /api/state, never above 85; Tesla exclusion). Admins also GET /api/state through
 * ingress for the cap, the next change / why and the 24 h event strip (0.4.0). The add-on panel is admin-only, so Supervisor only grants ingress
 * sessions to admins: for any other user (or if a session can't be made) the card is read-only and says so.
 *
 *   type: custom:smart-hvac-card
 *   zones:            # optional; defaults below
 *     - entity: climate.room_a_mini_split
 *       label: Room A
 *       indoor: sensor.room_a_temperature
 *       humidity: sensor.room_a_humidity
 *   weather: weather.home
 *   hours: 24
 *   layout: glance    # 0.6.5: one compact card (rows + one shared split mode); default = full cards
 */
export const ADDON_SLUG = "47b3c4e5_smart_hvac";
export const RADIANT_CAP = 85;
export const VACATION_ENTITY = "input_boolean.vacation_mode";
export const DEFAULT_ZONES = [
  { zone: "room_a", entity: "climate.room_a_mini_split", label: "Room A",
    indoor: "sensor.room_a_temperature", humidity: "sensor.room_a_humidity" },
  { zone: "room_b", entity: "climate.room_b_mini_split", label: "Room B",
    indoor: "sensor.room_b_temperature", humidity: "sensor.room_b_humidity" },
  { zone: "bath_floor", entity: "climate.bath_floor", label: "Bath floor", heat_only: true,
    indoor: "sensor.bath_temperature", humidity: "sensor.bath_humidity" },
];

export const PRESETS = [["home", "Home"], ["sleep", "Sleep"], ["away", "Away"], ["eco", "Eco"]];
export const DEFROST = { defrosting: "Defrosting", preheating: "Pre-heating" };

/** Radiant setpoint cap: the add-on's radiant_setpoint_max, never above the 85 hard ceiling (MA low 0.3.3). */
export function radiantCap(api) {
  const v = Number(api && api.radiant_setpoint_max);
  return Number.isFinite(v) && v > 0 ? Math.min(v, RADIANT_CAP) : RADIANT_CAP;
}

export function clampSetpoint(t, heatOnly, cap = RADIANT_CAP) {
  const hi = heatOnly ? Math.min(cap, RADIANT_CAP) : 84;
  return Math.max(55, Math.min(hi, Math.round(Number(t) * 2) / 2));
}

/** Card action -> add-on API request {path, body}, or null. The add-on re-validates everything. */
export function buildRequest(zone, action, value, ctx = {}) {
  const name = String(zone.zone || "");
  if (!/^[a-z0-9_]+$/.test(name)) return null;
  const heatOnly = !!zone.heat_only;
  let what, body;
  if (action === "setpoint") { what = "setpoint"; body = { temperature: clampSetpoint(value, heatOnly, ctx.cap) }; }
  else if (action === "mode") {
    const ok = heatOnly ? ["heat", "off"] : ["heat", "cool", "off"];
    if (!ok.includes(value)) return null;
    if (heatOnly && value !== "off" && ctx.vacation) return null;
    what = "mode"; body = { hvac_mode: value };
  } else if (action === "fan") {
    if (heatOnly || typeof value !== "string") return null;
    what = "fan"; body = { fan_mode: value };
  } else if (action === "resume") { what = "resume"; body = {}; }
  else return null;
  return { path: `api/zone/${name}/${what}`, body };
}

/** 0.6.0 shared-mode lock: on a mini split, Heat / Cool switch BOTH splits (one outdoor unit); Off is per room. */
export function modeLabel(m, heatOnly) {
  const t = m[0].toUpperCase() + m.slice(1);
  return !heatOnly && (m === "heat" || m === "cool") ? `${t} · both` : t;
}


/** 0.6.5 glance layout: one row per room (now, target, what it's doing) and ONE shared mode for the splits. */
export function sharedSplitMode(hass, zones) {
  const fam = { heat: "heat", cool: "cool", dry: "cool" };
  const f = new Set(zones.filter((z) => !z.heat_only).map((z) => fam[hass?.states?.[z.entity]?.state]).filter(Boolean));
  if (f.size === 1) return [...f][0];
  if (f.size === 0) return zones.some((z) => !z.heat_only && hass?.states?.[z.entity]?.state === "off") ? "off" : null;
  return "conflict"; // never expected: the add-on corrects it on its next cycle
}

/** Requests behind the shared mode control, sent one after another (not one call): heat / cool go to each split
    in turn and the add-on switches any split running the other direction off first; off turns each split off.
    The radiant floor is never included. */
export function sharedModeRequests(zones, mode) {
  if (!["heat", "cool", "off"].includes(mode)) return [];
  return zones.filter((z) => !z.heat_only).map((z) => buildRequest(z, "mode", mode)).filter(Boolean);
}

export function glanceRow(hass, z, api, vac, tz) {
  const st = hass?.states?.[z.entity], a = st?.attributes || {};
  const s = statusOf(st, z, vac);
  const now = num(hass?.states?.[z.indoor]?.state) ?? num(a.current_temperature);
  const zr = api?.last?.zones?.[z.zone], za = (api?.zones || []).find((x) => x.name === z.zone);
  const hold = za?.override_until && za.override_until * 1000 > Date.now() ? za.override_until : null;
  let doing = s.status;
  if (vac) doing = z.heat_only ? "Vacation · off" : "Vacation";
  else if (hold) doing = `Hold · auto at ${fmtClock(hold, tz)}`;
  else if (zr?.preheat) doing = "Warming up";
  else if (zr?.profile === "away") doing = "Away";
  else if (zr?.profile === "sleep") doing = s.off ? "Off" : "Sleep";
  return { label: z.label || a.friendly_name || z.entity, now, target: s.off ? null : num(a.temperature),
    mode: s.mode, off: s.off, doing, hold: !!hold, heatOnly: !!z.heat_only };
}

/** One-tap preset -> add-on API request (global profile; the add-on maps Eco to automatic). */
export function buildPreset(preset) {
  return PRESETS.some(([k]) => k === preset) ? { path: "api/preset", body: { preset } } : null;
}

/** Writes are possible only for admins (the add-on panel is admin-only; Supervisor refuses others). */
export function canWrite(hass) { return !!(hass && hass.user && hass.user.is_admin); }

export function ingressCookie(session, https) {
  return `ingress_session=${session};path=/api/hassio_ingress/;SameSite=Strict${https ? ";Secure" : ""}`;
}

/** Sanctioned frontend mechanism: dedicated supervisor/ingress/* commands, falling back to the
 * admin-only supervisor/api tunnel on older cores. Returns the add-on's ingress base URL. */
export async function openIngress(hass, slug, doc) {
  let session, url;
  try { session = (await hass.callWS({ type: "supervisor/ingress/session", slug })).session; }
  catch (e) { session = (await hass.callWS({ type: "supervisor/api", endpoint: "/ingress/session", method: "post" })).session; }
  try { url = (await hass.callWS({ type: "supervisor/ingress/info", slug })).ingress_url; }
  catch (e) { url = (await hass.callWS({ type: "supervisor/api", endpoint: `/addons/${slug}/info`, method: "get" })).ingress_url; }
  if (!session || !url) throw new Error("no ingress");
  doc.cookie = ingressCookie(session, doc.location?.protocol === "https:");
  return url.endsWith("/") ? url : url + "/";
}

/** POST one card write to the add-on through ingress. Re-opens the session once on 401. */
export async function postWrite(state, hass, req, deps) {
  if (!canWrite(hass)) throw new Error("read-only");
  for (let attempt = 0; attempt < 2; attempt++) {
    if (!state.base) state.base = await openIngress(hass, state.slug, deps.doc);
    const r = await deps.fetch(state.base + req.path, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(req.body) });
    if (r.status === 401 && attempt === 0) { state.base = null; continue; }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || String(r.status));
    return j;
  }
  throw new Error("ingress session refused");
}

/** GET the add-on state through ingress (admins only). */
export async function getState(state, hass, deps) {
  if (!canWrite(hass)) return null;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (!state.base) state.base = await openIngress(hass, state.slug, deps.doc);
    const r = await deps.fetch(state.base + "api/state", { credentials: "same-origin" });
    if (r.status === 401 && attempt === 0) { state.base = null; continue; }
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }
  return null;
}

const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : null; };
const tsOf = (r) => (r.lu ?? r.lc ?? (Date.parse(r.last_updated || r.last_changed) / 1000));

/** Recorder history (compressed or full rows) -> graph series, timestamps in epoch seconds. */
export function buildSeries(hist, zone, weather, start, end) {
  const out = { indoor: [], setpoint: [], heating: [], defrost: [], outdoor: [], start, end };
  const clamp = (t) => Math.max(t, start);
  for (const r of hist[zone.entity] || []) {
    const t = clamp(tsOf(r)), st = r.s ?? r.state, a = r.a || r.attributes || {};
    const off = st === "off" || st == null;
    out.heating.push([t, !off && (a.hvac_action ? a.hvac_action === "heating" : st === "heat") ? 1 : 0]);
    out.setpoint.push([t, off ? null : num(a.temperature)]);
    out.defrost.push([t, !off && DEFROST[a.hvac_action] ? 1 : 0]);
    if (!zone.indoor && num(a.current_temperature) != null) out.indoor.push([t, num(a.current_temperature)]);
  }
  for (const r of (zone.indoor && hist[zone.indoor]) || []) {
    const v = num(r.s ?? r.state); if (v != null) out.indoor.push([clamp(tsOf(r)), v]);
  }
  for (const r of (weather && hist[weather]) || []) {
    const v = num((r.a || r.attributes || {}).temperature); if (v != null) out.outdoor.push([clamp(tsOf(r)), v]);
  }
  for (const k of ["indoor", "setpoint", "heating", "defrost", "outdoor"]) out[k].sort((x, y) => x[0] - y[0]);
  return out;
}

/** Status line: active mode and why (vacation / off / heating ...). */
export function statusOf(stateObj, zone, vacation) {
  const mode = stateObj ? stateObj.state : "unavailable";
  const act = stateObj?.attributes?.hvac_action;
  const off = mode === "off";
  const status = !stateObj || mode === "unavailable" ? "Unavailable" : off ? "Off"
    : DEFROST[act] ? DEFROST[act] : act === "heating" ? "Heating" : act === "cooling" ? "Cooling" : act === "fan" ? "Fan" : "Idle";
  let why = "";
  if (vacation) why = zone.heat_only ? "Vacation — radiant floor stays off" : "Vacation — set back";
  else if (off) why = "Off";
  else if (DEFROST[act]) why = `${DEFROST[act]} — not a manual change; the app waits`;
  return { mode, off, status, why };
}

export function fmtTime(ts, tz) {
  return new Date(ts * 1000).toLocaleTimeString("en-US", { hour: "numeric", timeZone: tz || undefined });
}

export function fmtClock(ts, tz) {
  return new Date(ts * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: tz || undefined });
}

const REASON = { manual_override: "Manual hold", vacation_setback: "Vacation setback", vacation_at_setback: "Vacation setback",
  vacation_radiant_off: "Vacation — radiant off", window_open: "Window open — off", unit_off: "Off at the unit — app won't turn it on",
  overtemp_shutoff: "Bath over air shutoff — off", short_cycle_guard: "Waiting (anti short-cycle)",
  defrosting: "Defrosting — app waits", preheating: "Pre-heating — app waits", disabled: "App off" };

/** "Next change / why" line from the add-on zone result, e.g. "71° at 7:00 AM · pre-heat 6:15 AM". */
export function nextLine(zr, tz, holdUntil) {
  if (!zr) return "";
  if (holdUntil && zr.reason === "manual_override") return `Manual hold until ${fmtClock(holdUntil, tz)}`;
  const n = zr.next;
  if (n && n.setpoint != null) {
    const prof = { home: "home", sleep: "sleep", away: "away", guest: "guest" }[n.profile] || "";
    if (n.preheat && n.wake_ts) return `Next: ${n.setpoint}° at ${fmtClock(n.wake_ts, tz)} · pre-heat ${fmtClock(n.ts, tz)}`;
    return `Next: ${n.setpoint}° at ${fmtClock(n.ts, tz)}${prof ? " (" + prof + ")" : ""}`;
  }
  return REASON[zr.reason] || "";
}

export const EVENT_COLORS = { hold: "#d9902b", window: "#3fa7e0", vacation: "#b07ad9", defrost: "#7fd6ff", preheat: "#e0637a", defrost_stuck: "#ff3b3b" };
const EVENT_LABEL = { hold: "Hold", window: "Window", vacation: "Vacation", defrost: "Defrost", preheat: "Pre-heat", defrost_stuck: "Defrost stuck > 30 min" };

/** [ts, kind, start|end] events -> [{kind, t1, t2}] spans clipped to the window (open spans run to end). */
export function eventSpans(events, start, end) {
  const open = {}, out = [];
  for (const [t, k, what] of [...(events || [])].sort((a, b) => a[0] - b[0])) {
    if (!EVENT_COLORS[k]) continue;
    if (what === "start") { if (open[k] == null) open[k] = t; }
    else { out.push({ kind: k, t1: open[k] ?? start, t2: t }); delete open[k]; }
  }
  for (const k of Object.keys(open)) out.push({ kind: k, t1: open[k], t2: end });
  return out.filter((s) => s.t2 > start && s.t1 < end)
    .map((s) => ({ ...s, t1: Math.max(s.t1, start), t2: Math.min(s.t2, end) }));
}

/** Event strip SVG: one thin lane, coloured spans, titles for hover. */
export function eventStrip(events, start, end, w = 600, h = 12) {
  const X = (t) => ((t - start) / Math.max(1, end - start)) * w;
  const spans = eventSpans(events, start, end);
  const r = spans.map((s) => `<rect x="${X(s.t1).toFixed(1)}" y="1" width="${Math.max(2, X(s.t2) - X(s.t1)).toFixed(1)}" height="${h - 2}" rx="2" fill="${EVENT_COLORS[s.kind]}"><title>${EVENT_LABEL[s.kind]}</title></rect>`).join("");
  return `<svg class="ev" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><rect width="${w}" height="${h}" rx="3" fill="#0f161d"/>${r}</svg>`;
}

/** SVG graph markup (pure; testable). */
export function graphSvg(s, tz, w = 600, h = 140) {
  const pad = { l: 30, r: 6, t: 6, b: 18 }, W = w - pad.l - pad.r, Hh = h - pad.t - pad.b;
  const vals = [...s.indoor, ...s.setpoint, ...s.outdoor].map((p) => p[1]).filter((v) => v != null);
  if (!vals.length) return `<svg viewBox="0 0 ${w} ${h}"><text x="${w / 2}" y="${h / 2}" fill="#6f8296" text-anchor="middle" font-size="12">No history yet</text></svg>`;
  let lo = Math.floor(Math.min(...vals) - 2), hi = Math.ceil(Math.max(...vals) + 2);
  const X = (t) => pad.l + ((t - s.start) / Math.max(1, s.end - s.start)) * W;
  const Y = (v) => pad.t + (1 - (v - lo) / Math.max(1, hi - lo)) * Hh;
  const step = (pts) => { let d = "", prev = null;
    for (const [t, v] of [...pts, pts.length ? [s.end, pts[pts.length - 1][1]] : null].filter(Boolean)) {
      if (v == null) { prev = null; continue; }
      d += prev == null ? `M${X(t).toFixed(1)},${Y(v).toFixed(1)}` : `H${X(t).toFixed(1)}V${Y(v).toFixed(1)}`; prev = v;
    } return d; };
  const line = (pts) => [...pts, pts.length ? [s.end, pts[pts.length - 1][1]] : null].filter(Boolean)
    .map(([t, v], i) => `${i ? "L" : "M"}${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  let bands = "";
  s.heating.forEach(([t, on], i) => { if (!on) return; const t2 = (s.heating[i + 1] || [s.end])[0];
    bands += `<rect x="${X(t).toFixed(1)}" y="${pad.t}" width="${Math.max(1, X(t2) - X(t)).toFixed(1)}" height="${Hh}" fill="rgba(201,137,43,.28)"/>`; });
  (s.defrost || []).forEach(([t, on], i) => { if (!on) return; const t2 = (s.defrost[i + 1] || [s.end])[0];
    bands += `<rect class="dfr" x="${X(t).toFixed(1)}" y="${pad.t}" width="${Math.max(2, X(t2) - X(t)).toFixed(1)}" height="${Hh}" fill="rgba(127,214,255,.35)"/><text x="${(X(t) + 2).toFixed(1)}" y="${pad.t + 10}" fill="#7fd6ff" font-size="9">Defrosting</text>`; });
  let grid = "";
  for (const v of [lo, Math.round((lo + hi) / 2), hi]) grid += `<line x1="${pad.l}" x2="${w - pad.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="#1f2a36"/><text x="2" y="${Y(v) + 4}" fill="#6f8296" font-size="10">${v}°</text>`;
  const ticks = 4; let xt = "";
  for (let i = 0; i <= ticks; i++) { const t = s.start + ((s.end - s.start) * i) / ticks;
    xt += `<text x="${X(t)}" y="${h - 4}" fill="#6f8296" font-size="10" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${fmtTime(t, tz)}</text>`; }
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${grid}${bands}
    <path d="${line(s.outdoor)}" stroke="#6f8296" stroke-dasharray="4 3" fill="none" stroke-width="1.2"/>
    <path d="${step(s.setpoint)}" stroke="#2fb3a0" fill="none" stroke-width="1.6"/>
    <path d="${line(s.indoor)}" stroke="#e8eef4" fill="none" stroke-width="1.8"/>${xt}</svg>`;
}

const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";");

const CSS = `
:host{display:block;font-family:var(--paper-font-body1_-_font-family,system-ui,sans-serif)}
.wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;font-family:var(--paper-font-body1_-_font-family,system-ui,sans-serif);color:#e8eef4}
.card{background:#16202b;border-radius:16px;padding:12px 14px;box-shadow:0 2px 10px rgba(0,0,0,.35)}
.zh{display:flex;justify-content:space-between;align-items:baseline}.zh h2{margin:0;font-size:17px;font-weight:500}.dim{color:#8a9aab;font-size:12px}
.face{margin:10px auto 8px;border-radius:44px;background:radial-gradient(120% 90% at 50% 0%,#1f2933,#050708 70%);aspect-ratio:1.25;max-height:230px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px}
.face.off .big{color:#5d6b78}
.status{font-size:13px;color:#b9c5d0}.big{font-size:72px;font-weight:200;line-height:1}
.sp{display:flex;gap:12px;align-items:center}.step{width:36px;height:36px;border-radius:50%;border:1px solid #3a4652;background:#0b0f13;color:#e8eef4;font-size:20px;cursor:pointer}
.step:disabled{opacity:.35}.pill{min-width:52px;padding:5px 12px;border-radius:18px;border:1.5px solid #3a4652;text-align:center;font-size:17px}
.pill.heat{border-color:#d9902b}.pill.cool{border-color:#3fa7e0}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}.chip{font-size:12px;padding:3px 9px;border-radius:12px;border:1px solid #2c3a47;color:#b9c5d0}
.chip.vac{border-color:#d9902b;color:#f0c27a}.why{font-size:13px;color:#b9c5d0;min-height:18px}
.row{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.btn{font-size:12px;padding:5px 10px;border-radius:10px;border:1px solid #2c3a47;background:#0f161d;color:#e8eef4;cursor:pointer}
.btn.sel{border-color:#2fb3a0;color:#7fe0d0}.btn:disabled{opacity:.35}
.g{margin-top:8px}.g svg{width:100%;height:120px;display:block}
.leg{display:flex;gap:10px;font-size:11px;color:#8a9aab;margin-top:2px}.leg i{display:inline-block;width:12px;height:3px;margin-right:4px;vertical-align:middle}
.pre{display:flex;gap:8px;align-items:center;margin:0 0 10px}.pre .btn{font-size:14px;padding:7px 16px}.pre .dim{margin-left:auto}
.next{font-size:12px;color:#7fe0d0;min-height:16px}.ev{width:100%;height:10px;display:block;margin-top:4px}.chip.dfr{border-color:#7fd6ff;color:#7fd6ff}
.ro{margin:0 0 10px;padding:8px 12px;border-radius:10px;background:#1d2733;color:#f0c27a;font-size:13px}.ro a{color:#7fe0d0}
@media (max-width:1300px) and (max-height:820px){.big{font-size:56px}.face{max-height:170px;margin:6px auto 4px}.g svg{height:84px}.chips{margin:4px 0}.card{padding:10px 12px}.pre{margin:0 0 8px}}
`;

const GCSS = `.gl{color:#e6edf3;display:flex;flex-direction:column;gap:6px}.gr{display:grid;grid-template-columns:1fr auto auto auto;align-items:center;gap:10px;padding:10px 12px;border-radius:14px;background:#141a21}
.gn{font-size:15px}.gd{font-size:12px;color:#9fb0c0}.gd.h{color:#d9902b}.gt{font-size:34px;font-weight:300;min-width:56px;text-align:right}.gs{display:flex;align-items:center;gap:6px}
.gp{min-width:44px;text-align:center;padding:4px 8px;border-radius:999px;border:1px solid #2fb3a0;color:#2fb3a0}.gp.heat{border-color:#e08a3c;color:#e08a3c}.gp.cool{border-color:#4aa3e0;color:#4aa3e0}
.gh{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 8px}.gh .sep{flex:1}.gh .btn{min-height:48px;min-width:48px}
`;

const Base = typeof HTMLElement !== "undefined" ? HTMLElement : class {};
class SmartHvacCard extends Base {
  setConfig(config) {
    const zones = (config && config.zones) || DEFAULT_ZONES;
    for (const z of zones) {
      if (!z.entity || String(z.entity).startsWith("climate.p3arl2_")) throw new Error("smart-hvac-card: zone entity not allowed");
      if (!/^[a-z0-9_]+$/.test(String(z.zone || ""))) throw new Error("smart-hvac-card: each zone needs the add-on zone name");
    }
    this._cfg = { weather: "", hours: 24, vacation_entity: VACATION_ENTITY, slug: ADDON_SLUG, ...config, zones };
    this._ing = { slug: this._cfg.slug, base: null }; this._msg = "";
    this._pend = {}; this._hist = {}; this._histAt = 0; this._api = null; this._apiAt = 0;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }
  set hass(h) { this._hass = h; this._render(); this._maybeHistory(); this._maybeApi(); }

  async _maybeApi(force) {
    const now = Date.now() / 1000;
    if (!canWrite(this._hass) || (!force && now - this._apiAt < 60)) return;
    this._apiAt = now;
    try { this._api = await getState(this._ing, this._hass, { fetch: (...a) => fetch(...a), doc: document }); this._render(); }
    catch (e) { this._apiAt = now - 45; }
  }

  async _preset(p) {
    const req = buildPreset(p);
    if (!req || !canWrite(this._hass)) return;
    try { this._api = await postWrite(this._ing, this._hass, req, { fetch: (...a) => fetch(...a), doc: document }); this._msg = ""; }
    catch (e) { this._msg = "Can't change it now: " + e.message; }
    this._render();
  }
  getCardSize() { return this._cfg?.layout === "glance" ? 4 : 8; }
  static getStubConfig() { return {}; }

  _vac() { return this._hass?.states?.[this._cfg.vacation_entity]?.state === "on"; }

  async _maybeHistory() {
    const now = Date.now() / 1000;
    if (!this._hass?.callWS || now - this._histAt < 300 || this._cfg.layout === "glance") return;
    this._histAt = now;
    const start = now - this._cfg.hours * 3600;
    const ents = [...new Set(this._cfg.zones.flatMap((z) => [z.entity, z.indoor]).concat(this._cfg.weather).filter(Boolean))];
    try {
      const r = await this._hass.callWS({ type: "history/history_during_period", start_time: new Date(start * 1000).toISOString(),
        end_time: new Date(now * 1000).toISOString(), entity_ids: ents, minimal_response: false, no_attributes: false,
        significant_changes_only: false });
      for (const z of this._cfg.zones) this._hist[z.entity] = buildSeries(r || {}, z, this._cfg.weather, start, now);
      this._render();
    } catch (e) { this._histAt = 0; }
  }

  async _do(zone, action, value) {
    if (!canWrite(this._hass)) return;
    const req = buildRequest(zone, action, value, { vacation: this._vac(), cap: radiantCap(this._api) });
    if (!req) return;
    if (action === "setpoint") this._pend[zone.entity] = req.body.temperature;
    this._render();
    try {
      const j = await postWrite(this._ing, this._hass, req, { fetch: (...a) => fetch(...a), doc: document });
      if (j && j.zones) this._api = j;
      this._msg = "";
    } catch (e) { this._msg = "Can't change it now: " + e.message; }
    finally { setTimeout(() => { delete this._pend[zone.entity]; this._render(); }, 4000); this._render(); }
  }

  _bump(zone, d) {
    if (!canWrite(this._hass)) return;
    const st = this._hass.states[zone.entity];
    const cur = this._pend[zone.entity] ?? num(st?.attributes?.temperature) ?? 70;
    const t = clampSetpoint(cur + d, zone.heat_only, radiantCap(this._api));
    this._pend[zone.entity] = t; this._render();
    clearTimeout(this._t?.[zone.entity]); this._t = this._t || {};
    this._t[zone.entity] = setTimeout(() => this._do(zone, "setpoint", t), 800);
  }

  _card(z, i, vac, tz) {
    const h = this._hass, st = h.states[z.entity], a = st?.attributes || {};
    const s = statusOf(st, z, vac), ro = !canWrite(h);
    const indoor = num(h.states[z.indoor]?.state) ?? num(a.current_temperature);
    const hum = num(h.states[z.humidity]?.state) ?? num(a.current_humidity);
    const sp = this._pend[z.entity] ?? num(a.temperature);
    const outdoor = num(h.states[this._cfg.weather]?.attributes?.temperature);
    const modes = (z.heat_only ? ["heat", "off"] : ["heat", "cool", "off"]).filter((m) => !a.hvac_modes || a.hvac_modes.includes(m));
    const fans = z.heat_only ? [] : a.fan_modes || [];
    let chips = vac ? `<span class="chip vac">Vacation · ${z.heat_only ? "radiant off" : "set back"}</span>` : `<span class="chip">${esc(s.mode[0]?.toUpperCase() + s.mode.slice(1))}</span>`;
    if (outdoor != null) chips += `<span class="chip">Outside ${Math.round(outdoor)}°</span>`;
    if (DEFROST[a.hvac_action] && !s.off) chips += `<span class="chip dfr">${DEFROST[a.hvac_action]}</span>`;
    if (z.heat_only) chips += `<span class="chip">Max ${radiantCap(this._api)}°</span>`;
    const g = this._hist[z.entity];
    const api = this._api, zr = api?.last?.zones?.[z.zone], za = (api?.zones || []).find((x) => x.name === z.zone);
    const nl = nextLine(zr, tz, za?.override_until);
    const evs = za?.events || [];
    return `<div class="card"><div class="zh"><h2>${esc(z.label || a.friendly_name || z.entity)}</h2><span class="dim">${z.heat_only ? "Radiant floor" : "Mini split"}</span></div>
      <div class="face ${s.off ? "off" : ""}"><div class="status">${hum != null ? Math.round(hum) + "% · " : ""}${esc(s.status)}</div>
      <div class="big">${indoor != null ? Math.round(indoor) : "--"}</div>
      <div class="sp"><button class="step" data-i="${i}" data-d="-1" ${s.off || ro ? "disabled" : ""}>−</button>
      <div class="pill ${s.off ? "" : esc(s.mode)}">${s.off ? "Off" : sp != null ? sp : "--"}</div>
      <button class="step" data-i="${i}" data-d="1" ${s.off || ro ? "disabled" : ""}>+</button></div></div>
      <div class="chips">${chips}</div><div class="why">${esc(s.why)}</div><div class="next">${esc(nl)}</div>
      <div class="row">${modes.map((m) => `<button class="btn ${m === s.mode ? "sel" : ""}" data-i="${i}" data-mode="${m}" ${ro || (z.heat_only && vac && m !== "off") ? "disabled" : ""}>${modeLabel(m, z.heat_only)}</button>`).join("")}
      ${fans.map((f) => `<button class="btn ${f === a.fan_mode ? "sel" : ""}" data-i="${i}" data-fan="${esc(f)}" ${ro ? "disabled" : ""}>${esc(f)}</button>`).join("")}</div>
      <div class="g">${g ? graphSvg(g, tz) : '<div class="dim">Loading history…</div>'}</div>
      ${g ? eventStrip(evs, g.start, g.end) : ""}
      <div class="leg"><span><i style="background:#e8eef4"></i>Indoor</span><span><i style="background:#2fb3a0"></i>Setpoint</span><span><i style="background:rgba(201,137,43,.6);height:7px"></i>Heating</span><span><i style="background:#6f8296"></i>Outdoor</span><span><i style="background:rgba(127,214,255,.7);height:7px"></i>Defrost</span></div></div>`;
  }

  async _shared(mode) {
    if (!canWrite(this._hass)) return;
    try {
      for (const req of sharedModeRequests(this._cfg.zones, mode)) {
        const j = await postWrite(this._ing, this._hass, req, { fetch: (...a) => fetch(...a), doc: document });
        if (j && j.zones) this._api = j;
      }
      this._msg = "";
    } catch (e) { this._msg = "Can't change it now: " + e.message; }
    this._render();
  }

  _renderGlance(vac, tz) {
    const h = this._hass, ro = !canWrite(h), zs = this._cfg.zones;
    const shared = sharedSplitMode(h, zs);
    const out = num(h.states[this._cfg.weather]?.attributes?.temperature);
    const prof = this._api?.profile, sel = { sleep: "sleep", away: "away", auto: "eco" }[prof];
    const banner = ro ? `<div class="ro">View only — admins can change it.</div>` : this._msg ? `<div class="ro">${esc(this._msg)}</div>` : "";
    const head = `<div class="gh">${["heat", "cool", "off"].map((m) => `<button class="btn ${m === shared ? "sel" : ""}" data-shared="${m}" ${ro || vac ? "disabled" : ""}>${m[0].toUpperCase() + m.slice(1)}</button>`).join("")}
      <span class="sep"></span>${[["eco", "Auto"], ["sleep", "Sleep"], ["away", "Away"]].map(([k, l]) => `<button class="btn ${k === sel ? "sel" : ""}" data-preset="${k}" ${ro ? "disabled" : ""}>${l}</button>`).join("")}
      ${out != null ? `<span class="dim">Outside ${Math.round(out)}°</span>` : ""}</div>`;
    const rows = zs.map((z, i) => {
      const r = glanceRow(h, z, this._api, vac, tz), sp = this._pend[z.entity] ?? r.target;
      return `<div class="gr"><div><div class="gn">${esc(r.label)}</div><div class="gd ${r.hold ? "h" : ""}">${esc(r.doing)}</div></div>
        <div class="gt">${r.now != null ? Math.round(r.now) + "°" : "--"}</div>
        <div class="gs"><button class="step" data-i="${i}" data-d="-1" ${r.off || ro ? "disabled" : ""}>−</button><span class="gp ${r.off ? "" : esc(r.mode)}">${r.off ? "Off" : sp != null ? sp + "°" : "--"}</span><button class="step" data-i="${i}" data-d="1" ${r.off || ro ? "disabled" : ""}>+</button></div>
        <div>${r.hold && !ro ? `<button class="btn" data-i="${i}" data-resume="1">Auto</button>` : ""}</div></div>`;
    }).join("");
    return `<style>${CSS}${GCSS}</style>${banner}${head}<div class="gl">${rows}</div>`;
  }

  _render() {
    if (!this._hass || !this._cfg || !this.shadowRoot) return;
    if (this._cfg.layout === "glance") {
      this.shadowRoot.innerHTML = this._renderGlance(this._vac(), this._hass.config?.time_zone);
      this.shadowRoot.querySelectorAll("button[data-shared]").forEach((b) => b.addEventListener("click", () => this._shared(b.dataset.shared)));
      this.shadowRoot.querySelectorAll("button[data-preset]").forEach((b) => b.addEventListener("click", () => this._preset(b.dataset.preset)));
      this.shadowRoot.querySelectorAll("button[data-i]").forEach((b) => b.addEventListener("click", () => {
        const z = this._cfg.zones[+b.dataset.i];
        if (b.dataset.d) this._bump(z, +b.dataset.d);
        else if (b.dataset.resume) this._do(z, "resume");
      }));
      return;
    }
    const vac = this._vac(), tz = this._hass.config?.time_zone;
    const banner = canWrite(this._hass)
      ? (this._msg ? `<div class="ro">${esc(this._msg)}</div>` : "")
      : `<div class="ro">View only — changes are made in the <a href="/hassio/ingress/${esc(this._cfg.slug)}">Climate panel</a> (admin) so the app's safety rules apply.</div>`;
    const prof = this._api?.profile, ro = !canWrite(this._hass);
    const sel = { home: "home", sleep: "sleep", away: "away", auto: "eco" }[prof];
    const presets = `<div class="pre">${PRESETS.map(([k, l]) => `<button class="btn ${k === sel ? "sel" : ""}" data-preset="${k}" ${ro ? "disabled" : ""}>${l}</button>`).join("")}<span class="dim">${vac ? "Vacation overrides presets" : sel === "eco" ? "Eco: automatic by presence + schedule" : ""}</span></div>`;
    this.shadowRoot.innerHTML = `<style>${CSS}</style>${banner}${presets}<div class="wrap">${this._cfg.zones.map((z, i) => this._card(z, i, vac, tz)).join("")}</div>`;
    this.shadowRoot.querySelectorAll("button[data-preset]").forEach((b) => b.addEventListener("click", () => this._preset(b.dataset.preset)));
    this.shadowRoot.querySelectorAll("button[data-i]").forEach((b) => b.addEventListener("click", () => {
      const z = this._cfg.zones[+b.dataset.i];
      if (b.dataset.d) this._bump(z, +b.dataset.d);
      else if (b.dataset.mode) this._do(z, "mode", b.dataset.mode);
      else if (b.dataset.fan) this._do(z, "fan", b.dataset.fan);
    }));
  }
}

if (typeof customElements !== "undefined" && !customElements.get("smart-hvac-card")) {
  customElements.define("smart-hvac-card", SmartHvacCard);
  (window.customCards = window.customCards || []).push({ type: "smart-hvac-card", name: "Smart HVAC thermostats",
    description: "Ecobee-style thermostat cards with history (no iframe)." });
}
