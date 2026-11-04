/* Tesla Smart Charge Lovelace card (0.3.0). Styled after the Tesla in-car (Model S Plaid) charging screen:
 * dark panel, big light numerals, horizontal battery bar with a limit marker, green while charging. Reads ONLY the add-on's status sensor; its attributes carry the plan,
 * ETA, cost, mode, transport and the helper entity ids. Mode chips toggle those input_boolean helpers
 * (input_boolean.turn_on / turn_off only). No iframe, no ingress, no other service calls.
 *
 *   type: custom:tesla-smart-charge-card
 *   entity: sensor.tesla_smart_charge_status   # optional (default)
 *   title: Car                                  # optional
 */
export const DEFAULT_ENTITY = "sensor.tesla_smart_charge_status";
export const CHIPS = [["smart", "Smart"], ["charge_now", "Charge now"], ["paused", "Pause"], ["trip", "Trip"]];
export const HELPER_RE = /^input_boolean\.[a-z0-9_]+$/;
const okHelper = (v) => (typeof v === "string" && HELPER_RE.test(v) ? v : null);
const HELPER = { charge_now: "charge_now_entity", paused: "pause_entity", trip: "trip_entity" };

export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export function fmtTime(iso, now = new Date()) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const days = Math.round((new Date(d.toDateString()) - new Date(now.toDateString())) / 864e5);
  return days === 0 ? t : days === 1 ? "Tmrw " + t : d.toLocaleDateString([], { weekday: "short" }) + " " + t;
}

export function fmtCost(v, cur = "$") {
  const n = Number(v);
  return v === null || v === undefined || !Number.isFinite(n) ? "—" : esc(cur) + n.toFixed(2);
}

/** Pure view model from the status entity state object. */
export function viewModel(st) {
  if (!st) return { missing: true };
  const a = st.attributes || {};
  const soc = Number(a.soc), target = Number(a.target);
  const alerts = [];
  if (st.state === "not_configured") alerts.push(["warn", "Add-on not configured yet"]);
  if (a.dry_run) alerts.push(["info", "Dry run: planning only, no charge commands"]);
  if (a.behind && a.reason !== "past_finish_time") alerts.push(["warn", "Behind plan: charging at max amps"]);
  if (a.plugged === false) alerts.push(["info", "Unplugged"]);
  if (a.reason === "stale_state") alerts.push(["warn", "Waiting for fresh car data"]);
  if (a.fault === "wall_connector_fault") alerts.push(["bad", "Wall Connector fault: charging stopped"]);
  if (a.fault === "handle_too_hot") alerts.push(["bad", "Handle too hot" + (a.handle_temp_f != null ?
    " (" + Math.round(a.handle_temp_f) + "°F)" : "") + ": charging stopped. Inspect the handle and cable."]);
  if (a.derated) alerts.push(["warn", "Wall Connector is limiting current: not raising amps"]);
  if (a.reason === "past_finish_time") alerts.push(["info", "Deadline passed: no more adjustments"]);
  if (a.last_error) alerts.push(["bad", "Last error: " + a.last_error]);
  return {
    state: st.state, mode: a.mode || "smart",
    soc: Number.isFinite(soc) ? soc : null, target: Number.isFinite(target) ? target : null,
    charging: a.charging === true, amps: a.plan_amps ?? a.amps ?? null, kwh: a.kwh_needed,
    start: a.start_at, finishBy: a.finish_by, eta: a.eta_finish, cost: a.est_cost, currency: a.currency || "$",
    transport: a.transport || null, setup: a.setup_mode || "cloud-only", next: a.next_action || "",
    alerts, helpers: { charge_now: okHelper(a.charge_now_entity), paused: okHelper(a.pause_entity), trip: okHelper(a.trip_entity) },
  };
}

/** Service calls for tapping a chip. Smart = turn the other helpers off. Only input_boolean on/off. */
export function chipCalls(vm, chip) {
  const raw = vm.helpers || {};
  const h = Object.fromEntries(Object.keys(HELPER).map((k) => [k, okHelper(raw[k])]));
  const offs = (except) => Object.keys(HELPER).filter((k) => k !== except && h[k])
    .map((k) => ["input_boolean", "turn_off", { entity_id: h[k] }]);
  if (chip === "smart") return offs(null);
  if (!h[chip]) return [];
  if (vm.mode === chip) return [["input_boolean", "turn_off", { entity_id: h[chip] }]];
  return [...offs(chip), ["input_boolean", "turn_on", { entity_id: h[chip] }]];
}

/** Horizontal battery bar like the in-car charging screen: fill = SoC, white tick = charge limit/target. */
export function battery(soc, target, charging) {
  const p = Math.max(0, Math.min(100, soc ?? 0)), t = target == null ? null : Math.max(0, Math.min(100, target));
  return `<div class="bat" role="img" aria-label="Battery ${esc(soc)}% of ${esc(target)}%">
    <div class="fill${charging ? " live" : ""}" style="width:${p.toFixed(1)}%"></div>
    ${t != null ? `<div class="lim" style="left:${t.toFixed(1)}%"></div>` : ""}</div>`;
}

