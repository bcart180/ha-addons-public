import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { buildRequest, clampSetpoint, buildSeries, statusOf, graphSvg, fmtTime, canWrite, ingressCookie,
  openIngress, postWrite, DEFAULT_ZONES, ADDON_SLUG, radiantCap, buildPreset, getState, nextLine, eventSpans,
  eventStrip, PRESETS } from "../smart-hvac-card.js";

const [LR, , BATH] = DEFAULT_ZONES;
const SRC = fs.readFileSync(new URL("../smart-hvac-card.js", import.meta.url), "utf8");

function fakeHass({ admin = true, dedicated = true, info = "/api/hassio_ingress/TOK/" } = {}) {
  const ws = [];
  return { ws, user: { is_admin: admin }, async callWS(m) {
    ws.push(m);
    if (m.type === "supervisor/ingress/session") { if (!dedicated) throw new Error("unknown command"); return { session: "S" + ws.length }; }
    if (m.type === "supervisor/ingress/info") { if (!dedicated) throw new Error("unknown command"); return { ingress_url: info }; }
    if (m.type === "supervisor/api" && m.endpoint === "/ingress/session") return { session: "OLD" };
    if (m.type === "supervisor/api") return { ingress_url: info };
    throw new Error("unexpected " + m.type);
  } };
}
const fakeFetch = (statuses) => { const calls = []; const f = async (url, o) => { calls.push({ url, ...o });
  const st = statuses.shift() ?? 200; return { status: st, ok: st < 400, json: async () => ({ ok: st < 400, error: st >= 400 ? "nope" : undefined }) }; };
  f.calls = calls; return f; };

test("M1: the card never calls HA services directly", () => {
  assert.doesNotMatch(SRC, /callService/);
  assert.doesNotMatch(SRC, /<iframe/);
});

test("routing: requests target add-on endpoints by zone name only", () => {
  assert.deepEqual(buildRequest(LR, "setpoint", 72), { path: "api/zone/living_room/setpoint", body: { temperature: 72 } });
  assert.deepEqual(buildRequest(LR, "fan", "low"), { path: "api/zone/living_room/fan", body: { fan_mode: "low" } });
  assert.equal(buildRequest(BATH, "setpoint", 95).body.temperature, 85);
  assert.equal(buildRequest(BATH, "mode", "heat", { vacation: true }), null);
  assert.equal(buildRequest(BATH, "fan", "low"), null);
  assert.equal(buildRequest({ zone: "../x" }, "setpoint", 70), null);
  assert.equal(clampSetpoint(40, false), 55);
});

test("admin: write goes POST through ingress with a session cookie (dedicated WS commands)", async () => {
  const hass = fakeHass(), f = fakeFetch([200]), doc = { cookie: "", location: { protocol: "https:" } };
  const st = { slug: ADDON_SLUG, base: null };
  await postWrite(st, hass, buildRequest(LR, "setpoint", 72), { fetch: f, doc });
  assert.equal(f.calls[0].url, "/api/hassio_ingress/TOK/api/zone/living_room/setpoint");
  assert.equal(f.calls[0].method, "POST");
  assert.equal(JSON.parse(f.calls[0].body).temperature, 72);
  assert.match(doc.cookie, /^ingress_session=S1;path=\/api\/hassio_ingress\/;SameSite=Strict;Secure$/);
  assert.equal(hass.ws[0].slug, ADDON_SLUG);
});

test("admin on older core: falls back to supervisor/api tunnel", async () => {
  const hass = fakeHass({ dedicated: false }), f = fakeFetch([200]), doc = { cookie: "", location: { protocol: "http:" } };
  await postWrite({ slug: ADDON_SLUG, base: null }, hass, buildRequest(LR, "mode", "cool"), { fetch: f, doc });
  assert.equal(doc.cookie, ingressCookie("OLD", false));
  assert.ok(hass.ws.some((m) => m.endpoint === `/addons/${ADDON_SLUG}/info`));
});

