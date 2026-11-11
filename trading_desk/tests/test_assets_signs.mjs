/**
 * Schwab tab sign formatting. Placeholder amounts only.
 * Run: node --test trading_desk/tests/test_assets_signs.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const assetsPath = join(dirname(fileURLToPath(import.meta.url)), "../rootfs/app/static/assets.js");

function render(sign) {
  const n = sign === "pos" ? 1100 : sign === "neg" ? -1100 : 0;
  const pc = sign === "pos" ? 10 : sign === "neg" ? -10 : 0;
  const roll = {
    total: 5000, cash: 100, positions_value: 4900,
    day_pl: n, day_plpc: pc, unrealized_pl: n, unrealized_plpc: pc,
    missing: [], day_excluded: [], basis_excluded: [],
  };
  const view = { classList: { contains() { return false; } }, innerHTML: "" };
  const sandbox = {
    document: { getElementById() { return view; }, hidden: false },
    console,
    Math,
    Number,
    Date,
    String,
    isFinite,
    isNaN,
  };
  sandbox.window = sandbox;
  vm.runInNewContext(readFileSync(assetsPath, "utf8"), sandbox);
  sandbox.TDShared = {
    schwab: {
      ok: true,
      as_of: "2026-01-02T14:00:00Z",
      feed: "iex",
      market: { is_open: true },
      combined: roll,
      accounts: [{
        id: "individual",
        name: "Brokerage",
        cash: 100,
        positions: [{
          symbol: "AAA", name: "Example A Corp", qty: 10, price: 100, value: 1000,
          day_pl: n, day_plpc: pc, unrealized_pl: n, unrealized_plpc: pc,
          label: "live", cost_basis: 900,
        }],
        ...roll,
      }],
    },
  };
  sandbox.TDAssets.start();
  return view.innerHTML;
}

function text(html) {
  return html.replace(/<[^>]+>/g, "");
}

function fig(section, label) {
  const m = section.match(new RegExp("<span>" + label + "</span><b>(.*?)</b>"));
  assert.ok(m, "missing " + label + " figure");
  return text(m[1]);
}

function cell(section, label) {
  const m = section.match(new RegExp('data-l="' + label + '">(.*?)</td>'));
  assert.ok(m, "missing " + label + " cell");
  return text(m[1]);
}

function fields(html) {
  const parts = html.split("<section").slice(1);
  assert.equal(parts.length, 2);
  const account = parts[1].split("<table")[0];
  const table = parts[1].slice(parts[1].indexOf("<table"));
  return {
    combinedToday: fig(parts[0], "Today"),
    combinedUnrealized: fig(parts[0], "Unrealized"),
    accountToday: fig(account, "Today"),
    accountUnrealized: fig(account, "Unrealized"),
    rowToday: cell(table, "Today"),
    rowGain: cell(table, "Gain/loss"),
  };
}

test("signed Schwab fields use one sign", () => {
  const expect = {
    pos: "+$1,100.00 +10.00%",
    neg: "−$1,100.00 −10.00%",
    zero: "$0.00 0.00%",
  };
  for (const sign of ["pos", "neg", "zero"]) {
    const got = fields(render(sign));
    for (const [name, value] of Object.entries(got)) {
      assert.equal(value, expect[sign], sign + " " + name);
    }
  }
});

test("negative money is not doubled", () => {
  const html = render("neg");
  assert.equal(html.includes("−-$"), false);
  assert.equal(html.includes("-$"), false);
  assert.equal(html.includes("−$1,100.00"), true);
});