export const CSS = `
:host{--bg:#0b0c0e;--panel:#16181b;--text:#f5f6f7;--dim:#8d9399;--line:#2a2e33;--green:#3ddc84;--blue:#3e6ae1;
 --red:#e82127;--amber:#f2a33a}
ha-card{display:block;background:var(--panel);color:var(--text);border-radius:16px;padding:20px;border:1px solid var(--line);
 font-family:"Gotham SSm","Gotham","Helvetica Neue",-apple-system,system-ui,sans-serif;letter-spacing:.01em}
.hd{display:flex;align-items:center;justify-content:space-between;gap:8px}
.title{font-size:15px;font-weight:500;color:var(--dim);text-transform:uppercase;letter-spacing:.12em}
.badge{font-size:12px;padding:3px 10px;border-radius:999px;background:#1f2226;color:var(--dim)}
.badge.ble{color:var(--blue)}
.big{display:flex;align-items:baseline;gap:10px;margin-top:10px}
.pct{font-size:56px;font-weight:300;line-height:1}.pct small{font-size:24px;color:var(--dim);margin-left:2px}
.status{font-size:16px;color:var(--dim)}.status.live{color:var(--green)}
.bat{position:relative;height:14px;border-radius:7px;background:#25282c;margin:14px 0 4px;overflow:visible}
.bat .fill{height:100%;border-radius:7px;background:#c9ccd0}.bat .fill.live{background:var(--green)}
.bat .lim{position:absolute;top:-4px;width:2px;height:22px;background:var(--text);border-radius:1px}
.tgt{font-size:12px;color:var(--dim);text-align:right}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px 12px;margin-top:16px}
.k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em}.v{font-size:20px;font-weight:400;white-space:nowrap}
.chips{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:18px}
.chip{min-height:44px;border-radius:10px;border:0;background:#24272b;color:var(--text);font:inherit;font-size:14px;cursor:pointer}
.chip.on{background:var(--blue);color:#fff}.chip:disabled{opacity:.35;cursor:default}
.al{margin-top:10px;padding:10px 12px;border-radius:10px;font-size:13px;background:#1f2226;border-left:3px solid var(--dim)}
.al.warn{border-color:var(--amber)}.al.bad{border-color:var(--red)}.al.info{border-color:var(--blue)}
@media (max-width:420px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.chips{grid-template-columns:repeat(2,1fr)}}`;

export function render(vm, title = "Tesla Smart Charge", now = new Date()) {
  if (vm.missing) return `<ha-card><div class="title">${esc(title)}</div><div class="al bad">Status sensor not found.
    Start the Tesla Smart Charge add-on.</div></ha-card>`;
  const t = vm.transport === "ble" ? "Bluetooth" : vm.transport && vm.transport.startsWith("cloud") ? "Cloud" : null;
  const chips = CHIPS.map(([k, label]) => {
    const can = k === "smart" || vm.helpers[k];
    return `<button class="chip${vm.mode === k ? " on" : ""}" data-chip="${k}" ${can ? "" : "disabled"}>${label}</button>`;
  }).join("");
  return `<ha-card><div class="hd"><div class="title">${esc(title)}</div>
    <span class="badge${t === "Bluetooth" ? " ble" : ""}">${esc(t ? "via " + t : vm.setup)}</span></div>
    <div class="big"><div class="pct">${vm.soc == null ? "—" : Math.round(vm.soc)}<small>%</small></div>
      <div class="status${vm.charging ? " live" : ""}">${vm.charging ? "Charging · " : ""}${esc(vm.next)}</div></div>
    ${battery(vm.soc, vm.target, vm.charging)}
    <div class="tgt">${vm.target == null ? "" : "Limit " + esc(Math.round(vm.target)) + "%"}</div>
    <div class="grid">
      <div><div class="k">Current</div><div class="v">${vm.amps ? esc(vm.amps) + " A" : "—"}</div></div>
      <div><div class="k">Needed</div><div class="v">${vm.kwh != null ? esc(Number(vm.kwh).toFixed(1)) + " kWh" : "—"}</div></div>
      <div><div class="k">Est. cost</div><div class="v">${fmtCost(vm.cost, vm.currency)}</div></div>
      <div><div class="k">Starts</div><div class="v">${esc(fmtTime(vm.start, now))}</div></div>
      <div><div class="k">Done</div><div class="v">${esc(fmtTime(vm.eta, now))}</div></div>
      <div><div class="k">Deadline</div><div class="v">${esc(fmtTime(vm.finishBy, now))}</div></div>
    </div>
    <div class="chips">${chips}</div>
    ${vm.alerts.map(([lvl, m]) => `<div class="al ${lvl}">${esc(m)}</div>`).join("")}</ha-card>`;
}

if (typeof HTMLElement !== "undefined" && typeof customElements !== "undefined") {
  class TeslaSmartChargeCard extends HTMLElement {
    setConfig(c) { this._c = { entity: DEFAULT_ENTITY, ...(c || {}) }; if (!this.shadowRoot) this.attachShadow({ mode: "open" }); }
    set hass(h) {
      this._h = h;
      const st = h.states[this._c.entity];
      const key = st ? st.last_updated + st.state : "none";
      if (key === this._k) return;
      this._k = key;
      this._vm = viewModel(st);
      this.shadowRoot.innerHTML = `<style>${CSS}</style>` + render(this._vm, this._c.title);
      this.shadowRoot.querySelectorAll("[data-chip]").forEach((b) => b.addEventListener("click", () => {
        for (const [d, s, data] of chipCalls(this._vm, b.dataset.chip)) this._h.callService(d, s, data);
      }));
    }
    getCardSize() { return 6; }
    static getStubConfig() { return { entity: DEFAULT_ENTITY }; }
  }
  if (!customElements.get("tesla-smart-charge-card")) customElements.define("tesla-smart-charge-card", TeslaSmartChargeCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "tesla-smart-charge-card", name: "Tesla Smart Charge",
    description: "Plan, ETA, cost and mode chips for the Tesla Smart Charge add-on" });
}