test("expired session (401) re-opens ingress once, then gives up", async () => {
  const hass = fakeHass(), doc = { cookie: "", location: {} };
  const ok = fakeFetch([401, 200]);
  await postWrite({ slug: ADDON_SLUG, base: "/stale/" }, hass, buildRequest(LR, "setpoint", 70), { fetch: ok, doc });
  assert.equal(ok.calls.length, 2);
  const bad = fakeFetch([401, 401]);
  await assert.rejects(postWrite({ slug: ADDON_SLUG, base: null }, hass, buildRequest(LR, "setpoint", 70), { fetch: bad, doc }));
});

test("add-on gate errors surface (e.g. 409 dry run / vacation)", async () => {
  await assert.rejects(postWrite({ slug: ADDON_SLUG, base: null }, fakeHass(), buildRequest(LR, "setpoint", 70),
    { fetch: fakeFetch([409]), doc: { location: {} } }), /nope/);
});

test("non-admin: read-only, no WS session request and no fetch", async () => {
  const hass = fakeHass({ admin: false }), f = fakeFetch([200]);
  assert.equal(canWrite(hass), false);
  await assert.rejects(postWrite({ slug: ADDON_SLUG, base: null }, hass, buildRequest(LR, "setpoint", 72), { fetch: f, doc: {} }), /read-only/);
  assert.equal(hass.ws.length, 0);
  assert.equal(f.calls.length, 0);
});

test("ingress failure throws (card then shows the error, no fallback write path)", async () => {
  const hass = { user: { is_admin: true }, callWS: async () => { throw new Error("denied"); } };
  await assert.rejects(openIngress(hass, ADDON_SLUG, { location: {} }));
});

test("history: off is never heating; first row clamped; last_updated used", () => {
  const h = { [LR.entity]: [{ s: "heat", a: { temperature: 71, hvac_action: "heating" }, lu: 50 },
    { s: "off", a: { temperature: 71, hvac_action: "heating" }, lu: 200 }],
    [LR.indoor]: [{ s: "70.5", lu: 120 }], "weather.w": [{ s: "sunny", a: { temperature: 55 }, lu: 150 }] };
  const s = buildSeries(h, LR, "weather.w", 100, 400);
  assert.deepEqual(s.heating, [[100, 1], [200, 0]]);
  assert.deepEqual(s.setpoint, [[100, 71], [200, null]]);
});

test("status shows active mode and why (vacation)", () => {
  const off = statusOf({ state: "off", attributes: { hvac_action: "heating" } }, BATH, true);
  assert.equal(off.status, "Off");
  assert.match(off.why, /Vacation/);
});

test("graph axis uses the HA time zone", () => {
  assert.equal(fmtTime(1793357234, "America/New_York"), "6 AM");
  assert.doesNotMatch(graphSvg({ start: 1793270834, end: 1793357234, indoor: [[1793270834, 70]], setpoint: [], heating: [], outdoor: [] },
    "America/New_York"), /NaN/);
});

test("L1: mode is escaped in the class attribute", () => {
  assert.match(SRC, /class="pill \$\{s\.off \? "" : esc\(s\.mode\)\}"/);
});

// ---------- 0.4.0 ----------
test("0.4.0 MA low: radiant cap comes from /api/state, never above 85", () => {
  assert.equal(radiantCap(null), 85);
  assert.equal(radiantCap({ radiant_setpoint_max: 82 }), 82);
  assert.equal(radiantCap({ radiant_setpoint_max: 99 }), 85);
  assert.equal(buildRequest(BATH, "setpoint", 84, { cap: 82 }).body.temperature, 82);
  assert.equal(buildRequest(BATH, "setpoint", 95, { cap: 99 }).body.temperature, 85);
  assert.equal(buildRequest(LR, "setpoint", 84, { cap: 60 }).body.temperature, 84); // splits unaffected
  assert.doesNotMatch(SRC, /Room cap \$\{RADIANT_CAP\}/);
});

