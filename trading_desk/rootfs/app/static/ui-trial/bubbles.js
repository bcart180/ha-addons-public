/* Main-tab bubble charts. Presentation only: no orders, no writes.
   One bubble per open row (source + account + symbol). Placeholder-safe. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TDBubbles = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var EMPTY_GAIN = "No positions with a known cost basis yet.";
  var EMPTY_DAY = "Today's change isn't available yet.";
  var PLOT_H = 320;

  function escText(t) {
    return String(t == null ? "" : t).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }
  function escAttr(t) {
    return escText(t).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function num(v) {
    if (v == null || v === "" || typeof v === "boolean") return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function timeMs(v) {
    if (v == null || v === "") return null;
    if (typeof v === "number" && isFinite(v)) return v;
    var t = Date.parse(String(v));
    return isNaN(t) ? null : t;
  }
  function para(list) {
    var i, s, out = [];
    for (i = 0; i < list.length; i++) {
      s = String(list[i] == null ? "" : list[i]).replace(/^\s+|\s+$/g, "");
      if (!s) continue;
      if (s.charAt(s.length - 1) !== ".") s += ".";
      out.push(s);
    }
    return out.join("  ");
  }
  function plainMoney(v) {
    if (v == null || isNaN(v)) return "—";
    var body = "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (v < 0 ? "−" : "") + body;
  }
  function signedMoney(v) {
    if (v == null || isNaN(v)) return "—";
    var body = "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (v > 0 ? "+" : v < 0 ? "−" : "") + body;
  }
  function signedPct(v) {
    if (v == null || isNaN(v)) return "—";
    return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%";
  }
  function tone(v) {
    if (v == null || isNaN(v) || v === 0) return "flat";
    return v > 0 ? "good" : "bad";
  }
  function radiusPx(value, maxValue, minR, maxR) {
    var v = Math.abs(num(value) || 0);
    var maxV = Math.abs(num(maxValue) || 0);
    if (!(maxV > 0)) return minR;
    var r = maxR * Math.sqrt(v / maxV);
    if (r < minR) r = minR;
    if (r > maxR) r = maxR;
    return r;
  }
  function tagFor(source, account) {
    if (source === "alpaca") return "Alpaca";
    if (source === "trezor") return "Trezor";
    var id = String((account && account.id) || "").toLowerCase();
    var name = String((account && account.name) || "").toLowerCase();
    if (id.indexOf("roth") !== -1 || name.indexOf("roth") !== -1) return "Roth";
    return "Schwab";
  }
  function maskOf(account) {
    var raw = String((account && account.mask) || "").replace(/\D/g, "");
    if (!raw) return "";
    return raw.slice(-4);
  }
  function labelOf(row) {
    return row.symbol + " (" + row.tag + ")";
  }

  function finishDay(row) {
    if (row.day_plpc == null && row.price != null && row.prev_close) {
      row.day_plpc = (row.price / row.prev_close - 1) * 100;
    }
    if (row.day_pl == null && row.day_plpc != null && row.value != null) {
      var factor = 1 + row.day_plpc / 100;
      if (factor && isFinite(factor)) row.day_pl = row.value - row.value / factor;
    }
    if (row.day_pl == null && row.qty != null && row.price != null && row.prev_close) {
      row.day_pl = row.qty * (row.price - row.prev_close);
    }
  }
  function finishEconomics(row) {
    var short = row.qty != null && row.qty < 0;
    var basis = row.cost_basis;
    if (short || basis == null || !(basis > 0)) {
      row.no_basis = true;
      row.cost_basis = null;
      row.unrealized_pl = null;
      row.unrealized_plpc = null;
      return;
    }
    row.no_basis = false;
    if (row.value == null) {
      row.unrealized_pl = null;
      row.unrealized_plpc = null;
      return;
    }
    row.unrealized_pl = row.value - basis;
    row.unrealized_plpc = row.unrealized_pl / basis * 100;
  }
  function baseRow(fields) {
    var row = {
      source: fields.source,
      account_id: fields.account_id,
      account_name: fields.account_name || "",
      mask: fields.mask || "",
      tag: fields.tag,
      symbol: fields.symbol,
      name: fields.name || "",
      qty: fields.qty,
      value: fields.value,
      cost_basis: fields.cost_basis,
      day_pl: fields.day_pl,
      day_plpc: fields.day_plpc,
      price: fields.price,
      prev_close: fields.prev_close,
      quote: fields.quote || "",
      price_ts_ms: fields.price_ts_ms,
      price_age_s: fields.price_age_s,
      label: fields.label || "",
      fromFile: !!fields.fromFile,
      no_basis: false,
      key: ""
    };
    if (row.quote === "static") {
      finishDay(row);
      row.day_pl = null;
      row.day_plpc = null;
    } else finishDay(row);
    finishEconomics(row);
    return row;
  }

  function pushAccount(out, account, index, fromFile) {
    var positions = (account && account.positions) || [];
    var i, p, quote;
    for (i = 0; i < positions.length; i++) {
      p = positions[i] || {};
      if (!p.symbol) continue;
      quote = p.quote ? String(p.quote) : (p.type === "mutual_fund" ? "static" : "");
      out.push(baseRow({
        source: "schwab",
        account_id: account && account.id ? String(account.id) : ("schwab-" + index),
        account_name: account && account.name ? String(account.name) : "",
        mask: maskOf(account),
        tag: tagFor("schwab", account),
        symbol: String(p.symbol),
        name: p.name ? String(p.name) : "",
        qty: num(p.qty),
        value: num(p.value),
        cost_basis: num(p.cost_basis),
        day_pl: num(p.day_pl),
        day_plpc: num(p.day_plpc),
        price: num(p.price),
        prev_close: num(p.prev_close),
        quote: quote,
        price_ts_ms: timeMs(p.price_ts != null ? p.price_ts : p.price_ts_ms),
        price_age_s: num(p.price_age_s),
        label: p.label ? String(p.label) : "",
        fromFile: fromFile
      }));
    }
  }
  function pushAlpaca(out, p) {
    var qty, entry, value, dpc, basis;
    if (!p || !p.symbol) return;
    qty = num(p.qty);
    entry = num(p.avg_entry);
    value = num(p.market_value);
    dpc = num(p.day_change_pct);
    basis = (qty != null && qty >= 0 && entry != null && entry > 0) ? Math.abs(qty * entry) : null;
    out.push(baseRow({
      source: "alpaca",
      account_id: "alpaca",
      account_name: "Alpaca",
      mask: "",
      tag: "Alpaca",
      symbol: String(p.symbol),
      name: "",
      qty: qty,
      value: value,
      cost_basis: basis,
      day_pl: null,
      day_plpc: dpc,
      price: num(p.price),
      prev_close: null,
      quote: "alpaca",
      price_ts_ms: timeMs(p.price_ts_ms),
      price_age_s: null,
      label: "",
      fromFile: false
    }));
  }
  function pushTrezor(out, c) {
    var value = num(c && c.value);
    if (value == null) return;
    out.push(baseRow({
      source: "trezor",
      account_id: "trezor",
      account_name: "Trezor",
      mask: "",
      tag: "Trezor",
      symbol: "BTC",
      name: "Bitcoin (Trezor)",
      qty: num(c.btc_qty),
      value: value,
      cost_basis: null,
      day_pl: num(c.day_pl),
      day_plpc: num(c.day_plpc),
      price: num(c.btc_price),
      prev_close: null,
      quote: "",
      price_ts_ms: timeMs(c.price_as_of_ms),
      price_age_s: null,
      label: "",
      fromFile: false
    }));
  }
  function applyHaPrices(rows, hp) {
    var i, row, x, px, prev;
    for (i = 0; i < rows.length; i++) {
      row = rows[i];
      if (row.source !== "schwab" || row.fromFile) continue;
      x = hp[row.symbol];
      if (!x || num(x.price) == null) continue;
      px = num(x.price);
      row.price = px;
      if (row.qty != null) row.value = row.qty * px;
      prev = num(x.prev_close);
      if (prev && row.quote !== "static") {
        row.prev_close = prev;
        row.day_pl = null;
        row.day_plpc = null;
        finishDay(row);
      }
      finishEconomics(row);
    }
  }
  function assignKeys(rows) {
    var seen = {}, i, base, n;
    for (i = 0; i < rows.length; i++) {
      base = rows[i].source + "|" + rows[i].account_id + "|" + rows[i].symbol;
      n = seen[base] || 0;
      seen[base] = n + 1;
      rows[i].key = base + "|" + n;
    }
  }

  function buildRows(state) {
    var out = [], sw, live, accounts, hold, positions, i, now;
    state = state || {};
    sw = state.schwab;
    live = !!(sw && sw.ok);
    if (live) {
      accounts = sw.accounts || [];
      for (i = 0; i < accounts.length; i++) pushAccount(out, accounts[i], i, true);
    } else {
      hold = ((state.desk || {}).holdings || {}).accounts || [];
      for (i = 0; i < hold.length; i++) pushAccount(out, hold[i], i, false);
    }
    positions = state.positions || [];
    for (i = 0; i < positions.length; i++) pushAlpaca(out, positions[i]);
    pushTrezor(out, state.crypto || {});
    if (!live) applyHaPrices(out, state.ha_prices || {});
    now = num(state.ts_ms);
    for (i = 0; i < out.length; i++) {
      if (out[i].price_age_s == null && out[i].price_ts_ms != null && now != null) {
        out[i].price_age_s = Math.max(0, (now - out[i].price_ts_ms) / 1000);
      }
    }
    assignKeys(out);
    return out;
  }

  function onGain(row) {
    return row.value != null && !row.no_basis && row.unrealized_pl != null && row.unrealized_plpc != null &&
      isFinite(row.unrealized_pl) && isFinite(row.unrealized_plpc);
  }
  function onDay(row) {
    return row.value != null && row.day_pl != null && row.day_plpc != null &&
      isFinite(row.day_pl) && isFinite(row.day_plpc);
  }
  function gainReason(row) {
    if (row.value == null) return "no market value";
    return "basis n/a";
  }
  function dayReason(row) {
    if (row.value == null) return "no market value";
    return "no day change";
  }
  function split(rows, keep, reason) {
    var included = [], excluded = [], i, row;
    for (i = 0; i < rows.length; i++) {
      row = rows[i];
      if (keep(row)) included.push(row);
      else excluded.push({
        symbol: row.symbol, tag: row.tag, reason: reason(row), quote: row.quote || "",
        key: row.key, source: row.source
      });
    }
    return { included: included, excluded: excluded };
  }
  function sum(rows, field) {
    var i, n = 0;
    for (i = 0; i < rows.length; i++) n += rows[i][field];
    return n;
  }
  function schwabCash(state) {
    var sw = state && state.schwab, accounts, i, c, total = 0, any = false;
    if (sw && sw.ok && sw.combined && num(sw.combined.cash) != null) return num(sw.combined.cash);
    accounts = (((state || {}).desk || {}).holdings || {}).accounts || [];
    for (i = 0; i < accounts.length; i++) {
      c = num(accounts[i].cash);
      if (c != null) { total += c; any = true; }
    }
    return any ? total : 0;
  }
  function alpacaCash(state) {
    var c = num(((state || {}).account || {}).cash);
    return c == null ? 0 : c;
  }
  function bookCash(state, included) {
    var src = {}, i, cash = 0;
    for (i = 0; i < included.length; i++) src[included[i].source] = true;
    if (src.schwab) cash += schwabCash(state);
    if (src.alpaca) cash += alpacaCash(state);
    return cash;
  }

  function gainHeader(rows) {
    var parts = split(rows || [], onGain, gainReason);
    var included = parts.included;
    var pl = included.length ? sum(included, "unrealized_pl") : null;
    var cost = included.length ? sum(included, "cost_basis") : null;
    return {
      empty: !included.length,
      pl: pl,
      pct: cost ? pl / cost * 100 : null,
      cost: cost,
      included: included,
      excluded: parts.excluded
    };
  }
  function dayHeader(rows, state) {
    var parts = split(rows || [], onDay, dayReason);
    var included = parts.included;
    var pl = included.length ? sum(included, "day_pl") : null;
    var value = included.length ? sum(included, "value") : null;
    var cash = included.length ? bookCash(state, included) : 0;
    var prior = included.length ? (value - pl + cash) : null;
    return {
      empty: !included.length,
      pl: pl,
      pct: prior ? pl / prior * 100 : null,
      value: value,
      cash: cash,
      prior: prior,
      included: included,
      excluded: parts.excluded
    };
  }

  function names(list) {
    var copy = list.slice().sort(function (a, b) {
      if (a.symbol !== b.symbol) return a.symbol < b.symbol ? -1 : 1;
      if (a.tag !== b.tag) return a.tag < b.tag ? -1 : 1;
      return a.key < b.key ? -1 : 1;
    });
    var i, out = [];
    for (i = 0; i < copy.length; i++) out.push(labelOf(copy[i]));
    return out;
  }
  function accountDay(state) {
    var d = ((state || {}).totals || {}).day || {};
    var c = (state || {}).crypto || {};
    var parts = [], sw = num(d.schwab_pl), al = num(d.alpaca_pl), tr = num(c.day_pl);
    if (sw != null) parts.push(sw);
    if (al != null) parts.push(al);
    if (tr != null) parts.push(tr);
    if (!parts.length) return null;
    var i, n = 0;
    for (i = 0; i < parts.length; i++) n += parts[i];
    return n;
  }
  function infoParagraphs(mode, rows, state) {
    var gain = mode !== "day";
    var header = gain ? gainHeader(rows) : dayHeader(rows, state);
    var lines = [];
    var included = names(header.included);
    var missing = [], basis = [], dayMiss = [], i, item;
    if (gain) {
      lines.push(para([
        "X is the unrealized gain or loss in dollars versus cost",
        "Y is that gain or loss as a percent of cost",
        "Bubble area follows market value"
      ]));
      lines.push(para([
        "Green is a gain",
        "Red is a loss",
        "Zero is neutral and sits on the origin"
      ]));
      lines.push(para([
        "The header adds unrealized dollars for positions with a known cost",
        "The percent divides that sum by the sum of those costs",
        "Cash is not a bubble and is not part of this percent"
      ]));
    } else {
      lines.push(para([
        "X is the change in market value today, in dollars",
        "Y is the percent price change today",
        "Bubble area follows market value"
      ]));
      lines.push(para([
        "Green is a gain",
        "Red is a loss",
        "Zero is neutral and sits on the origin"
      ]));
      lines.push(para([
        "The header adds the dollar change for positions that have one",
        "The percent divides that sum by the prior value of those positions"
      ]));
      if (header.cash) {
        lines.push(para([
          "Reported cash of " + plainMoney(header.cash) + " is included in that prior value, matching the account total",
          "Cash is not a bubble"
        ]));
      } else {
        lines.push(para([
          "No cash was reported, so the prior value uses only the included positions",
          "Cash is not a bubble"
        ]));
      }
      lines.push(para([
        "A static mutual fund with no day change is left out of this chart",
        "It can still appear on Gain/Loss when its cost is known"
      ]));
    }
    lines.push(para([
      "Each open position is its own bubble",
      "The same symbol in Schwab, Roth, Alpaca, and Trezor stays separate because the cost and the day change differ"
    ]));
    lines.push(included.length ? ("Included: " + included.join(", ") + ".") : "Nothing is included yet.");
    for (i = 0; i < header.excluded.length; i++) {
      item = header.excluded[i];
      if (item.reason === "no market value") missing.push(labelOf(item));
      else if (item.reason === "basis n/a") basis.push(labelOf(item));
      else dayMiss.push(labelOf(item));
    }
    if (basis.length) lines.push("Left out because the cost is unknown: " + basis.join(", ") + ".");
    if (dayMiss.length) lines.push("Left out because the day change is missing: " + dayMiss.join(", ") + ".");
    if (missing.length) lines.push("Left out because the market value is missing: " + missing.join(", ") + ".");
    if (!header.excluded.length) lines.push("Nothing is left out.");
    if (!gain && header.pl != null) {
      var book = accountDay(state);
      if (book != null && Math.abs(book - header.pl) > 0.05) {
        lines.push(para([
          "These bubbles sum to " + signedMoney(header.pl),
          "The Day total on this page is " + signedMoney(book),
          "This chart uses each position, so the two match when every book day figure is the sum of its positions"
        ]));
      }
    }
    return lines;
  }

  function formatET(ms) {
    var d = new Date(ms);
    if (isNaN(d.getTime())) return null;
    var fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: true
    });
    var parts = fmt.formatToParts(d), map = {}, i;
    for (i = 0; i < parts.length; i++) if (parts[i].type !== "literal") map[parts[i].type] = parts[i].value;
    return map.month + " " + map.day + ", " + map.hour + ":" + map.minute + " " + map.dayPeriod;
  }
  function newestMs(rows) {
    var best = null, i, ts;
    for (i = 0; i < rows.length; i++) {
      ts = rows[i].price_ts_ms;
      if (ts == null) continue;
      if (best == null || ts > best) best = ts;
    }
    return best;
  }
  function priceStatus(included, state) {
    var market = (state && state.schwab && state.schwab.market) || {};
    var note, i, age, label, stale = false;
    if (market.is_open === false) return "closed";
    note = state && state.schwab && state.schwab.prices_note;
    if (note) return "stale";
    for (i = 0; i < included.length; i++) {
      age = included[i].price_age_s;
      label = included[i].label || "";
      if (age != null && isFinite(age) && age > 120) stale = true;
      if (label === "delayed" || label === "no quote") stale = true;
    }
    return stale ? "stale" : "ok";
  }
  function updatedLine(included, state) {
    var ms = newestMs(included || []);
    var status = priceStatus(included || [], state);
    var line;
    if (ms == null) return "Updated time isn't available yet.";
    line = "Updated " + formatET(ms) + " ET";
    if (status === "stale") return line + ".  Prices are stale.";
    if (status === "closed") return line + ".  Market is closed.";
    return line;
  }

  function domain(values) {
    var lo = values[0], hi = values[0], i, span;
    for (i = 1; i < values.length; i++) {
      if (values[i] < lo) lo = values[i];
      if (values[i] > hi) hi = values[i];
    }
    if (lo === hi) { lo -= 1; hi += 1; }
    span = hi - lo;
    return { lo: lo - span * 0.08, hi: hi + span * 0.08 };
  }
  function tickMoney(v) {
    var sign = v < 0 ? "−" : v > 0 ? "+" : "";
    var a = Math.abs(v);
    if (a >= 10000) return sign + "$" + (a / 1000).toFixed(a >= 100000 ? 0 : 1) + "k";
    return sign + "$" + Math.round(a).toLocaleString("en-US");
  }
  function tickPct(v) {
    var sign = v < 0 ? "−" : v > 0 ? "+" : "";
    var a = Math.abs(v);
    return sign + (a >= 10 ? a.toFixed(0) : a.toFixed(1)) + "%";
  }
  function boxesOverlap(a, b) {
    return a.l < b.r + 6 && b.l < a.r + 6 && a.t < b.b + 4 && b.t < a.b + 4;
  }
  function cullTicks(ticks, axis) {
    var list = [], i, zero = null, rest = [], kept = [], t, ok, j;
    for (i = 0; i < ticks.length; i++) if (ticks[i].axis === axis) list.push(ticks[i]);
    for (i = 0; i < list.length; i++) {
      t = list[i];
      if (t.text === "$0" || t.text === "0%" || t.text === "0.0%" || t.text === "+0%") zero = t;
      else rest.push(t);
    }
    if (zero) kept.push(zero);
    rest.sort(function (a, b) {
      var origin = zero ? (axis === "x" ? zero.box.l : zero.box.t) : 0;
      var da = Math.abs((axis === "x" ? a.box.l : a.box.t) - origin);
      var db = Math.abs((axis === "x" ? b.box.l : b.box.t) - origin);
      return db - da;
    });
    for (i = 0; i < rest.length; i++) {
      ok = true;
      for (j = 0; j < kept.length; j++) if (boxesOverlap(rest[i].box, kept[j].box)) ok = false;
      if (ok) kept.push(rest[i]);
    }
    return kept;
  }
  function layout(points, width, height) {
    var m = { l: 64, r: 16, t: 10, b: 28 };
    var minR = 22, maxR = 56;
    var availW, availH, maxV, i, p, textW, half, pad, bubbles, xs, ys, xDom, yDom;
    var innerL, innerR, innerT, innerB, ticks, plot;
    width = Math.max(1, Number(width) || 1);
    height = Math.max(1, Number(height) || 1);
    availW = Math.max(0, width - m.l - m.r);
    availH = Math.max(0, height - m.t - m.b);
    while (maxR > minR && (availW < maxR * 2 + 8 || availH < maxR * 2 + 8)) maxR -= 1;
    maxV = 0;
    for (i = 0; i < points.length; i++) {
      if (Math.abs(points[i].value || 0) > maxV) maxV = Math.abs(points[i].value || 0);
    }
    bubbles = [];
    for (i = 0; i < points.length; i++) {
      p = points[i];
      textW = Math.max(String(p.symbol || "").length, String(p.tag || "").length) * 7.2 + 10;
      half = textW / 2;
      var r = radiusPx(p.value || 0, maxV || 1, minR, maxR);
      if (half <= maxR && r < half) r = half;
      bubbles.push({
        key: p.key, x: p.x, y: p.y, value: p.value, r: r,
        symbol: p.symbol, tag: p.tag, tone: p.tone
      });
    }
    pad = minR;
    for (i = 0; i < bubbles.length; i++) if (bubbles[i].r > pad) pad = bubbles[i].r;
    if (availW > 0 && pad > availW / 2) pad = availW / 2;
    if (availH > 0 && pad > availH / 2) pad = availH / 2;
    if (pad < 0) pad = 0;
    for (i = 0; i < bubbles.length; i++) if (pad > 0 && bubbles[i].r > pad) bubbles[i].r = pad;
    xs = [0];
    ys = [0];
    for (i = 0; i < bubbles.length; i++) { xs.push(bubbles[i].x); ys.push(bubbles[i].y); }
    xDom = domain(xs);
    yDom = domain(ys);
    innerL = m.l + pad;
    innerR = width - m.r - pad;
    innerT = m.t + pad;
    innerB = height - m.b - pad;
    if (innerR < innerL) { innerL = m.l; innerR = Math.max(innerL, width - m.r); }
    if (innerB < innerT) { innerT = m.t; innerB = Math.max(innerT, height - m.b); }
    function xPix(v) {
      var span = xDom.hi - xDom.lo || 1;
      return innerL + (innerR - innerL) * ((v - xDom.lo) / span);
    }
    function yPix(v) {
      var span = yDom.hi - yDom.lo || 1;
      return innerB - (innerB - innerT) * ((v - yDom.lo) / span);
    }
    for (i = 0; i < bubbles.length; i++) {
      bubbles[i].cx = xPix(bubbles[i].x);
      bubbles[i].cy = yPix(bubbles[i].y);
    }
    plot = { l: m.l, t: m.t, r: width - m.r, b: height - m.b };
    ticks = [];
    function addTick(v, axis) {
      var label = axis === "x" ? tickMoney(v) : tickPct(v);
      var tw = label.length * 7.2;
      var box, cx, cy;
      if (axis === "x") {
        cx = xPix(v);
        var left = cx - tw / 2;
        if (left < 2) left = 2;
        if (left + tw > width - 2) left = Math.max(2, width - 2 - tw);
        box = { l: left, t: height - 20, r: Math.min(width - 2, left + tw), b: height - 2 };
        ticks.push({ axis: axis, text: label, x: box.l, y: height - 6, box: box });
      } else {
        cy = yPix(v);
        var right = m.l - 8;
        var leftY = right - tw;
        if (leftY < 2) leftY = 2;
        box = { l: leftY, t: cy - 7, r: Math.min(m.l - 4, leftY + tw), b: cy + 5 };
        if (box.t < 2) { box.b += 2 - box.t; box.t = 2; }
        if (box.b > height - 2) { box.t -= box.b - (height - 2); box.b = height - 2; }
        ticks.push({ axis: axis, text: label, x: box.l, y: box.t + 11, box: box });
      }
    }
    var xTicks = uniqueTicks([0].concat(pointValues(bubbles, "x")), tickMoney);
    var yTicks = uniqueTicks([0].concat(pointValues(bubbles, "y")), tickPct);
    for (i = 0; i < xTicks.length; i++) addTick(xTicks[i], "x");
    for (i = 0; i < yTicks.length; i++) addTick(yTicks[i], "y");
    ticks = cullTicks(ticks, "x").concat(cullTicks(ticks, "y"));
    return {
      bubbles: bubbles,
      ticks: ticks,
      zero: { x: xPix(0), y: yPix(0) },
      plot: plot,
      width: width,
      height: height
    };
  }
  function pointValues(bubbles, field) {
    var i, out = [];
    for (i = 0; i < bubbles.length; i++) out.push(bubbles[i][field]);
    return out;
  }
  function uniqueTicks(values, fmt) {
    var seen = {}, out = [], i, label;
    for (i = 0; i < values.length; i++) {
      label = fmt(values[i]);
      if (seen[label]) continue;
      seen[label] = 1;
      out.push(values[i]);
    }
    return out;
  }

  function chartPoints(rows, mode) {
    var gain = mode !== "day";
    var included = gain ? gainHeader(rows).included : dayHeader(rows).included;
    var i, row, points = [];
    for (i = 0; i < included.length; i++) {
      row = included[i];
      points.push({
        key: row.key,
        x: gain ? row.unrealized_pl : row.day_pl,
        y: gain ? row.unrealized_plpc : row.day_plpc,
        value: Math.abs(row.value),
        symbol: row.symbol,
        tag: row.tag,
        tone: tone(gain ? row.unrealized_pl : row.day_pl),
        row: row
      });
    }
    points.sort(function (a, b) {
      if (a.symbol !== b.symbol) return a.symbol < b.symbol ? -1 : 1;
      if (a.tag !== b.tag) return a.tag < b.tag ? -1 : 1;
      return a.key < b.key ? -1 : 1;
    });
    return points;
  }

  function tipText(row, mode) {
    var gain = mode !== "day";
    var head = row.symbol + " · " + row.tag;
    if (row.account_name && row.source === "schwab") head += ", " + row.account_name;
    if (row.mask) head += ", ending " + row.mask;
    return para([
      head,
      (gain ? "Gain/loss " : "Today ") + signedMoney(gain ? row.unrealized_pl : row.day_pl) + ", " + signedPct(gain ? row.unrealized_plpc : row.day_plpc),
      "Market value " + plainMoney(row.value)
    ]);
  }

  function svgMarkup(box) {
    var h = '<svg class="bb-svg" viewBox="0 0 ' + box.width.toFixed(2) + " " + box.height.toFixed(2) + '" aria-hidden="true" focusable="false">';
    var i, t;
    h += '<rect class="bb-frame" x="' + box.plot.l + '" y="' + box.plot.t + '" width="' + Math.max(0, box.plot.r - box.plot.l) + '" height="' + Math.max(0, box.plot.b - box.plot.t) + '"></rect>';
    h += '<line class="bb-zero" x1="' + box.plot.l + '" y1="' + box.zero.y + '" x2="' + box.plot.r + '" y2="' + box.zero.y + '"></line>';
    h += '<line class="bb-zero" x1="' + box.zero.x + '" y1="' + box.plot.t + '" x2="' + box.zero.x + '" y2="' + box.plot.b + '"></line>';
    for (i = 0; i < box.ticks.length; i++) {
      t = box.ticks[i];
      h += '<text x="' + t.x.toFixed(1) + '" y="' + t.y.toFixed(1) + '">' + escText(t.text) + "</text>";
    }
    h += "</svg>";
    return h;
  }

  function cardHtml(state, width, height, mode, infoOpen, tipKey) {
    var rows = buildRows(state);
    var gain = mode !== "day";
    var active = gain ? "gain" : "day";
    var header = gain ? gainHeader(rows) : dayHeader(rows, state);
    var points = chartPoints(rows, active);
    var w = Math.max(260, Number(width) || 320);
    var hgt = Number(height) || PLOT_H;
    var box = layout(points, w, hgt);
    var included = header.included;
    var title = gain ? "Gain/Loss" : "Positions";
    var axisX = gain ? "Unrealized $" : "Today $";
    var axisY = gain ? "Unrealized %" : "Today %";
    var empty = gain ? EMPTY_GAIN : EMPTY_DAY;
    var updated = updatedLine(included, state);
    var info = infoParagraphs(active, rows, state);
    var tip = "";
    var i, b, row, pressed, html, paras = "";
    for (i = 0; i < included.length; i++) if (included[i].key === tipKey) tip = tipText(included[i], active);
    for (i = 0; i < info.length; i++) paras += "<p>" + escText(info[i]) + "</p>";
    html = '<div class="mv-card bb-card" data-bb-root="1">';
    html += '<div class="bb-top">';
    html += '<h2 class="bb-title" data-k="bb-title">' + escText(title) + "</h2>";
    html += '<div class="bb-toggle" role="group" aria-label="Bubble chart">';
    html += '<button type="button" class="bb-seg' + (gain ? " on" : "") + '" data-k="bb-mode-gain" data-bb="mode" data-mode="gain" aria-pressed="' + (gain ? "true" : "false") + '">Gain/Loss</button>';
    html += '<button type="button" class="bb-seg' + (!gain ? " on" : "") + '" data-k="bb-mode-day" data-bb="mode" data-mode="day" aria-pressed="' + (!gain ? "true" : "false") + '">Positions</button>';
    html += "</div>";
    html += '<button type="button" class="bb-info" data-k="bb-info-btn" data-bb="info" aria-expanded="' + (infoOpen ? "true" : "false") + '" aria-controls="bb-dialog">Info</button>';
    html += "</div>";
    html += '<div id="bb-dialog" class="bb-dialog' + (infoOpen ? " open" : "") + '" data-k="bb-info" role="dialog" aria-modal="false" aria-labelledby="bb-dialog-title">';
    html += '<div class="bb-dialog-bar"><h3 id="bb-dialog-title">About this chart</h3>';
    html += '<button type="button" class="bb-close" data-bb="close">Close</button></div>';
    html += paras + "</div>";
    if (header.empty) html += '<div class="mv-gain flat" data-k="bb-head">' + escText(empty) + "</div>";
    else html += '<div class="mv-gain ' + tone(header.pl) + '" data-k="bb-head">' + escText(signedMoney(header.pl)) + " <small>" + escText(signedPct(header.pct)) + "</small></div>";
    html += '<div class="bb-updated" data-k="bb-updated">' + escText(updated) + "</div>";
    html += '<div class="bb-axis-y" data-k="bb-axis-y">' + escText(axisY) + "</div>";
    html += '<div class="bb-plot" id="bbplot" data-k="bb-plot" style="height:' + hgt + 'px">';
    html += svgMarkup(box);
    if (!points.length) html += '<div class="bb-empty">' + escText(empty) + "</div>";
    for (i = 0; i < box.bubbles.length; i++) {
      b = box.bubbles[i];
      row = null;
      var j;
      for (j = 0; j < points.length; j++) if (points[j].key === b.key) row = points[j].row;
      if (!row) continue;
      pressed = tipKey && tipKey === row.key;
      html += '<button type="button" class="bb-dot ' + b.tone + (pressed ? " on" : "") + '" data-bb="bubble" data-id="' + escAttr(row.key) + '" data-symbol="' + escAttr(row.symbol) + '" data-source="' + escAttr(row.source) + '" data-account="' + escAttr(row.account_id) + '" data-tone="' + b.tone + '" aria-pressed="' + (pressed ? "true" : "false") + '" aria-label="' + escAttr(tipText(row, active)) + '" style="left:' + b.cx.toFixed(1) + "px;top:" + b.cy.toFixed(1) + "px;width:" + (b.r * 2).toFixed(1) + "px;height:" + (b.r * 2).toFixed(1) + "px;z-index:" + Math.max(1, Math.round(400 - b.r)) + '">';
      html += '<span class="bb-sym">' + escText(row.symbol) + '</span><span class="bb-tag">' + escText(row.tag) + "</span></button>";
    }
    html += "</div>";
    html += '<div class="bb-axis-x" data-k="bb-axis-x">' + escText(axisX) + "</div>";
    html += '<div class="bb-tip" data-k="bb-tip">' + (tip ? escText(tip) : "") + "</div>";
    html += "</div>";
    return html;
  }

  function plotWidthForViewport(viewport) {
    var page = viewport < 768 ? 28 : 40;
    var inner = Math.min(viewport, 1100) - page;
    return Math.max(260, inner - 38);
  }

  return {
    EMPTY_GAIN: EMPTY_GAIN,
    EMPTY_DAY: EMPTY_DAY,
    PLOT_H: PLOT_H,
    buildRows: buildRows,
    gainHeader: gainHeader,
    dayHeader: dayHeader,
    layout: layout,
    radiusPx: radiusPx,
    tone: tone,
    cardHtml: cardHtml,
    formatET: formatET,
    updatedLine: updatedLine,
    infoParagraphs: infoParagraphs,
    chartPoints: chartPoints,
    plotWidthForViewport: plotWidthForViewport,
    para: para
  };
});
