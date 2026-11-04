import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { viewModel, chipCalls, render, esc, fmtCost, ring } from "../tesla-smart-charge-card.js";

const SRC = fs.readFileSync(new URL("../tesla-smart-charge-card.js", import.meta.url), "utf8");
const ST = { state: "start", last_updated: "x", attributes: {
  mode: "smart", soc: 58.6, target: 90, plan_amps: 18, kwh_needed: 33.3, charging: true, transport: "ble",
  start_at: "2026-11-03T21:00:00-05:00", eta_finish: "2026-11-04T05:00:00-05:00", finish_by: "2026-11-04T05:30:00-05:00",
  est_cost: 3.41, currency: "$", next_action: "charging at 18 A", dry_run: false,
  charge_now_entity: "input_boolean.car_charge_now", pause_entity: "input_boolean.car_charge_pause",
  trip_entity: "input_boolean.car_trip_mode" } };

test("only input_boolean on/off service calls; no iframe", () => {
  const calls = [...SRC.matchAll(/\["input_boolean", "(turn_on|turn_off)"/g)];
  assert.ok(calls.length >= 2);
  assert.doesNotMatch(SRC, /<iframe/);
  assert.doesNotMatch(SRC, /callService\((?!d, s, data)/);
});

test("view model + render", () => {
  const vm = viewModel(ST);
  assert.equal(vm.soc, 58.6);
  const html = render(vm, "Car", new Date("2026-11-03T19:00:00-05:00"));
  assert.match(html, /18 A/);
  assert.match(html, /\$3\.41/);
  assert.match(html, /via Bluetooth/);
  assert.match(html, /chip on" data-chip="smart"/);
});

test("chips: trip turns others off then trip on; tapping active turns it off; smart clears all", () => {
  const vm = viewModel(ST);
  assert.deepEqual(chipCalls(vm, "trip").at(-1), ["input_boolean", "turn_on", { entity_id: "input_boolean.car_trip_mode" }]);
  assert.equal(chipCalls(vm, "trip").length, 3);
  assert.equal(chipCalls(vm, "smart").length, 3);
  assert.ok(chipCalls(vm, "smart").every((c) => c[1] === "turn_off"));
  const tripVm = viewModel({ ...ST, attributes: { ...ST.attributes, mode: "trip" } });
  assert.deepEqual(chipCalls(tripVm, "trip"), [["input_boolean", "turn_off", { entity_id: "input_boolean.car_trip_mode" }]]);
});

test("missing helper: chip disabled, no calls", () => {
  const vm = viewModel({ ...ST, attributes: { ...ST.attributes, trip_entity: null } });
  assert.deepEqual(chipCalls(vm, "trip"), []);
  assert.match(render(vm), /data-chip="trip" disabled/);
});

test("escapes attribute text; alerts; missing sensor", () => {
  const vm = viewModel({ ...ST, attributes: { ...ST.attributes, last_error: "<img src=x onerror=1>", behind: true } });
  const html = render(vm);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /Behind plan/);
  assert.match(render(viewModel(undefined)), /Status sensor not found/);
  assert.equal(esc('"<'), "&quot;&lt;");
  assert.equal(fmtCost(null), "—");
  assert.match(ring(null, null, false), /—/);
});