test("0.4.0 presets: Home/Sleep/Away/Eco go only to the add-on preset API", async () => {
  assert.deepEqual(PRESETS.map(([k]) => k), ["home", "sleep", "away", "eco"]);
  assert.deepEqual(buildPreset("eco"), { path: "api/preset", body: { preset: "eco" } });
  assert.equal(buildPreset("guest"), null);
  const hass = fakeHass(), f = fakeFetch([200]), doc = { cookie: "", location: { protocol: "https:" } };
  await postWrite({ slug: ADDON_SLUG, base: null }, hass, buildPreset("sleep"), { fetch: f, doc });
  assert.equal(f.calls[0].url, "/api/hassio_ingress/TOK/api/preset");
  assert.equal(f.calls[0].method, "POST");
  await assert.rejects(postWrite({ slug: ADDON_SLUG, base: null }, fakeHass({ admin: false }), buildPreset("home"), { fetch: f, doc }));
});

test("0.4.0 state read goes GET through ingress for admins only", async () => {
  const f = fakeFetch([200]), doc = { cookie: "", location: { protocol: "http:" } };
  await getState({ slug: ADDON_SLUG, base: null }, fakeHass(), { fetch: f, doc });
  assert.equal(f.calls[0].url, "/api/hassio_ingress/TOK/api/state");
  assert.equal(f.calls[0].method, undefined);
  const f2 = fakeFetch([]);
  assert.equal(await getState({ slug: ADDON_SLUG, base: null }, fakeHass({ admin: false }), { fetch: f2, doc }), null);
  assert.equal(f2.calls.length, 0);
});

test("0.4.0 defrost: status, why and graph band", () => {
  const st = { state: "heat", attributes: { hvac_action: "defrosting" } };
  const s = statusOf(st, LR, false);
  assert.equal(s.status, "Defrosting");
  assert.match(s.why, /not a manual change/);
  assert.equal(statusOf({ state: "heat", attributes: { hvac_action: "preheating" } }, LR, false).status, "Pre-heating");
  const hist = { [LR.entity]: [
    { s: "heat", a: { temperature: 71, hvac_action: "heating" }, lu: 1000 },
    { s: "heat", a: { temperature: 71, hvac_action: "defrosting" }, lu: 2000 },
    { s: "heat", a: { temperature: 71, hvac_action: "heating" }, lu: 2600 }] };
  const ser = buildSeries(hist, { ...LR, indoor: null }, null, 0, 4000);
  assert.deepEqual(ser.defrost.map((p) => p[1]), [0, 1, 0]);
  assert.match(graphSvg({ ...ser, indoor: [[0, 70]] }, "America/New_York"), /Defrosting/);
});

test("0.4.0 next change / why line", () => {
  const tz = "America/New_York";
  const wake = Date.parse("2026-10-30T10:00:00Z") / 1000, pre = wake - 45 * 60;
  assert.equal(nextLine({ reason: "no_change", next: { setpoint: 71, ts: pre, wake_ts: wake, preheat: true, profile: "home" } }, tz),
    "Next: 71° at 7:00 AM · pre-heat 5:45 AM");
  const t = Date.parse("2026-10-31T02:30:00Z") / 1000;
  assert.equal(nextLine({ reason: "ok", next: { setpoint: 68, ts: t, profile: "sleep" } }, tz), "Next: 68° at 10:00 PM (sleep)");
  assert.equal(nextLine({ reason: "manual_override", next: null }, tz, t), "Manual hold until 10:00 PM");
  assert.equal(nextLine({ reason: "defrosting", next: null }, tz), "Defrosting — app waits");
  assert.equal(nextLine(null, tz), "");
});

test("0.4.0 event strip: spans, open spans run to now, clipped, escaped kinds only", () => {
  const ev = [[50, "hold", "start"], [150, "hold", "end"], [300, "defrost", "start"], [10, "bogus", "start"]];
  assert.deepEqual(eventSpans(ev, 100, 400), [{ kind: "hold", t1: 100, t2: 150 }, { kind: "defrost", t1: 300, t2: 400 }]);
  const svg = eventStrip(ev, 100, 400);
  assert.match(svg, /<title>Defrost<\/title>/);
  assert.doesNotMatch(svg, /bogus/);
  assert.equal(eventSpans([], 0, 1).length, 0);
});

