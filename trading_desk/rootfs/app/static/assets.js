/* Schwab tab. Reads S.schwab from the shared SSE state. Read-only.
   If that object is missing, fall back once to the legacy api/assets snapshot. */
(function () {
  "use strict";
  var view = document.getElementById("assetsview");
  var legacy = null, legacyTried = false, lastHtml = "";
  var lastRun = 0, timer = null, pending = false;

  function esc(t) {
    return String(t == null ? "" : t).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function ok(v) { return v != null && !isNaN(v) && isFinite(v); }
  function money(v) {
    if (!ok(v)) return "—";
    return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function signed(v) {
    if (!ok(v)) return "—";
    var body = "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (v > 0) return "+" + body;
    if (v < 0) return "−" + body;
    return body;
  }
  function pct(v) {
    if (!ok(v)) return "";
    return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%";
  }
  function shares(v) {
    if (!ok(v)) return "—";
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  }
  function tone(v) { return !ok(v) || v === 0 ? "sw-flat" : v > 0 ? "sw-up" : "sw-dn"; }
  function labelClass(label) {
    if (!label || label === "no quote") return "sw-none";
    if (label === "live") return "sw-live";
    if (String(label).indexOf("snapshot") === 0) return "sw-snap";
    return "sw-muted";
  }
  function td(label, inner) {
    return '<td data-l="' + esc(label) + '">' + inner + "</td>";
  }
  function when(iso, opts) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d)) return "";
    opts.timeZone = "America/New_York";
    return d.toLocaleString("en-US", opts);
  }
  function dateET(iso) {
    var d = when(iso, { month: "short", day: "numeric", year: "numeric" });
    return d ? d + " ET" : "";
  }
  function openET(iso) {
    if (!iso) return "later ET";
    var day = when(iso, { weekday: "short" });
    var time = when(iso, { hour: "numeric", minute: "2-digit" });
    return (day && time) ? day + " " + time + " ET" : "later ET";
  }
  function write(html) {
    if (!view || html === lastHtml) return;
    lastHtml = html;
    var apply = function () { view.innerHTML = html; };
    if (window.TDGuard) window.TDGuard(apply); else apply();
  }
  function figures(roll) {
    var day = !ok(roll.day_pl) ? "—" : '<span class="' + tone(roll.day_pl) + '">' + signed(roll.day_pl) + " " + pct(roll.day_plpc) + "</span>";
    var un = !ok(roll.unrealized_pl) ? "basis n/a" : '<span class="' + tone(roll.unrealized_pl) + '">' + signed(roll.unrealized_pl) + " " + pct(roll.unrealized_plpc) + "</span>";
    return '<div class="sw-fig"><span>Total</span><b class="sw-total">' + money(roll.total) + "</b></div>" +
      '<div class="sw-fig"><span>Today</span><b>' + day + "</b></div>" +
      '<div class="sw-fig"><span>Unrealized</span><b>' + un + "</b></div>";
  }
  function exclusions(roll) {
    var bits = [];
    if (roll.missing && roll.missing.length) bits.push("No price, left out of the total: " + roll.missing.map(esc).join(", "));
    if (roll.day_excluded && roll.day_excluded.length) bits.push("Left out of today's change: " + roll.day_excluded.map(esc).join(", "));
    if (roll.basis_excluded && roll.basis_excluded.length) bits.push("Basis n/a: " + roll.basis_excluded.map(esc).join(", "));
    return bits.length ? '<p class="sw-ex">' + bits.join(". ") + ".</p>" : "";
  }
  function rows(acct) {
    var body = (acct.positions || []).map(function (p) {
      var px = !ok(p.price) ? "—" : money(p.price);
      var today = !ok(p.day_pl) ? "—" : '<span class="' + tone(p.day_pl) + '">' + signed(p.day_pl) + " " + pct(p.day_plpc) + "</span>";
      var gain = !ok(p.unrealized_pl) ? "basis n/a" : '<span class="' + tone(p.unrealized_pl) + '">' + signed(p.unrealized_pl) + " " + pct(p.unrealized_plpc) + "</span>";
      return "<tr>" + td("Symbol", "<b>" + esc(p.symbol) + "</b>" + (p.name ? '<span class="sw-nm">' + esc(p.name) + "</span>" : "")) +
        td("Shares", shares(p.qty)) +
        td("Price", px + '<span class="sw-age ' + labelClass(p.label) + '">' + esc(p.label || "no quote") + "</span>") +
        td("Today", today) + td("Value", ok(p.value) ? money(p.value) : "—") + td("Gain/loss", gain) + "</tr>";
    }).join("");
    body += "<tr>" + td("Symbol", "Cash") + td("Shares", "") + td("Price", "") + td("Today", "") +
      td("Value", money(acct.cash)) + td("Gain/loss", "") + "</tr>";
    return '<table class="sw-pos"><thead><tr><th>Symbol</th><th>Shares</th><th>Price</th><th>Today</th><th>Value</th><th>Gain/loss</th></tr></thead><tbody>' +
      body + "</tbody></table>";
  }
  function renderSchwab(sw) {
    var m = sw.market || {}, c = sw.combined || {};
    var pill = "";
    if (m.is_open === true) pill = '<span class="sw-pill">Market open</span>';
    else if (m.is_open === false) pill = '<span class="sw-pill">Market closed · opens ' + esc(openET(m.next_open)) + "</span>";
    var html = "";
    if (sw.prices_note) html += '<p class="sw-warn">' + esc(sw.prices_note) + "</p>";
    html += '<section class="sw-card"><div class="sw-hd"><h2><i class="sw-mark" aria-hidden="true"></i>Combined</h2>' + pill + "</div>" +
      figures(c) + exclusions(c) + "</section>";
    (sw.accounts || []).forEach(function (a) {
      var name = esc(a.name || "Account") + (a.mask ? " ···" + esc(a.mask) : "");
      html += '<section class="sw-card"><div class="sw-hd"><h2>' + name + "</h2></div>" + figures(a) + exclusions(a) + rows(a) + "</section>";
    });
    html += '<p class="sw-note">Share counts: holdings file as of ' + esc(dateET(sw.as_of) || "—") +
      ". Prices: Alpaca " + esc(sw.feed || "iex") + " live trades. Funds without an Alpaca quote use their snapshot price. Read-only.</p>";
    write(html);
  }

  var M = function (x, s) { return (s && x > 0 ? "+" : x < 0 ? "-" : "") + "$" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
  var P = function (x) { return "(" + (x > 0 ? "+" : x < 0 ? "-" : "") + Math.abs(x).toFixed(2) + "%)"; };
  var C = function (x) { return x > 0 ? "as-up" : x < 0 ? "as-dn" : "as-mu"; };
  var N = function (x, d) { return Number(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); };
  function renderLegacy(r) {
    if (!r || !r.ok) {
      write('<div class="as-empty">No assets snapshot yet. ' + esc((r && r.error) || "") +
        '<br>Share counts go in <b>/share/trading_desk/schwab_holdings.json</b>.' +
        '<br>Feed one to <b>/share/trading_desk/assets.json</b> (or via the Life HQ feed).</div>');
      return;
    }
    var a = r.assets, t = a.equities_total || {}, c = a.cash || {};
    var d = new Date(a.as_of);
    var ds = isNaN(d) ? "?" : d.toLocaleDateString("en-US", { month: "numeric", day: "numeric", year: "numeric", timeZone: "America/New_York" }) +
      " " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET";
    var h = '<div class="as-hd"><h1>' + esc(a.account || "Account") + '<span class="as-pill ' + (a.stale ? "stale" : "fresh") + '">' + (a.stale ? "STALE" : "fresh") +
      '</span></h1><small>as of ' + esc(ds) + "</small></div>";
    h += '<div class="as-sum"><div class="as-row"><span class="l">Account Value</span><span class="as-big">' + M(a.account_value) + "</span></div>" +
      '<div class="as-row"><span class="l">Day Change</span><span class="as-v ' + C(a.day_change) + '">' + M(a.day_change, 1) + " " + P(a.day_change_pct) + "</span></div>" +
      '<div class="as-row"><span class="l">Unrealized Gain/Loss</span><span class="as-v ' + C(a.unrealized) + '">' + M(a.unrealized, 1) + " " + P(a.unrealized_pct) + "</span></div></div>";
    h += '<table class="as-pos"><thead><tr><th>Symbol</th><th>Qty<br>Price<br>Chg</th><th>Mkt Val<br>Day Chg<br>Gain/Loss</th></tr></thead><tbody>' +
      '<tr class="sec"><td>Equities</td><td colspan="2">' + esc(t.alloc_pct) + "%</td></tr>";
    (a.positions || []).forEach(function (x) {
      h += '<tr><td><div class="s">' + esc(x.symbol) + '</div><div class="n">' + esc(x.name) + "</div></td><td><div>" + N(x.qty, 4) + "</div><div>$" + N(x.price, 2) +
        '</div><div class="' + C(x.price_chg) + '">' + (x.price_chg > 0 ? "+" : "-") + "$" + Math.abs(x.price_chg).toFixed(2) + "</div></td><td><div>" + M(x.mkt_val) +
        '</div><div class="' + C(x.day_chg) + '">' + M(x.day_chg, 1) + " " + P(x.day_pct) + '</div><div class="' + C(x.gain) + '">' + M(x.gain, 1) + " " + P(x.gain_pct) + "</div></td></tr>";
    });
    h += '<tr class="tot"><td><div class="s" style="font-weight:600">Equities Total</div></td><td colspan="2"><div>' + M(t.mkt_val) + '</div><div class="' + C(t.day_chg) + '">' +
      M(t.day_chg, 1) + " " + P(t.day_pct) + '</div><div class="' + C(t.gain) + '">' + M(t.gain, 1) + " " + P(t.gain_pct) + "</div></td></tr>" +
      '<tr class="sec"><td>Cash &amp; Money Market</td><td colspan="2">' + esc(c.alloc_pct) + "%</td></tr><tr><td>Cash &amp; Cash Investments</td><td colspan=\"2\">" + M(c.value) + "</td></tr></tbody></table>";
    h += '<div class="as-note">Manual snapshot (' + esc(a.source || "fed file") + "). No brokerage login. Read-only, behind the HA login.</div>";
    write(h);
  }
  function ensureLegacy(cb) {
    if (legacyTried) { cb(legacy); return; }
    legacyTried = true;
    fetch("api/assets", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
      legacy = d; cb(d);
    }).catch(function () {
      legacy = { ok: false, error: "Could not load the assets snapshot." };
      cb(legacy);
    });
  }
  function renderNow() {
    if (!visible()) return;
    lastRun = Date.now();
    if (window.TDRenderCounts) window.TDRenderCounts.assets++;
    var S = window.TDShared, sw = S && S.schwab;
    if (sw && sw.ok) { renderSchwab(sw); return; }
    ensureLegacy(function (r) {
      if (!visible()) return;
      if (r && r.ok) renderLegacy(r);
      else renderLegacy({ ok: false, error: ((sw && sw.error) ? sw.error + ". " : "") + ((r && r.error) || "") });
    });
  }
  function visible() { return !!(view && !view.classList.contains("hidden")); }
  function schedule(force) {
    if (!visible()) return;
    if (document.hidden && !force) return;
    if (force) {
      if (timer) { clearTimeout(timer); timer = null; }
      pending = false;
      renderNow();
      return;
    }
    if (pending) return;
    var wait = Math.max(0, 1000 - (Date.now() - lastRun));
    pending = true;
    timer = setTimeout(function () {
      timer = null;
      pending = false;
      requestAnimationFrame(function () { renderNow(); });
    }, wait);
  }
  window.TDAssets = {
    start: function () { schedule(true); },
    stop: function () { if (timer) { clearTimeout(timer); timer = null; } pending = false; },
    onState: function () { schedule(false); },
    render: function () { schedule(true); }
  };
})();
