/* UI-4 trial copy of the Main tab and tab switcher. GET only.
   Behavior matches static/main.js. Colors and the hash URL are the trial differences:
   allocation swatches are CSS classes (Schwab / Alpaca / Bitcoin), the Alpaca
   line is Tesla blue, and the hash stays on /ui-trial when a <base> is set. */
(function () {
  var mv = document.getElementById("mainview"), desk = document.getElementById("deskview"),
      av = document.getElementById("assetsview"), tabs = document.querySelectorAll("#tabs button");
  var S = null, RANGE = "1D", chart = null, seriesCache = {};
  var LABEL = { "1D": "Day", "1W": "Week", "1M": "Month", "1A": "Year", "5A": "5Y" };
  var SPAN_DAYS = { "1W": 7, "1M": 30, "1A": 365, "5A": 1826 };
  function esc(t) { return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function ok(v) { return v != null && !isNaN(v); }
  function money(v, d) { return ok(v) ? "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d }) : "—"; }
  function smoney(v) { return ok(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + money(v) : "—"; }
  function spct(v) { return ok(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%" : ""; }
  function tone(v) { return !ok(v) ? "flat" : v > 0 ? "good" : v < 0 ? "bad" : "flat"; }
  function word(v) { return !ok(v) ? "" : v >= 0 ? "Up" : "Down"; }
  function day(ts) { return new Date(ts * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "America/New_York" }); }
  function etStamp(ms) { return ms ? new Date(ms).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET" : "?"; }

  function parts() {
    var t = S.totals || {}, c = S.crypto || {};
    return { schwab: t.schwab_total, alpaca: t.alpaca_equity, btc: c.value,
             total: [t.schwab_total, t.alpaca_equity, c.value].filter(ok).reduce(function (a, b) { return a + b; }, 0) };
  }

  /* Gain for the selected range. Only real numbers; anything missing is said out loud. */
  function gain(key) {
    var t = S.totals || {}, c = S.crypto || {}, p = parts();
    if (key === "1D") {
      var d = t.day || {}, pieces = [d.alpaca_pl, d.schwab_pl, c.day_pl].filter(ok);
      if (!pieces.length) return { na: "Today's change isn't available yet." };
      var pl = pieces.reduce(function (a, b) { return a + b; }, 0), start = p.total - pl;
      var miss = [];
      if (!ok(d.schwab_pl)) miss.push("Schwab");
      if (!ok(d.alpaca_pl)) miss.push("Alpaca");
      if (!ok(c.day_pl)) miss.push("Bitcoin");
      return { pl: pl, pct: start ? pl / start * 100 : null,
               note: "Stocks: since yesterday's close. Bitcoin: last 24 hours." + (miss.length ? " Missing: " + miss.join(", ") + "." : "") };
    }
    var r = (S.ranges || {})[key];
    if (!r || !ok(r.pl)) return { na: LABEL[key] + " history isn't available" + (r && r.error ? " (" + r.error + ")." : " yet."), disabled: true };
    var note = "Alpaca trading account only, not counting deposits. Schwab and Trezor history isn't available, so they're left out.";
    var limited = false;
    if (r.covers_from && (Date.now() / 1000 - r.covers_from) < (SPAN_DAYS[key] - 3) * 86400) {
      limited = true;
      note = "Only covers " + day(r.covers_from) + " to today (the Alpaca account's full history). " + note;
    }
    return { pl: r.pl, pct: r.plpc, note: note, limited: limited };
  }

  function holdings() {
    var rows = {}, sw = S.schwab, h = (S.desk || {}).holdings || {}, live = {};
    function add(sym, name, qty, px, dpc, val, cost) {
      var r = rows[sym] || (rows[sym] = { sym: sym, name: name, qty: 0, px: px, dpc: dpc, val: 0, cost: 0, noBasis: false });
      if (name && !r.name) r.name = name;
      qty = Number(qty); cost = Number(cost);   // feeds may send numeric strings
      r.qty += ok(qty) ? qty : 0; r.val += Number(val) || 0;
      if (ok(qty) && qty < 0) r.noBasis = true;                          // short: basis n/a, not in unrealized
      else if (ok(cost) && cost > 0) r.cost += cost; else r.noBasis = true;   // never invent a basis
      if (ok(px)) r.px = px; if (ok(dpc)) r.dpc = dpc;
    }
    if (sw && sw.ok) {
      (sw.accounts || []).forEach(function (a) { (a.positions || []).forEach(function (p) {
        live[p.symbol] = 1;
        add(p.symbol, p.name, p.qty, p.price, p.day_plpc, p.value, p.cost_basis); }); });
    } else {
      (h.accounts || []).forEach(function (a) { (a.positions || []).forEach(function (p) {
        var dpc = ok(p.prev_close) && p.prev_close ? (p.price / p.prev_close - 1) * 100 : null;
        add(p.symbol, p.name, p.qty, p.price, dpc, p.value, p.cost_basis); }); });
    }
    (S.positions || []).forEach(function (p) { add(p.symbol, null, p.qty, p.price, p.day_change_pct, p.market_value,
      ok(p.avg_entry) && p.avg_entry > 0 ? Math.abs(p.qty * p.avg_entry) : null); });
    var hp = S.ha_prices || {};   // HA keyless price sensors win when present, except live Schwab rows
    Object.keys(rows).forEach(function (k) { if (live[k]) return; var x = hp[k], r = rows[k];
      if (x && ok(x.price)) { if (r.qty) r.val = r.qty * x.price; r.px = x.price; if (ok(x.prev_close) && x.prev_close) r.dpc = (x.price / x.prev_close - 1) * 100; } });
    Object.keys(rows).forEach(function (k) { var r = rows[k];
      if (!r.noBasis && r.cost > 0) { r.unrl = r.val - r.cost; r.unrlPct = r.unrl / r.cost * 100; } });
    var out = Object.keys(rows).map(function (k) { return rows[k]; }).sort(function (a, b) { return b.val - a.val; });
    var c = S.crypto || {};
    if (ok(c.value)) out.push({ sym: "BTC", name: "Bitcoin (Trezor)", qty: c.btc_qty, px: c.btc_price, dpc: c.day_plpc, val: c.value, btc: true, noBasis: true });
    return out;
  }

  function unrealized(rows) {
    var pl = 0, cost = 0, n = 0, ex = [];
    rows.forEach(function (r) {
      if (ok(r.unrl)) { pl += r.unrl; cost += r.cost; n++; }
      else ex.push(r.btc ? "Bitcoin (Trezor)" : r.sym);
    });
    return n ? { pl: pl, pct: cost ? pl / cost * 100 : null, ex: ex } : { na: true, ex: ex };
  }

  var lastStamp = "", lastMainHtml = "";
  function render() {
    if (!S) { mv.innerHTML = '<div class="mv-card mv-empty">Loading…</div>'; return; }
    var p = parts(), g = gain(RANGE), c = S.crypto || {}, h = "", rows = holdings(), u = unrealized(rows);
    var stamp = RANGE + "|" + rows.map(function (r) { return r.sym; }).join(",") + "|" + (g.na ? "na" : "y");
    h += '<div class="mv-card"><h2>Gain or loss on what you hold now (not yet sold)</h2>' +
      (u.na ? '<div class="mv-gain flat" data-k="held">Not available</div>' :
        '<div class="mv-gain ' + tone(u.pl) + '" data-k="held">' + word(u.pl) + " " + smoney(u.pl).replace(/^[+−]/, "") + " <small>" + spct(u.pct) + "</small></div>") +
      '<div class="mv-asof" data-k="heldnote">Schwab + Alpaca, current value vs. what you paid.' +
      (u.ex.length ? " Left out because the purchase price isn't known (basis n/a): " + esc(u.ex.join(", ")) + "." : "") + "</div></div>";
    h += '<div class="mv-card"><h2>Everything you own</h2><div class="mv-total" data-k="total">' + money(p.total) + "</div>" +
      '<div class="mv-asof" data-k="asof">Schwab + Alpaca + Bitcoin on your Trezor · updated ' + esc(etStamp(S.ts_ms)) + "</div>";
    var tot = p.total || 1, seg = [["Schwab", p.schwab, "schwab"], ["Alpaca", p.alpaca, "alpaca"], ["Bitcoin", p.btc, "bitcoin"]];
    h += '<div class="mv-bar" role="img" aria-label="Allocation: Schwab, Alpaca, and Bitcoin">' + seg.map(function (s) { return ok(s[1]) ? '<i class="seg ' + s[2] + '" data-k="seg-' + s[2] + '" style="width:' + (s[1] / tot * 100).toFixed(2) + '%"></i>' : ""; }).join("") + "</div>";
    h += '<div class="mv-split">' + seg.map(function (s) {
      return '<div><span><i class="sw ' + s[2] + '" aria-hidden="true"></i>' + s[0] + '</span><b data-k="part-' + s[2] + '">' + money(s[1]) + "</b></div>"; }).join("") + "</div>";
    if (!ok(c.value)) h += '<div class="mv-note">Bitcoin isn\'t counted yet: ' + esc(c.qty_error || c.price_error || "waiting for the BTC price") + ".</div>";
    h += "</div>";

    h += '<div class="mv-card"><h2>How much you\'ve gained or lost</h2><div class="mv-ranges" role="group" aria-label="Gain or loss range">' +
      Object.keys(LABEL).map(function (k) { var gk = gain(k);
        return '<button type="button" data-k="rng-' + k + '" data-r="' + k + '" class="' + (k === RANGE ? "on" : "") + (gk.limited ? " limited" : "") + (gk.disabled ? " na" : "") + '" aria-pressed="' + (k === RANGE ? "true" : "false") + '"' + (gk.disabled ? ' disabled aria-disabled="true" title="' + esc(gk.na) + '"' : "") + ">" + LABEL[k] + "</button>"; }).join("") + "</div>";
    if (g.na) h += '<div class="mv-gain flat" data-k="gain">Not available</div><div class="mv-note" data-k="gnote">' + esc(g.na) + "</div>";
    else h += '<div class="mv-gain ' + tone(g.pl) + '" data-k="gain">' + word(g.pl) + " " + smoney(g.pl).replace(/^[+−]/, "") + " <small>" + spct(g.pct) + "</small></div>" +
      '<div class="mv-note" data-k="gnote">' + esc(g.note) + "</div>";
    h += '<h2 style="margin-top:16px">' + (RANGE === "1D" ? "Alpaca account today" : "Alpaca account value, " + LABEL[RANGE].toLowerCase()) + '</h2><div id="mvchart" class="mv-chart"></div><div class="mv-key"><i class="sw alpaca" aria-hidden="true"></i> Blue line: Alpaca equity</div></div>';

    h += '<div class="mv-card"><h2>Your stocks and Bitcoin</h2>';
    if (!rows.length) h += '<div class="mv-empty">No holdings reported yet.</div>';
    else h += "<table><thead><tr><th>Name</th><th>Price</th><th>Today</th><th class=\"hide-sm\">Shares</th><th>Value</th><th>Gain/loss</th></tr></thead><tbody>" +
      rows.map(function (r) { return "<tr><td><b>" + esc(r.sym) + '</b><span class="nm">' + esc(r.name || "") + "</span></td><td data-k=\"px-" + esc(r.sym) + "\">" + money(r.px) +
        '</td><td data-k="day-' + esc(r.sym) + '" class="' + tone(r.dpc) + '">' + (ok(r.dpc) ? spct(r.dpc) : "—") + '</td><td class="hide-sm">' +
        (ok(r.qty) ? Number(r.qty).toLocaleString("en-US", { maximumFractionDigits: r.btc ? 5 : 3 }) : "—") + '</td><td data-k="val-' + esc(r.sym) + '">' + money(r.val, 0) + '</td><td data-k="un-' + esc(r.sym) + '" class="' + (ok(r.unrl) ? tone(r.unrl) : "flat") + '">' +
        (ok(r.unrl) ? smoney(r.unrl).replace(/\.\d\d$/, "") + '<span class="nm">' + spct(r.unrlPct) + "</span>" : '<span class="nm" style="display:inline">basis n/a</span>') + "</td></tr>"; }).join("") + "</tbody></table>";
    var priceNote = (S.schwab && S.schwab.ok)
      ? "Stock prices: Alpaca live trades. Funds without an Alpaca quote use their snapshot price. "
      : "Stock prices: Alpaca live quotes (IEX). Funds without a live quote use the morning Schwab snapshot. ";
    h += '<div class="mv-asof" data-k="src" style="margin-top:10px">' + priceNote + "Bitcoin: " +
      esc(c.price_source || "—") + (c.price_as_of_ms ? " at " + esc(etStamp(c.price_as_of_ms)) : "") + ". Change the Trezor amount in Home Assistant: <b>Trezor Bitcoin</b> helper.</div></div>";
    if (h === lastMainHtml) { drawChart(false); return; }
    if (stamp === lastStamp && mv.querySelector("#mvchart") && patchMain(h)) {
      lastMainHtml = h;
      drawChart(false);
      return;
    }
    lastStamp = stamp;
    lastMainHtml = h;
    if (chart) { try { chart.destroy(); } catch (x) { /* ignore */ } chart = null; }
    var apply = function () { mv.innerHTML = h; };
    if (window.TDGuard) window.TDGuard(apply); else apply();
    mv.querySelectorAll(".mv-ranges button").forEach(function (b) { b.onclick = function () { RANGE = b.dataset.r; render(); }; });
    drawChart(false);
  }
  function patchMain(html) {
    var box = document.createElement("div");
    box.innerHTML = html;
    var cur = mv.querySelectorAll("[data-k]"), nxt = box.querySelectorAll("[data-k]");
    if (cur.length !== nxt.length) return false;
    var i;
    for (i = 0; i < cur.length; i++) if (cur[i].getAttribute("data-k") !== nxt[i].getAttribute("data-k")) return false;
    for (i = 0; i < cur.length; i++) {
      if (cur[i].className !== nxt[i].className) cur[i].className = nxt[i].className;
      if (cur[i].getAttribute("style") !== nxt[i].getAttribute("style")) {
        if (nxt[i].getAttribute("style")) cur[i].setAttribute("style", nxt[i].getAttribute("style"));
      }
      if (cur[i].innerHTML !== nxt[i].innerHTML) cur[i].innerHTML = nxt[i].innerHTML;
    }
    return true;
  }

  function drawChart(fromResize) {
    var el = document.getElementById("mvchart"); if (!el || !window.uPlot) return;
    function paint(t, e) {
      if (!t || t.length < 2) {
        if (!el.querySelector(".mv-empty")) el.textContent = "No chart data for this range yet.";
        return;
      }
      if (chart && chart._range === RANGE) {
        chart.setData([t, e]);
        if (fromResize) chart.setSize({ width: el.clientWidth || 600, height: 240 });
        return;
      }
      if (chart) { try { chart.destroy(); } catch (x) { /* ignore */ } chart = null; }
      el.innerHTML = "";
      var w = el.clientWidth || 600;
      chart = new uPlot({ width: w, height: 240, legend: { show: false }, cursor: { y: false },
        scales: { x: { time: true } },
        axes: [{ stroke: "#b4b6bb", grid: { stroke: "#2e2e32" }, ticks: { stroke: "#2e2e32" } },
               { stroke: "#b4b6bb", grid: { stroke: "#2e2e32" }, ticks: { stroke: "#2e2e32" }, size: 70, values: function (u, v) { return v.map(function (x) { return "$" + Math.round(x).toLocaleString(); }); } }],
        series: [{}, { stroke: "#3e6ae1", width: 2.5, fill: "rgba(62,106,225,.16)" }] }, [t, e], el);
      chart._range = RANGE;
    }
    if (RANGE === "1D") { var cv = S.curve || {}; paint(cv.t, cv.equity); return; }
    var cached = seriesCache[RANGE];
    if (cached && Date.now() - cached.at < 120000) { paint(cached.d.t, cached.d.equity); return; }
    fetch("api/history?range=" + RANGE, { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
      seriesCache[RANGE] = { at: Date.now(), d: d }; if (document.getElementById("mvchart") === el) paint(d.t, d.equity);
    }).catch(function () { if (!el.querySelector(".mv-empty")) el.textContent = "Chart unavailable."; });
  }

  var lastMain = 0, mainTimer = null, mainPending = false;
  function mainVisible() { return !mv.classList.contains("hidden"); }
  function runMain() {
    if (!mainVisible() || !S) return;
    lastMain = Date.now();
    if (window.TDRenderCounts) window.TDRenderCounts.main++;
    render();
  }
  window.TDRenderMain = function (force) {
    if (window.TDShared) S = window.TDShared;
    if (!S || !mainVisible()) return;
    if (document.hidden && !force) return;
    if (force) {
      if (mainTimer) { clearTimeout(mainTimer); mainTimer = null; }
      mainPending = false;
      runMain();
      return;
    }
    if (mainPending) return;
    var wait = Math.max(0, 1000 - (Date.now() - lastMain));
    mainPending = true;
    mainTimer = setTimeout(function () {
      mainTimer = null;
      mainPending = false;
      requestAnimationFrame(function () { if (window.TDShared) S = window.TDShared; runMain(); });
    }, wait);
  };

  var current = null;
  var savedY = { main: 0, desk: 0, assets: 0 };
  var seenTab = {};
  var pinScroll = 0;
  function writeHash(tab) {
    var hash = tab === "desk" ? "#detail" : (tab === "assets" ? "#assets" : "#");
    try { history.replaceState(null, "", (location.pathname || "/") + (location.search || "") + hash); }
    catch (e) { /* ignore */ }
  }
  function show(tab, fromClick) {
    // A click focuses the tab button and WebKit scrolls that button to the top
    // before the click event. pointerdown already stored the real scrollY.
    if (current && tab !== current && (Date.now() - pinScroll > 700))
      savedY[current] = window.scrollY || window.pageYOffset || 0;
    var first = !seenTab[tab];
    seenTab[tab] = true;
    current = tab;
    document.body.classList.toggle("slate", tab === "main");
    mv.classList.toggle("hidden", tab !== "main");
    desk.classList.toggle("hidden", tab !== "desk");
    av.classList.toggle("hidden", tab !== "assets");
    tabs.forEach(function (b) {
      var on = b.dataset.tab === tab;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (window.TDAssets) {
      if (tab === "assets") window.TDAssets.start();
      else if (window.TDAssets.stop) window.TDAssets.stop();
    }
    if (tab === "desk" && window.TDRenderDetail) window.TDRenderDetail();
    if (tab === "main" && window.TDRenderMain) window.TDRenderMain(true);
    var y = first ? 0 : (savedY[tab] || 0);
    // Focus can scroll the tab button into view after the click. Put the saved
    // position back once the focus scroll has happened.
    function put() { if (window.TDRestoreScroll) window.TDRestoreScroll(y); }
    requestAnimationFrame(put);
    setTimeout(put, 60);
    setTimeout(put, 180);
    if (fromClick) writeHash(tab);
    setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 0);
  }
  tabs.forEach(function (b) {
    b.addEventListener("pointerdown", function () {
      if (!current) return;
      savedY[current] = window.scrollY || window.pageYOffset || 0;
      pinScroll = Date.now();
    });
    b.addEventListener("click", function () { show(b.dataset.tab, true); });
  });
  window.addEventListener("resize", function () { if (!mv.classList.contains("hidden") && chart) chart.setSize({ width: (document.getElementById("mvchart") || {}).clientWidth || 600, height: 240 }); });
  if (!window.TDShared) {
    fetch("api/state", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!window.TDShared && window.TDApplyState) window.TDApplyState(d);
    }).catch(function () { if (mainVisible()) mv.innerHTML = '<div class="mv-card mv-empty">Can\'t reach the Trading Desk right now.</div>'; });
  }
  var hsh = location.hash.replace("#", "");
  show(hsh === "assets" ? "assets" : (hsh === "detail" || hsh === "desk") ? "desk" : "main", false);
})();