test("0.4.1 defrost_stuck shows on the event strip", () => {
  const svg = eventStrip([[200, "defrost_stuck", "start"]], 100, 400);
  assert.match(svg, /Defrost stuck/);
  assert.match(svg, /#ff3b3b/);
});

test("0.6.0 shared mode: split heat / cool buttons say they switch both splits; radiant and off do not", async () => {
  const { modeLabel } = await import("../smart-hvac-card.js");
  assert.equal(modeLabel("heat", false), "Heat · both");
  assert.equal(modeLabel("cool", false), "Cool · both");
  assert.equal(modeLabel("off", false), "Off");
  assert.equal(modeLabel("heat", true), "Heat");
  // the card still only asks the add-on; the add-on enforces the lock (no direct HA writes)
  assert.doesNotMatch(SRC, /callService/);
});

test("0.6.5 glance: one shared split mode, radiant never included, off/heat/cool only", async () => {
  const { sharedModeRequests, sharedSplitMode } = await import("../smart-hvac-card.js");
  const reqs = sharedModeRequests(DEFAULT_ZONES, "cool");
  assert.deepEqual(reqs.map((r) => r.path), ["api/zone/living_room/mode", "api/zone/zone_b/mode"]);
  assert.ok(reqs.every((r) => r.body.hvac_mode === "cool"));
  assert.deepEqual(sharedModeRequests(DEFAULT_ZONES, "dry"), []);
  assert.deepEqual(sharedModeRequests(DEFAULT_ZONES, "fan_only"), []);
  const h = (a, b, c = "heat") => ({ states: { [DEFAULT_ZONES[0].entity]: { state: a }, [DEFAULT_ZONES[1].entity]: { state: b },
    [DEFAULT_ZONES[2].entity]: { state: c } } });
  assert.equal(sharedSplitMode(h("heat", "heat", "off"), DEFAULT_ZONES), "heat");
  assert.equal(sharedSplitMode(h("cool", "off"), DEFAULT_ZONES), "cool");
  assert.equal(sharedSplitMode(h("off", "off"), DEFAULT_ZONES), "off");
  assert.equal(sharedSplitMode(h("heat", "dry"), DEFAULT_ZONES), "conflict");
});

test("0.6.5 glance row: now, target, and why; hold shows the 2 h auto time", async () => {
  const { glanceRow } = await import("../smart-hvac-card.js");
  const z = DEFAULT_ZONES[0];
  const hass = { states: { [z.entity]: { state: "heat", attributes: { temperature: 71, hvac_action: "heating", current_temperature: 69 } },
    [z.indoor]: { state: "68.9" } } };
  let r = glanceRow(hass, z, null, false, "America/New_York");
  assert.equal(r.now, 68.9); assert.equal(r.target, 71); assert.equal(r.doing, "Heating"); assert.equal(r.hold, false);
  const until = Math.floor(Date.now() / 1000) + 7200;
  r = glanceRow(hass, z, { zones: [{ name: z.zone, override_until: until }], last: { zones: {} } }, false, "America/New_York");
  assert.equal(r.hold, true); assert.match(r.doing, /^Hold · auto at /);
  r = glanceRow(hass, z, { zones: [], last: { zones: { [z.zone]: { preheat: true } } } }, false);
  assert.equal(r.doing, "Warming up");
  r = glanceRow({ states: { [z.entity]: { state: "off", attributes: {} } } }, z, null, false);
  assert.equal(r.target, null); assert.equal(r.off, true);
});

test("0.6.5 glance: header buttons are at least 48px (Echo Show touch target)", async () => {
  const fs = await import("node:fs");
  const src = fs.readFileSync(new URL("../smart-hvac-card.js", import.meta.url), "utf8");
  assert.match(src, /\.gh \.btn\{min-height:48px;min-width:48px\}/);
});
