/**
 * Main-tab bubble charts. Placeholder symbols only: AAA, BBB, FUNDX, BTC.
 * Run: node --test trading_desk/tests/test_bubbles.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const root = dirname(fileURLToPath(import.meta.url));
const bubbles = require("../rootfs/app/static/ui-trial/bubbles.js");
const bubblesPath = join(root, "../rootfs/app/static/ui-trial/bubbles.js");
const mainPath = join(root, "../rootfs/app/static/ui-trial/main.js");
const cssPath = join(root, "../rootfs/app/static/ui-trial/trial.css");
const legacyPath = join(root, "../rootfs/app/static/main.js");

const STAMP = "2026-01-02T15:07:00-05:00";

function rollupState() {
  return {
    ts_ms: Date.parse(STAMP),
    account: { cash: 0 },
    positions: [],
    schwab: {
      ok: true,
      price_as_of: STAMP,
      prices_note: null,
      market: { is_open: true },
      combined: { cash: 125, day_pl: 131, unrealized_pl: 170 },
      accounts: [
        {
          id: "individual", name: "Brokerage", mask: "0000", cash: 100,
          positions: [
            { symbol: "AAA", name: "Example A Corp", qty: 10, cost_basis: 1000, value: 1100,
              price: 110, prev_close: 100, day_pl: 100, day_plpc: 10, quote: "alpaca",
              price_ts: STAMP, price_age_s: 30, label: "live" },
            { symbol: "BBB", name: "Example B Inc", qty: 5.5, cost_basis: null, value: 110,
              price: 20, prev_close: 18, day_pl: 11, day_plpc: (20 / 18 - 1) * 100, quote: "alpaca",
              price_ts: "2026-01-02T15:06:00-05:00", price_age_s: 40, label: "live" },
          ],
        },
        {
          id: "roth", name: "Retirement", mask: "0000", cash: 25,
          positions: [
            { symbol: "AAA", name: "Example A Corp", qty: 2, cost_basis: 150, value: 220,
              price: 110, prev_close: 100, day_pl: 20, day_plpc: 10, quote: "alpaca",
              price_ts: STAMP, price_age_s: 20, label: "live" },
            { symbol: "FUNDX", name: "Example Index Fund", type: "mutual_fund", qty: 3,
              cost_basis: null, value: 150, price: 50, day_pl: null, day_plpc: null,
              quote: "static", label: "snapshot Jan 2 ET" },
          ],
        },
      ],
    },
    crypto: {},
    desk: { holdings: { accounts: [{ id: "ignored", name: "Desk", positions: [
      { symbol: "BBB", qty: 1, value: 9, cost_basis: 8, price: 9 },
    ] }] } },
    ha_prices: { AAA: { price: 999, prev_close: 100 } },
    totals: { day: { schwab_pl: 131, alpaca_pl: null }, unrealized: { schwab_pl: 170 } },
  };
}

function rowsOf(state) {
  return bubbles.buildRows(state);
}

test("gain header matches the hand sum and leaves out a null cost", () => {
  const header = bubbles.gainHeader(rowsOf(rollupState()));
  const symbols = header.included.map((r) => r.symbol + ":" + r.tag).sort();
  assert.deepEqual(symbols, ["AAA:Roth", "AAA:Schwab"]);
  assert.equal(header.pl, 170);
  assert.equal(header.cost, 1150);
  assert.ok(Math.abs(header.pct - (170 / 1150 * 100)) < 1e-9);
  const left = header.excluded.map((r) => r.symbol).sort();
  assert.deepEqual(left, ["BBB", "FUNDX"]);
  assert.equal(header.excluded.find((r) => r.symbol === "BBB").reason, "basis n/a");
});

test("day header matches the hand sum, includes cash, and leaves out FUNDX", () => {
  const state = rollupState();
  const header = bubbles.dayHeader(rowsOf(state), state);
  const symbols = header.included.map((r) => r.symbol + ":" + r.account_id).sort();
  assert.deepEqual(symbols, ["AAA:individual", "AAA:roth", "BBB:individual"]);
  assert.equal(header.pl, 131);
  assert.equal(header.value, 1430);
  assert.equal(header.cash, 125);
  assert.equal(header.prior, 1424);
  assert.ok(Math.abs(header.pct - (131 / 1424 * 100)) < 1e-9);
  assert.equal(header.excluded.length, 1);
  assert.equal(header.excluded[0].symbol, "FUNDX");
  assert.equal(header.excluded[0].reason, "no day change");
  // Cash of 0 reduces to the position prior-value formula.
  state.schwab.combined.cash = 0;
  state.schwab.accounts[0].cash = 0;
  state.schwab.accounts[1].cash = 0;
  const bare = bubbles.dayHeader(rowsOf(state), state);
  assert.equal(bare.cash, 0);
  assert.equal(bare.prior, 1430 - 131);
  assert.ok(Math.abs(bare.pct - (131 / (1430 - 131) * 100)) < 1e-9);
});

test("the same symbol in two books stays two bubbles", () => {
  const state = rollupState();
  state.positions = [{
    symbol: "AAA", qty: 2, avg_entry: 100, market_value: 220, price: 110,
    day_change_pct: 4, price_ts_ms: Date.parse(STAMP),
  }];
  const rows = rowsOf(state).filter((r) => r.symbol === "AAA");
  assert.equal(rows.length, 3);
  const tags = rows.map((r) => r.tag).sort();
  assert.deepEqual(tags, ["Alpaca", "Roth", "Schwab"]);
  const day = rows.map((r) => r.day_plpc).sort((a, b) => a - b);
  assert.equal(day[0], 4);
  assert.equal(day[1], 10);
  assert.equal(day[2], 10);
  const basis = rows.map((r) => r.cost_basis).sort((a, b) => a - b);
  assert.deepEqual(basis, [150, 200, 1000]);
});

test("a short is left out of Gain/Loss and a zero stays on the origin", () => {
  const state = {
    ts_ms: Date.parse(STAMP),
    account: { cash: 0 },
    schwab: { ok: false },
    positions: [
      { symbol: "AAA", qty: 4, avg_entry: 25, market_value: 100, price: 25, day_change_pct: 0, price_ts_ms: Date.parse(STAMP) },
      { symbol: "BBB", qty: -4, avg_entry: 50, market_value: -200, price: 50, day_change_pct: 2, price_ts_ms: Date.parse(STAMP) },
    ],
    crypto: {},
    desk: {},
    totals: { day: {} },
  };
  const rows = rowsOf(state);
  const gain = bubbles.gainHeader(rows);
  assert.deepEqual(gain.included.map((r) => r.symbol), ["AAA"]);
  assert.equal(gain.included[0].unrealized_pl, 0);
  assert.equal(bubbles.tone(0), "flat");
  assert.equal(gain.excluded.find((r) => r.symbol === "BBB").reason, "basis n/a");
  const day = bubbles.dayHeader(rows, state);
  assert.equal(day.included.some((r) => r.symbol === "BBB"), true);
  const zero = day.included.find((r) => r.symbol === "AAA");
  assert.equal(zero.day_pl, 0);
  assert.equal(zero.day_plpc, 0);
  const html = bubbles.cardHtml(state, 640, 320, "gain", false, "");
  assert.match(html, /data-symbol="AAA"[^>]*data-tone="flat"/);
});

test("a static fund with a cost is on Gain/Loss and off the day chart", () => {
  const state = rollupState();
  state.schwab.accounts[1].positions[1].cost_basis = 120;
  state.schwab.accounts[1].positions[1].day_pl = 5;
  const rows = rowsOf(state);
  const fund = rows.find((r) => r.symbol === "FUNDX");
  assert.equal(fund.quote, "static");
  assert.equal(fund.day_pl, null);
  assert.equal(fund.no_basis, false);
  assert.equal(fund.unrealized_pl, 150 - 120);
  const gain = bubbles.gainHeader(rows);
  const day = bubbles.dayHeader(rows, state);
  assert.equal(gain.included.some((r) => r.symbol === "FUNDX"), true);
  assert.equal(day.included.some((r) => r.symbol === "FUNDX"), false);
  const info = bubbles.infoParagraphs("day", rows, state).join("\n");
  assert.match(info, /FUNDX \(Roth\)/);
  assert.match(info, /static mutual fund/i);
  const gainInfo = bubbles.infoParagraphs("gain", rows, state).join("\n");
  assert.match(gainInfo, /Included:.*FUNDX \(Roth\)/);
  assert.equal(/Left out[^.]*FUNDX/.test(gainInfo), false);
});

test("Trezor is out of Gain/Loss, in the day chart, and named in Info", () => {
  const state = rollupState();
  state.crypto = {
    btc_qty: 1, btc_price: 100, value: 100, day_pl: 5, day_plpc: 5,
    price_as_of_ms: Date.parse(STAMP),
  };
  const rows = rowsOf(state);
  assert.equal(rows.filter((r) => r.symbol === "BTC").length, 1);
  const btc = rows.find((r) => r.source === "trezor");
  assert.equal(btc.no_basis, true);
  assert.equal(bubbles.gainHeader(rows).included.some((r) => r.symbol === "BTC"), false);
  assert.equal(bubbles.dayHeader(rows, state).included.some((r) => r.symbol === "BTC"), true);
  const info = bubbles.infoParagraphs("gain", rows, state).join(" ");
  assert.match(info, /BTC \(Trezor\)/);
});

test("day header matches Schwab + Alpaca + Trezor when each book day is the row sum", () => {
  const state = {
    ts_ms: Date.parse(STAMP),
    account: { cash: 0 },
    schwab: {
      ok: true, market: { is_open: true }, combined: { cash: 0 },
      accounts: [{ id: "individual", name: "Brokerage", mask: "0000", cash: 0, positions: [
        { symbol: "AAA", qty: 2, cost_basis: 100, value: 110, price: 55, prev_close: 50,
          day_pl: 10, day_plpc: 10, quote: "alpaca", price_ts: STAMP, price_age_s: 10, label: "live" },
      ] }],
    },
    positions: [{ symbol: "BBB", qty: 2, avg_entry: 100, market_value: 220, price: 110, day_change_pct: 10, price_ts_ms: Date.parse(STAMP) }],
    crypto: { btc_qty: 1, value: 105, day_pl: 5, day_plpc: 5, price_as_of_ms: Date.parse(STAMP) },
    totals: { day: { schwab_pl: 10, alpaca_pl: 20, combined_pl: 30 } },
    desk: {},
  };
  const rows = rowsOf(state);
  const header = bubbles.dayHeader(rows, state);
  assert.ok(Math.abs(header.pl - 35) < 1e-9);
  assert.equal(header.cash, 0);
  assert.ok(Math.abs(header.prior - 400) < 1e-9);
  assert.ok(Math.abs(header.pct - 8.75) < 1e-9);
  const book = 10 + 20 + 5;
  assert.ok(Math.abs(header.pl - book) < 1e-9);
  state.totals.day.alpaca_pl = 50;
  const note = bubbles.infoParagraphs("day", rows, state).join(" ");
  assert.match(note, /These bubbles sum to \+\$35\.00/);
  assert.match(note, /The Day total on this page is \+\$65\.00/);
});

test("gain header matches a merged headline when the exclusion set is the same", () => {
  const state = rollupState();
  state.positions = [{ symbol: "AAA", qty: 4, avg_entry: 10, market_value: 48, price: 12, day_change_pct: 0 }];
  state.crypto = { value: 100, day_pl: 1, day_plpc: 1, btc_qty: 1 };
  const rows = rowsOf(state);
  const header = bubbles.gainHeader(rows);
  // Mirror of the Main table merge. Every lot of a symbol has a cost, or the symbol is left out entirely.
  const merged = {};
  for (const p of rows) {
    if (p.value == null) continue;
    const r = merged[p.symbol] || (merged[p.symbol] = { val: 0, cost: 0, noBasis: false });
    r.val += Number(p.value) || 0;
    if (p.qty != null && p.qty < 0) r.noBasis = true;
    else if (!p.no_basis && p.cost_basis > 0) r.cost += p.cost_basis;
    else r.noBasis = true;
  }
  let pl = 0, cost = 0;
  for (const sym of Object.keys(merged)) {
    const r = merged[sym];
    if (!r.noBasis && r.cost > 0) { pl += r.val - r.cost; cost += r.cost; }
  }
  assert.equal(header.pl, pl);
  assert.ok(Math.abs(header.pct - (pl / cost * 100)) < 1e-9);
  assert.equal(header.included.some((r) => r.symbol === "BTC"), false);
});

test("area is proportional to value, then clamped", () => {
  const big = bubbles.radiusPx(400, 400, 10, 80);
  const small = bubbles.radiusPx(100, 400, 10, 80);
  assert.equal(big, 80);
  assert.ok(Math.abs((small * small) / (big * big) - (100 / 400)) < 1e-9);
  assert.equal(bubbles.radiusPx(1, 10000, 22, 56), 22);
  assert.equal(bubbles.radiusPx(10000, 10000, 22, 56), 56);
});

test("colors are good, bad, and flat, with no orange or amber", () => {
  assert.equal(bubbles.tone(12), "good");
  assert.equal(bubbles.tone(-3), "bad");
  assert.equal(bubbles.tone(0), "flat");
  const css = readFileSync(cssPath, "utf8");
  const js = readFileSync(bubblesPath, "utf8");
  assert.match(css, /\.bb-dot\.good[\s\S]*var\(--up\)/);
  assert.match(css, /\.bb-dot\.bad[\s\S]*var\(--dn\)/);
  assert.match(css, /\.bb-dot\.flat[\s\S]*var\(--mut\)/);
  assert.match(css, /\.bb-toggle button\.on \{ background: var\(--acc\)/);
  assert.match(css, /--acc: #3e6ae1/);
  assert.equal(/\b(orange|amber)\b/i.test(css + js), false);
  assert.equal(/#c9892b|#f59e0b|#d4a054|#ffa500/i.test(css + js), false);
});

test("layout stays inside the plot at phone, tablet, and desktop widths", () => {
  const state = rollupState();
  state.positions = [
    { symbol: "AAA", qty: 2, avg_entry: 40, market_value: 100, price: 50, day_change_pct: 25 },
    { symbol: "BBB", qty: 1, avg_entry: 80, market_value: 40, price: 40, day_change_pct: -50 },
  ];
  state.crypto = { value: 400, day_pl: -10, day_plpc: -2.5, btc_qty: 1, price_as_of_ms: Date.parse(STAMP) };
  const rows = rowsOf(state);
  const widths = [390, 1280, 1440].concat([390, 1280, 1440].map((v) => bubbles.plotWidthForViewport(v)));
  const seen = new Set();
  for (const width of widths) {
    if (seen.has(width)) continue;
    seen.add(width);
    for (const mode of ["gain", "day"]) {
      const points = bubbles.chartPoints(rows, mode);
      const box = bubbles.layout(points, width, 320);
      assert.ok(box.bubbles.length >= 2, mode + " " + width);
      for (const b of box.bubbles) {
        assert.ok(b.cx - b.r >= -0.01, "left " + width);
        assert.ok(b.cx + b.r <= width + 0.01, "right " + width);
        assert.ok(b.cy - b.r >= -0.01, "top " + width);
        assert.ok(b.cy + b.r <= 320 + 0.01, "bottom " + width);
        if (width >= 320) assert.ok(b.r * 2 >= 44 - 0.01, "target " + width + " r=" + b.r);
      }
      for (const t of box.ticks) {
        assert.ok(t.box.l >= -0.01 && t.box.r <= width + 0.01, t.text);
        assert.ok(t.box.t >= -0.01 && t.box.b <= 320.01, t.text);
      }
      const same = box.ticks.filter((t) => t.axis === "x");
      for (let a = 0; a < same.length; a++) {
        for (let b = a + 1; b < same.length; b++) {
          const overlap = same[a].box.l < same[b].box.r + 4 && same[b].box.l < same[a].box.r + 4;
          assert.equal(overlap, false, width + " " + same[a].text + " / " + same[b].text);
        }
      }
    }
  }
  const css = readFileSync(cssPath, "utf8");
  assert.match(css, /\.bb-toggle button[\s\S]*min-height: 44px/);
  assert.match(css, /\.bb-info, \.bb-close[\s\S]*min-height: 44px/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.bb-dot/);
  assert.match(css, /\.bb-dot[\s\S]*animation: none/);
  assert.match(css, /\.bb-dot[\s\S]*transition: none/);
});

test("Updated is an ET clock and Info uses two spaces after periods", () => {
  assert.equal(bubbles.formatET(Date.parse("2026-11-12T16:07:00Z")), "Nov 12, 11:07 AM");
  const state = rollupState();
  const rows = rowsOf(state);
  const gain = bubbles.gainHeader(rows);
  assert.equal(bubbles.updatedLine(gain.included, state), "Updated Jan 2, 3:07 PM ET");
  state.schwab.market.is_open = true;
  rows.forEach((r) => { r.price_age_s = 500; });
  assert.match(bubbles.updatedLine(gain.included, state), /Updated Jan 2, 3:07 PM ET\.  Prices are stale\./);
  state.schwab.market.is_open = false;
  assert.match(bubbles.updatedLine(gain.included, state), /Market is closed\./);
  assert.equal(/Prices are stale/.test(bubbles.updatedLine(gain.included, state)), false);
  for (const mode of ["gain", "day"]) {
    const lines = bubbles.infoParagraphs(mode, rowsOf(rollupState()), rollupState());
    assert.ok(lines.length >= 4);
    for (const line of lines) {
      assert.equal(/\. (?! )/.test(line), false, line);
      assert.equal(/\b(orange|amber)\b/i.test(line), false);
    }
  }
  const html = bubbles.cardHtml(rollupState(), 640, 320, "day", true, "");
  assert.match(html, /Gain\/Loss/);
  assert.match(html, /Positions/);
  assert.match(html, /class="bb-seg on"/);
  assert.match(html, /About this chart/);
  assert.match(html, /data-bb="close"/);
  assert.match(html, />Close</);
  assert.match(html, /Today \$/);
  assert.match(html, /Today %/);
  const empty = bubbles.cardHtml({ schwab: { ok: false }, positions: [], crypto: {}, desk: {} }, 640, 320, "gain", false, "");
  assert.match(empty, /No positions with a known cost basis yet\./);
  const noDay = bubbles.cardHtml({
    schwab: { ok: true, accounts: [{ id: "roth", name: "Retirement", positions: [
      { symbol: "FUNDX", qty: 3, value: 150, cost_basis: 120, quote: "static", price: 50 },
    ] }] },
    positions: [], crypto: {}, desk: {},
  }, 640, 320, "day", false, "");
  assert.match(noDay, /Today's change isn't available yet\./);
});

test("bubble clicks do not fetch, and the live file wins over the desk feed", () => {
  const main = readFileSync(mainPath, "utf8");
  const start = main.indexOf("function onBubbleClick");
  const end = main.indexOf("function unrealized");
  const body = main.slice(start, end);
  assert.ok(start > 0 && end > start);
  assert.equal(body.includes("fetch"), false);
  assert.equal(body.includes("EventSource"), false);
  assert.equal(/orders/i.test(body), false);
  assert.match(main, /sessionStorage\.getItem\("td-bubble-mode"\)/);
  assert.match(main, /sessionStorage\.setItem\("td-bubble-mode"/);
  assert.equal(main.includes("localStorage"), false);
  const js = readFileSync(bubblesPath, "utf8");
  assert.equal(js.includes("fetch("), false);
  assert.equal(js.includes("EventSource"), false);
  assert.equal(js.includes("localStorage"), false);
  assert.equal(/https?:\/\//.test(js), false);
  assert.equal(/\b\d{8,}\b/.test(js), false);
  const state = rollupState();
  const live = rowsOf(state).map((r) => r.symbol + ":" + r.value).sort();
  assert.equal(live.includes("BBB:9"), false);
  assert.equal(live.includes("AAA:9990"), false);
  state.schwab.ok = false;
  const fallback = rowsOf(state);
  assert.equal(fallback.some((r) => r.symbol === "BBB" && r.value === 9), true);
  assert.equal(fallback.some((r) => r.account_id === "individual"), false);
  const html = bubbles.cardHtml(state, 640, 320, "gain", false, "alpaca|alpaca|AAA|0");
  assert.match(html, /type="button"/);
  assert.equal(html.includes("<a "), false);
  assert.equal(/<form/i.test(html), false);
});

test("desk fallback still draws Alpaca when the share file is absent", () => {
  const state = {
    ts_ms: Date.parse(STAMP),
    account: { cash: 15 },
    schwab: { ok: false, error: "no holdings file" },
    positions: [{ symbol: "AAA", qty: 2, avg_entry: 40, market_value: 100, price: 50, day_change_pct: 25, price_ts_ms: Date.parse(STAMP) }],
    crypto: {},
    desk: { holdings: { accounts: [{ id: "individual", name: "Brokerage", mask: "0000", cash: 10, positions: [
      { symbol: "BBB", qty: 4, value: 40, cost_basis: 32, price: 10, prev_close: 8, quote: "alpaca" },
    ] }] } },
    ha_prices: { BBB: { price: 12, prev_close: 10 } },
    totals: { day: { schwab_pl: null, alpaca_pl: 20 } },
  };
  const rows = rowsOf(state);
  const bbb = rows.find((r) => r.symbol === "BBB");
  assert.equal(bbb.source, "schwab");
  assert.equal(bbb.value, 48);
  assert.equal(bbb.day_pl, 8);
  assert.ok(Math.abs(bbb.day_plpc - 20) < 1e-9);
  const aaa = rows.find((r) => r.source === "alpaca");
  assert.equal(aaa.symbol, "AAA");
  assert.equal(aaa.value, 100);
  const legacy = readFileSync(legacyPath, "utf8");
  assert.equal(legacy.includes("TDBubbles"), false);
  assert.equal(legacy.includes("bb-toggle"), false);
});
