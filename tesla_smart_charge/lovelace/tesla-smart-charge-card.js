/* Tesla Smart Charge Lovelace card (0.2.0). Reads ONLY the add-on's status sensor; its attributes carry the plan,
 * ETA, cost, mode, transport and the helper entity ids. Mode chips toggle those input_boolean helpers
 * (input_boolean.turn_on / turn_off only). No iframe, no ingress, no other service calls.
 *
 *   type: custom:tesla-smart-charge-card
 *   entity: sensor.tesla_smart_charge_status   # optional (default)
 *   title: Car                                  # optional
 */
export const DEFAULT_ENTITY = "sensor.tesla_smart_charge_status";
export const CHIPS = [["smart", "Smart"], ["charge_now", "Charge now"], ["paused", "Pause"], ["trip", "Trip"]];
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
  if (a.behind) alerts.push(["warn", "Behind plan: charging at max amps"]);
  if (a.plugged === false) alerts.push(["info", "Unplugged"]);
  if (a.reason === "stale_state") alerts.push(["warn", "Waiting for fresh car data"]);
  if (a.last_error) alerts.push(["bad", "Last error: " + a.last_error]);
  return {
    state: st.state, mode: a.mode || "smart",
    soc: Number.isFinite(soc) ? soc : null, target: Number.isFinite(target) ? target : null,
    charging: a.charging === true, amps: a.plan_amps ?? a.amps ?? null, kwh: a.kwh_needed,
    start: a.start_at, finishBy: a.finish_by, eta: a.eta_finish, cost: a.est_cost, currency: a.currency || "$",
    transport: a.transport || null, setup: a.setup_mode || "cloud-only", next: a.next_action || "",
    alerts, helpers: { charge_now: a.charge_now_entity, paused: a.pause_entity, trip: a.trip_entity },
  };
}

/** Service calls for tapping a chip. Smart = turn the other helpers off. Only input_boolean on/off. */
export function chipCalls(vm, chip) {
  const h = vm.helpers || {};
  const offs = (except) => Object.keys(HELPER).filter((k) => k !== except && h[k])
    .map((k) => ["input_boolean", "turn_off", { entity_id: h[k] }]);
  if (chip === "smart") return offs(null);
  if (!h[chip]) return [];
  if (vm.mode === chip) return [["input_boolean", "turn_off", { entity_id: h[chip] }]];
  return [...offs(chip), ["input_boolean", "turn_on", { entity_id: h[chip] }]];
}

export function ring(soc, target, charging) {
  const r = 52, c = 2 * Math.PI * r, p = Math.max(0, Math.min(100, soc ?? 0)) / 100;
  const ta = ((target ?? 0) / 100) * 2 * Math.PI - Math.PI / 2;
  const tx = 60 + Math.cos(ta) * r, ty = 60 + Math.sin(ta) * r;
  return `<svg viewBox="0 0 120 120" class="ring" role="img" aria-label="Battery ${esc(soc)}% of ${esc(target)}%">
    <circle cx="60" cy="60" r="${r}" class="track"/>
    <circle cx="60" cy="60" r="${r}" class="fill${charging ? " live" : ""}" stroke-dasharray="${(c * p).toFixed(1)} ${c.toFixed(1)}"
      transform="rotate(-90 60 60)"/>
    ${target != null ? `<circle cx="${tx.toFixed(1)}" cy="${ty.toFixed(1)}" r="5" class="tick"/>` : ""}
    <text x="60" y="58" class="pct">${soc == null ? "—" : Math.round(soc) + "%"}</text>
    <text x="60" y="78" class="tgt">${target == null ? "" : "target " + Math.round(target) + "%"}</text></svg>`;
}

export const CSS = `
:host{--bg:#e8eef4;--panel:#f4f7fa;--text:#1e2a36;--dim:#5a6b7c;--line:#c5d0dc;--accent:#2a9d8f;--soft:#d5f0ec;
 --good:#2f9e6b;--bad:#c94c4c;--warn:#c9892b}
ha-card{display:block;background:var(--panel);color:var(--text);border-radius:14px;padding:16px;font-family:"Segoe UI",system-ui,sans-serif;
 box-shadow:0 2px 10px rgba(30,42,54,.08)}
.hd{display:flex;align-items:center;justify-content:space-between;gap:8px}.title{font-size:22px;font-weight:600}
.badge{font-size:13px;padding:3px 10px;border-radius:999px;background:#eef3f8;border:1px solid var(--line);color:var(--dim)}
.badge.ble{background:var(--soft);color:#21867a;border-color:var(--accent)}
.main{display:grid;grid-template-columns:150px 1fr;gap:16px;align-items:center;margin-top:8px}
.ring{width:150px;height:150px}.track{fill:none;stroke:var(--line);stroke-width:12}
.fill{fill:none;stroke:var(--accent);stroke-width:12;stroke-linecap:round}.fill.live{stroke:var(--good)}
.tick{fill:var(--text)}.pct{font-size:28px;font-weight:600;text-anchor:middle;fill:var(--text)}
.tgt{font-size:12px;text-anchor:middle;fill:var(--dim)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 16px}
.k{font-size:13px;color:var(--dim)}.v{font-size:18px;font-weight:600;white-space:nowrap}
.next{margin-top:10px;font-size:16px}.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chip{min-height:44px;padding:0 18px;border-radius:10px;border:1px solid var(--line);background:#fff;color:var(--text);
 font-size:16px;cursor:pointer}.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.chip:disabled{opacity:.45;cursor:default}
.al{margin-top:10px;padding:8px 12px;border-radius:10px;font-size:14px;background:#eef3f8;border-left:4px solid var(--dim)}
.al.warn{border-color:var(--warn)}.al.bad{border-color:var(--bad)}.al.info{border-color:var(--accent)}
@media (max-width:420px){.main{grid-template-columns:1fr;justify-items:center}}`;

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
    <div class="main">${ring(vm.soc, vm.target, vm.charging)}<div class="grid">
      <div><div class="k">Plan</div><div class="v">${vm.amps ? esc(vm.amps) + " A" : "—"}</div></div>
      <div><div class="k">Needed</div><div class="v">${vm.kwh != null ? esc(Number(vm.kwh).toFixed(1)) + " kWh" : "—"}</div></div>
      <div><div class="k">Starts</div><div class="v">${esc(fmtTime(vm.start, now))}</div></div>
      <div><div class="k">Done by</div><div class="v">${esc(fmtTime(vm.eta, now))}</div></div>
      <div><div class="k">Deadline</div><div class="v">${esc(fmtTime(vm.finishBy, now))}</div></div>
      <div><div class="k">Est. cost</div><div class="v">${fmtCost(vm.cost, vm.currency)}</div></div>
    </div></div>
    <div class="next">${vm.charging ? "⚡ " : ""}${esc(vm.next)}</div>
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
    getCardSize() { return 5; }
    static getStubConfig() { return { entity: DEFAULT_ENTITY }; }
  }
  if (!customElements.get("tesla-smart-charge-card")) customElements.define("tesla-smart-charge-card", TeslaSmartChargeCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "tesla-smart-charge-card", name: "Tesla Smart Charge",
    description: "Plan, ETA, cost and mode chips for the Tesla Smart Charge add-on" });
}
