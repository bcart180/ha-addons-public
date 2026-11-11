/* Trading Desk UI: vanilla JS, read-only. Receives state over SSE (GET). */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var S = null, curvePlot = null, pricePlot = null, selSym = null, lastMsg = 0, es = null;
  var seenHandoffs = {};
  var STAGE_BOT = { scan: "Strategist", vet: "Research Analyst", size: "Risk Manager",
    risk: "Desk Lead", fill: "Execution Trader", review: "Trade Reviewer" };
  var DESKS = {
    "Desk Lead": [360, 150], "Strategist": [90, 60], "Research Analyst": [270, 50],
    "Risk Manager": [450, 50], "Execution Trader": [630, 60], "Trade Reviewer": [630, 240],
    "Trading Floor": [90, 240], "Book": [360, 262]
  };

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function money(v, d) { if (v == null || isNaN(v)) return "—";
    return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d }); }
  // Shared signed-number helper: every P&L figure goes through signed(), so it is coloured
  // pos (green) / neg (red) / zero (neutral) by CSS class. Use only for P&L, never prices/shares/cash.
  function plClass(v) { return v == null || isNaN(v) ? "pl na" : v > 0 ? "pl pos" : v < 0 ? "pl neg" : "pl zero"; }
  function signed(v, text) { return '<span class="' + plClass(v) + '">' + (v == null || isNaN(v) ? "—" : text) + "</span>"; }
  function smoney(v) { return signed(v, (v > 0 ? "+" : "") + money(v)); }
  function pct(v, d) { return signed(v, (v > 0 ? "+" : "") + (v == null ? 0 : Number(v)).toFixed(d == null ? 2 : d) + "%"); }
  function cls(v) { return v > 0 ? "up" : v < 0 ? "dn" : ""; }
  function num(v, d) { if (v == null || isNaN(v)) return "—"; return Number(v).toLocaleString("en-US", { maximumFractionDigits: d == null ? 4 : d }); }
  function etTime(iso, withDate) {
    if (!iso) return "";
    var d = typeof iso === "number" ? new Date(iso) : new Date(iso);
    if (isNaN(d)) return "";
    var o = { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" };
    if (withDate) { o.month = "short"; o.day = "numeric"; }
    return d.toLocaleString("en-US", o) + " ET";
  }
  function ago(ms) { if (!ms) return "never"; var s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return Math.round(s) + "s"; if (s < 3600) return Math.round(s / 60) + "m"; return Math.round(s / 3600) + "h"; }

  // Skip identical DOM writes. Live cells update in place. The scroll guard only
  // restores a jump that the user did not make (F4).
  var lastHtml = {};
  var scrollLock = 0;
  var lastUserScroll = 0;
  window.addEventListener("scroll", function () {
    if (scrollLock) return;
    lastUserScroll = Date.now();
  }, { passive: true });
  function restoreScrollY(y) {
    scrollLock++;
    try { window.scrollTo(0, y); }
    finally { setTimeout(function () { scrollLock--; }, 80); }
  }
  window.TDRestoreScroll = restoreScrollY;
  function guardHeight(fn) {
    var se = document.scrollingElement || document.documentElement;
    var y = window.scrollY || window.pageYOffset || 0;
    var h = se.scrollHeight || 0;
    var view = window.innerHeight || 0;
    var atBottom = (y + view) >= (h - 2);
    fn();
    var ny = window.scrollY || window.pageYOffset || 0;
    if (!atBottom && ny !== y && (Date.now() - lastUserScroll) > 250) restoreScrollY(y);
  }
  window.TDGuard = guardHeight;
  function setHTML(el, key, html) {
    if (!el || lastHtml[key] === html) return false;
    lastHtml[key] = html;
    guardHeight(function () { el.innerHTML = html; });
    return true;
  }
  function setText(el, key, text) {
    if (!el || lastHtml[key] === text) return;
    lastHtml[key] = text;
    el.textContent = text;
  }
  function fillTbody(tableId, html) {
    var table = $(tableId);
    if (!table || !table.tBodies[0]) return;
    var key = "tb:" + tableId;
    if (lastHtml[key] === html) return;
    var tb = table.tBodies[0];
    var prev = lastHtml[key];
    lastHtml[key] = html;
    if (prev != null && tb.children.length) {
      var probe = document.createElement("tbody");
      probe.innerHTML = html;
      if (probe.children.length === tb.children.length) {
        var same = true, i, c;
        for (i = 0; i < tb.children.length; i++) {
          if (tb.children[i].children.length !== probe.children[i].children.length) { same = false; break; }
        }
        if (same) {
          guardHeight(function () {
            for (i = 0; i < tb.children.length; i++) {
              var a = tb.children[i], b = probe.children[i];
              if (a.className !== b.className) a.className = b.className;
              var ds = b.getAttribute("data-s");
              if ((a.getAttribute("data-s") || "") !== (ds || "")) {
                if (ds) a.setAttribute("data-s", ds); else a.removeAttribute("data-s");
              }
              for (c = 0; c < a.children.length; c++) {
                if (a.children[c].className !== b.children[c].className) a.children[c].className = b.children[c].className;
                if (a.children[c].innerHTML !== b.children[c].innerHTML) a.children[c].innerHTML = b.children[c].innerHTML;
              }
            }
          });
          return;
        }
      }
    }
    guardHeight(function () { tb.innerHTML = html; });
  }
  function td(label, inner, cls) {
    return '<td data-l="' + esc(label) + '"' + (cls ? ' class="' + cls + '"' : "") + ">" + inner + "</td>";
  }

  // ---------------------------------------------------------------- health
  function renderHealth() {
    var h = S.health, out = [];
    function item(label, state, lastMs, err) {
      var age = lastMs ? (Date.now() - lastMs) : null;
      var c = state === "streaming" ? (age != null && age > 120000 ? "mid" : "ok")
        : state === "disabled" ? "" : (state === "connecting" ? "mid" : "bad");
      out.push('<span class="hp" title="' + esc(err || "") + '"><i class="dot ' + c + '"></i>' + label + " " +
        esc(state) + (lastMs ? " · " + ago(lastMs) : "") + "</span>");
    }
    item("Trades", h.trade_stream.state, h.trade_stream.last_msg_ms, h.trade_stream.error);
    item("Data/" + (h.data_stream.feed || "").toUpperCase(), h.data_stream.state, h.data_stream.last_msg_ms, h.data_stream.error);
    var r = h.rest, rc = r.error ? "bad" : (r.last_ok_ms ? "ok" : "mid");
    out.push('<span class="hp" title="' + esc(r.error || "") + '"><i class="dot ' + rc + '"></i>Resync ' + ago(r.last_ok_ms) + "</span>");
    var f = h.status_feed, fs = S.desk.feed || "missing";
    var fc = f.error ? "bad" : fs === "live" ? "ok" : fs === "missing" ? "bad" : "mid";
    out.push('<span class="hp" title="' + esc(f.source || "") + '"><i class="dot ' + fc + '"></i>Desk feed ' + esc(fs) + "</span>");
    var uiAge = Date.now() - lastMsg;
    out.push('<span class="hp"><i class="dot ' + (uiAge < 30000 ? "ok" : "bad") + '"></i>UI ' + ago(lastMsg) + "</span>");
    setHTML($("health"), "health", out.join(""));
  }

  // ---------------------------------------------------------------- account

  function renderTotals() {
    var t = S.totals, el = $("tottotal"), split = $("totsplit"), per = $("totperiods"), hint = $("tothint");
    if (!el) return;
    if (!t) {
      setText(el, "tottotal", "—"); setHTML(split, "totsplit", ""); setHTML(per, "totperiods", "");
      setText(hint, "tothint", "waiting for feed + Alpaca"); return;
    }
    setText(el, "tottotal", money(t.combined_total));
    setHTML(split, "totsplit", "<span>Schwab<b>" + money(t.schwab_total) + "</b></span><span>Alpaca<b>" + money(t.alpaca_equity) + "</b></span>");
    var day = t.day || {}, week = t.week || {}, month = t.month || {}, year = t.year || {}, un = t.unrealized || {};
    function cell(label, pl, plpc, note) {
      var v = (pl == null ? '<span class="muted">n/a</span>' : '<span class="' + cls(pl) + '">' + smoney(pl) + (plpc == null ? "" : " " + pct(plpc)) + "</span>");
      return '<div class="p"><span class="l">' + label + '</span><span class="v">' + v + '</span>' + (note ? '<span class="s">' + note + "</span>" : "") + "</div>";
    }
    setHTML(per, "totperiods",
      cell("Day", day.combined_pl, null, (day.schwab_pl != null && day.alpaca_pl != null) ? "Schwab + Alpaca" : (day.alpaca_pl != null ? "Alpaca" : (day.schwab_pl != null ? "Schwab" : ""))) +
      cell("Week", week.alpaca_pl, week.alpaca_plpc, "Alpaca") +
      cell("Month", month.alpaca_pl, month.alpaca_plpc, "Alpaca") +
      cell("Year", year.alpaca_pl, year.alpaca_plpc, "Alpaca") +
      cell("Unrealized", un.combined_pl, null, (un.schwab_pl != null && un.alpaca_pl != null) ? "Schwab + Alpaca" : ""));
    var bits = [];
    if (t.schwab_as_of) bits.push("Schwab snapshot " + etTime(t.schwab_as_of, true));
    if (t.schwab_price_as_of) bits.push("prices " + etTime(t.schwab_price_as_of, true));
    bits.push("W/M/Y = Alpaca equity change (Schwab history not in feed)");
    setText(hint, "tothint", bits.join(" · "));
  }

  function renderAccount() {
    var a = S.account;
    setText($("equity"), "equity", money(a.equity));
    setHTML($("daypl"), "daypl", smoney(a.day_pl));
    setHTML($("dayplpc"), "dayplpc", a.day_plpc == null ? "" : "(" + pct(a.day_plpc) + ")");
    setText($("cash"), "cash", money(a.cash));
    setText($("lmv"), "lmv", money(a.long_market_value));
    setText($("bp"), "bp", money(a.buying_power));
    var env = $("env");
    var envText = (S.env || "").toUpperCase();
    var envClass = "badge " + (S.env === "live" ? "live" : "paper");
    if (env.textContent !== envText) env.textContent = envText;
    if (env.className !== envClass) env.className = envClass;
    $("mock").classList.toggle("hidden", !S.mock);
    var b = $("banner"), msgs = [];
    if (!S.keys_configured) msgs.push("No Alpaca API keys yet: open Settings → Add-ons → Trading Desk → Configuration and paste the key ID and secret.");
    if (S.mock) msgs.push("MOCK MODE: every number on this screen is simulated.");
    if (S.health.rest.error && S.keys_configured) msgs.push("Resync error: " + S.health.rest.error);
    setHTML(b, "banner", msgs.map(esc).join("<br>"));
    b.classList.toggle("hidden", !msgs.length);
    setText($("ver"), "ver", "v" + S.v);
  }

  // ---------------------------------------------------------------- risk
  function gauge(label, val, cap, capSrc, extra) {
    var ratio = (val != null && cap) ? val / cap : null;
    var w = ratio == null ? 0 : Math.min(100, ratio * 100);
    var c = ratio == null ? "" : ratio >= 1 ? "bad" : ratio >= 0.8 ? "mid" : "";
    return '<div class="gauge"><div class="lbl"><span>' + label + "</span><b>" +
      (val == null ? "—" : val.toFixed(2) + "%") + " / " + (cap == null ? "cap unknown" : cap + "%") +
      '</b></div><div class="bar"><i class="' + c + '" style="width:' + w + '%"></i></div>' +
      '<div class="note">cap source: ' + esc(capSrc) + (extra ? " · " + extra : "") + "</div></div>";
  }
  function renderRisk() {
    var r = S.risk, caps = r.caps, html = "";
    var extra = money(r.open_risk_usd) + " to stops";
    if (r.desk_open_risk_pct != null) extra += " · desk says " + r.desk_open_risk_pct.toFixed(2) + "%";
    html += gauge("Open risk vs TEMP cap", r.open_risk_pct, caps.temp_open_risk_pct.value, caps.temp_open_risk_pct.source, extra);
    var lg = r.largest;
    html += gauge("Largest name" + (lg ? " (" + esc(lg.symbol) + ")" : ""), lg ? lg.weight_pct : null,
      caps.single_name_cap_pct.value, caps.single_name_cap_pct.source);
    if (r.unprotected.length) html += '<div class="note dn">No resting stop: ' +
      r.unprotected.map(function (u) { return esc(u.symbol) + " " + num(u.qty); }).join(", ") + " (not counted in open risk)</div>";
    setHTML($("risk"), "risk", html);
  }

  // ---------------------------------------------------------------- positions
  function renderPositions() {
    var eq = S.account.equity, rows = S.positions.map(function (p) {
      var wt = eq ? Math.abs(p.market_value) / eq * 100 : null;
      return '<tr data-s="' + esc(p.symbol) + '"' + (p.symbol === selSym ? ' class="sel"' : "") + ">" +
        td("Sym", "<b>" + esc(p.symbol) + "</b>" + (p.price_live ? '<i class="live-dot" title="live"></i>' : "")) +
        td("Qty", num(p.qty)) + td("Avg cost", money(p.avg_entry)) + td("Price", money(p.price)) +
        td("Day", pct(p.day_change_pct), cls(p.day_change_pct)) +
        td("Unrl $ (vs cost)", smoney(p.unrealized_pl), cls(p.unrealized_pl)) +
        td("Unrl %", pct(p.unrealized_plpc), cls(p.unrealized_plpc)) +
        td("Wt", wt == null ? "—" : wt.toFixed(1) + "%") + "</tr>";
    });
    fillTbody("positions", rows.join("") || '<tr><td colspan="8" class="muted">No positions</td></tr>');
    var tot = S.positions.reduce(function (s, p) { return s + (p.unrealized_pl || 0); }, 0);
    setHTML($("poscount"), "poscount", S.positions.length + " · unrealized <span class=\"" + cls(tot) + "\">" + smoney(tot) + "</span>");
  }
  $("positions").addEventListener("click", function (e) {
    var tr = e.target.closest("tr[data-s]"); if (!tr) return;
    selSym = tr.getAttribute("data-s"); loadPrice(); renderPositions();
  });

  // ---------------------------------------------------------------- charts
  function chartOpts(el, color, base) {
    return { width: el.clientWidth || 300, height: el.clientHeight || 180, cursor: { show: false }, legend: { show: false },
      scales: { x: { time: true } },
      axes: [{ space: 70, stroke: "#666", grid: { stroke: "#111" }, ticks: { stroke: "#222" },
               values: function (u, vals) { return vals.map(function (v) { return new Date(v * 1000).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }); }); } },
             { stroke: "#666", grid: { stroke: "#111" }, ticks: { stroke: "#222" }, size: 64,
               values: function (u, vals) { return vals.map(function (v) { return "$" + Math.round(v).toLocaleString(); }); } }],
      series: [{}, { stroke: color, width: 2, fill: color + "22" }] };
  }
  var lastCurveKey = "";
  function renderCurve() {
    var el = $("curve"), c = S.curve;
    if (!c.t.length) {
      if (lastCurveKey === "empty") return;
      if (curvePlot) { try { curvePlot.destroy(); } catch (e) { /* ignore */ } curvePlot = null; }
      guardHeight(function () { el.innerHTML = '<div class="muted">No portfolio history yet</div>'; });
      lastCurveKey = "empty";
      return;
    }
    var last = c.equity[c.equity.length - 1], up = c.base == null || last >= c.base;
    var data = [c.t, c.equity];
    var key = c.t.length + ":" + c.t[c.t.length - 1] + ":" + last;
    if (!curvePlot) {
      guardHeight(function () { curvePlot = new uPlot(chartOpts(el, up ? "#22c55e" : "#ef4444"), data, el); });
      curvePlot._up = up;
    } else if (key !== lastCurveKey) {
      curvePlot.setData(data);
    }
    lastCurveKey = key;
    setText($("curvehint"), "curvehint", c.base != null ? "vs prior close " + money(c.base, 0) : "");
  }
  // Candles + VWAP + entry/stop lines; the last candle follows the live price tick.
  var PX = null;  // {sym, bars, entry, stops, t}
  function candlePlugin() {
    return { hooks: { draw: function (u) {
      if (!PX) return;
      var ctx = u.ctx, b = PX.bars, n = b.length; if (!n) return;
      var bw = Math.max(1, Math.min(10, (u.bbox.width / n) * 0.6));
      ctx.save();
      for (var i = 0; i < n; i++) {
        var x = u.valToPos(b[i].t, "x", true), o = b[i].o, c = b[i].c, hi = b[i].h, lo = b[i].l;
        if (o == null || c == null) continue;
        ctx.strokeStyle = ctx.fillStyle = c >= o ? "#22c55e" : "#ef4444";
        ctx.beginPath(); ctx.moveTo(x, u.valToPos(hi, "y", true)); ctx.lineTo(x, u.valToPos(lo, "y", true)); ctx.stroke();
        var y1 = u.valToPos(Math.max(o, c), "y", true), y2 = u.valToPos(Math.min(o, c), "y", true);
        ctx.fillRect(x - bw / 2, y1, bw, Math.max(1, y2 - y1));
      }
      function hline(v, col, label) {
        if (v == null) return; var y = u.valToPos(v, "y", true);
        ctx.strokeStyle = col; ctx.setLineDash([5, 4]); ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke();
        ctx.setLineDash([]); ctx.fillStyle = col; ctx.font = (11 * devicePixelRatio) + "px sans-serif";
        ctx.fillText(label + " " + money(v), u.bbox.left + 4, y - 3);
      }
      hline(PX.entry, "#e6e6e6", "entry");
      (PX.stops || []).forEach(function (sp) { hline(sp, "#ef4444", "stop"); });
      ctx.restore();
    } } };
  }
  function priceData() {
    var b = PX.bars, lo = [], hi = [];
    b.forEach(function (x) { lo.push(x.l); hi.push(x.h); });
    // y-range must include entry/stop lines: feed them as invisible series
    var ext = [PX.entry].concat(PX.stops || []).filter(function (v) { return v != null; });
    var mn = Math.min.apply(null, lo.concat(ext)), mx = Math.max.apply(null, hi.concat(ext));
    return [b.map(function (x) { return x.t; }), b.map(function (x) { return x.vw; }),
      b.map(function (x, i) { return i === 0 ? mn : i === b.length - 1 ? mx : null; })];
  }
  var lastPriceKey = "";
  function drawPrice() {
    var el = $("pricechart");
    if (!PX || !PX.bars.length) return;
    var data = priceData();
    var lastBar = PX.bars[PX.bars.length - 1];
    var key = PX.sym + ":" + PX.bars.length + ":" + lastBar.c + ":" + lastBar.h + ":" + lastBar.l;
    if (!pricePlot || pricePlot._sym !== PX.sym) {
      guardHeight(function () {
        el.innerHTML = "";
        var o = chartOpts(el, "#38bdf8");
        o.series = [{}, { stroke: "#a78bfa", width: 1.5, spanGaps: true }, { show: true, stroke: "transparent", points: { show: false } }];
        o.axes[1].values = function (u, vals) { return vals.map(function (v) { return "$" + v.toFixed(v < 20 ? 2 : 0); }); };
        o.plugins = [candlePlugin()];
        pricePlot = new uPlot(o, data, el); pricePlot._sym = PX.sym;
      });
      lastPriceKey = key;
    } else if (key !== lastPriceKey) {
      pricePlot.setData(data);
      lastPriceKey = key;
    }
  }
  function tickPrice() {
    // fold the live last-trade price into the newest 5-min candle
    if (!PX || !PX.bars.length || !S) return;
    var p = null; S.positions.forEach(function (x) { if (x.symbol === PX.sym && x.price_live) p = x.price; });
    if (p == null) return;
    var last = PX.bars[PX.bars.length - 1];
    if (last.c === p && p <= last.h && p >= last.l && (Math.floor(Date.now() / 1000) - last.t) < 300) return;
    var now = Math.floor(Date.now() / 1000);
    if (now - last.t >= 300) { PX.bars.push({ t: last.t + 300 * Math.floor((now - last.t) / 300), o: last.c, h: p, l: p, c: p, vw: last.vw }); }
    else { last.c = p; if (p > last.h) last.h = p; if (p < last.l) last.l = p; }
    drawPrice();
  }
  function loadPrice() {
    if (!selSym) return;
    setText($("pricesym"), "pricesym", selSym + " · 5-min candles, today");
    fetch("api/bars?symbol=" + encodeURIComponent(selSym)).then(function (r) { return r.json(); }).then(function (d) {
      var el = $("pricechart");
      if (!d.bars || !d.bars.length) {
        if (pricePlot) { try { pricePlot.destroy(); } catch (e2) { /* ignore */ } pricePlot = null; }
        guardHeight(function () { el.innerHTML = '<div class="muted">' + esc(d.error || "No bars yet today") + "</div>"; });
        PX = null; lastPriceKey = ""; return;
      }
      PX = { sym: d.symbol, bars: d.bars, entry: d.entry, stops: d.stops || [], t: Date.now() };
      setHTML($("pricelegend"), "pricelegend", '<span style="color:#a78bfa">VWAP</span> · entry ' + money(d.entry) +
        (PX.stops.length ? ' · <span class="dn">stop ' + PX.stops.map(function (v) { return money(v); }).join(", ") + "</span>" : ' · <span class="dn">no stop</span>'));
      pricePlot = null; lastPriceKey = ""; drawPrice();
    }).catch(function () {});
  }
  // refresh bars every 60 s for the selected symbol (one GET), ticks in between
  setInterval(function () { if (selSym && !document.hidden) loadPrice(); }, 60000);

  // ---------------------------------------------------------------- fills
  function renderFills() {
    var rows = S.fills.map(function (f) {
      return "<tr>" + td("Time", etTime(f.time)) + td("Sym", "<b>" + esc(f.symbol) + "</b>") +
        td("Side", esc(f.side), f.side === "buy" ? "up" : "dn") + td("Qty", num(f.qty)) + td("Price", money(f.price)) + "</tr>";
    });
    fillTbody("fills", rows.join("") || '<tr><td colspan="5" class="muted">No fills today</td></tr>');
    setText($("fillcount"), "fillcount", S.fills.length ? String(S.fills.length) : "");
  }
  function renderOrders() {
    var os = S.orders || [];
    fillTbody("orders", os.map(function (o) {
      return "<tr>" + td("Sym", "<b>" + esc(o.symbol) + "</b>") +
        td("Side", esc(o.side), o.side === "buy" ? "up" : "dn") + td("Type", esc(o.type)) +
        td("Qty", num(o.qty)) + td("Stop", o.stop_price == null ? "—" : money(o.stop_price)) +
        td("Limit", o.limit_price == null ? "—" : money(o.limit_price)) + td("Status", esc(o.status)) + "</tr>";
    }).join("") || '<tr><td colspan="7" class="muted">No open orders</td></tr>');
    setText($("ordcount"), "ordcount", os.length ? String(os.length) : "");
  }

  // Per-symbol realized breakdown: symbol escaped, amount from signed() inserted as HTML.
  function realizedList(m) {
    var ks = Object.keys(m || {});
    if (!ks.length) return "";
    return '<ul class="rlist">' + ks.map(function (k) { return "<li><span class=\"sym\">" + esc(k) + "</span> " + smoney(m[k]) + "</li>"; }).join("") + "</ul>";
  }
  function renderHoldings() {
    var h = (S.desk || {}).holdings;
    if (!h || !h.accounts) {
      fillTbody("holdings", '<tr><td colspan="8" class="muted">No holdings in the desk feed</td></tr>');
      setHTML($("holdtot"), "holdtot", "");
      return;
    }
    var rows = [];
    h.accounts.forEach(function (a) {
      (a.positions || []).forEach(function (p) {
        var dayCell = p.day_pl == null ? "n/a (snapshot price)" : smoney(p.day_pl) + " " + pct(p.day_plpc);
        var unCell = p.unrealized_pl == null ? "no cost basis" : smoney(p.unrealized_pl) + " " + pct(p.unrealized_plpc);
        rows.push("<tr>" + td("Acct", esc(a.name)) +
          td("Sym", esc(p.symbol) + (p.core ? ' <span class="badge dim">core</span>' : "")) +
          td("Shares", num(p.qty)) + td("Cost/sh", money(p.cost_per_share)) +
          td("Price", money(p.price) + (p.price_source && String(p.price_source).indexOf("live") === 0 ? "" : ' <span class="muted" title="no live quote">snap</span>')) +
          td("Value", money(p.value)) + td("Day", dayCell, p.day_pl == null ? "muted" : cls(p.day_pl)) +
          td("Unrl vs cost", unCell, p.unrealized_pl == null ? "" : cls(p.unrealized_pl)) + "</tr>");
      });
      if (a.cash) rows.push("<tr>" + td("Acct", esc(a.name)) + td("Sym", "cash", "muted") + td("Shares", "") +
        td("Cost/sh", "") + td("Price", "") + td("Value", money(a.cash)) + td("Day", "") + td("Unrl vs cost", "") + "</tr>");
    });
    fillTbody("holdings", rows.join(""));
    setHTML($("holdtot"), "holdtot", "<span>Schwab total</span><b>" + money(h.schwab_total) + "</b><span>Schwab unrealized</span><b class=\"" + cls(h.schwab_unrealized_pl) + "\">" + smoney(h.schwab_unrealized_pl) +
      "</b><span>Schwab today (vs prev close)</span><b class=\"" + cls(h.schwab_day_pl) + "\">" + smoney(h.schwab_day_pl) + " " + pct(h.schwab_day_plpc) + "</b><span>Alpaca equity</span><b>" + money(h.alpaca_equity) + "</b><span>Combined</span><b>" + money(h.combined_total) + "</b>");
    var at = h.alpaca_alltime, kv = function (l, v, c) { return "<span>" + l + "</span><b" + (c ? ' class="' + c + '"' : "") + ">" + v + "</b>"; }, x = "";
    if (at) {
      x += kv("Alpaca net deposits (since " + esc(at.first_activity || "?") + ")", money(at.net_deposits));
      x += kv("Alpaca total P&L since inception (equity − net deposits)", smoney(at.total_pl_since_inception) + " " + pct(at.total_pl_since_inception_pct), cls(at.total_pl_since_inception));
      x += kv("· Alpaca realized, closed trades (FIFO" + (at.realized_complete ? "" : ", incomplete") + ")", smoney(at.realized_pl_fifo) + realizedList(at.realized_by_symbol), cls(at.realized_pl_fifo));
      x += kv("· Alpaca unrealized, open positions", smoney(at.unrealized_pl), cls(at.unrealized_pl));
      x += kv("· Other (fees, dividends, rounding)", smoney(at.other_pl), cls(at.other_pl));
    } else x += kv("Alpaca since inception", "unavailable");
    x += kv("Schwab unrealized vs cost (realized not available; retirement fund has no cost basis)", smoney(h.schwab_unrealized_vs_cost), cls(h.schwab_unrealized_vs_cost));
    x += kv("Combined (Alpaca since inception + Schwab unrealized vs cost)", smoney(h.combined_alltime_pl), cls(h.combined_alltime_pl));
    setHTML($("alltime"), "alltime", x);
    setText($("holdhint"), "holdhint", (h.price_as_of ? "price as of " + etTime(h.price_as_of, true) + " · " : "") + (h.as_of ? "shares/cost snapshot " + etTime(h.as_of, true) : "") + " · not in open risk");
  }
  // ---------------------------------------------------------------- whole portfolio (desk feed)
  function renderPortfolio() {
    var p = (S.desk || {}).portfolio;
    if (!p || p.error) {
      setHTML($("pftot"), "pftot", '<span>Portfolio</span><b class="muted">' + esc(p && p.error ? "feed error: " + p.error : "not in desk feed yet") + "</b>");
      fillTbody("pf", "");
      return;
    }
    var kv = function (l, v) { return "<span>" + l + "</span><b>" + v + "</b>"; };
    setHTML($("pftot"), "pftot", kv("Equity", money(p.equity)) + kv("Today", smoney(p.day_pl) + " " + pct(p.day_plpc)) +
      kv("Lifetime (since " + esc(p.since || "?") + ")", smoney(p.lifetime_pl) + " " + pct(p.lifetime_plpc)) +
      kv("Net deposits", money(p.net_deposits)) + kv("Cash", money(p.cash)));
    fillTbody("pf", (p.positions || []).map(function (r) {
      return "<tr>" + td("Sym", "<b>" + esc(r.symbol) + "</b>") + td("Qty", num(r.qty)) + td("Price", money(r.price)) +
        td("Value", money(r.value)) + td("Wt", r.weight_pct == null ? "—" : r.weight_pct.toFixed(1) + "%") +
        td("Day $", smoney(r.day_pl)) + td("Day %", pct(r.day_plpc)) +
        td("Lifetime $", smoney(r.lifetime_pl)) + td("Lifetime %", pct(r.lifetime_plpc)) + "</tr>";
    }).join("") || '<tr><td colspan="9" class="muted">No positions</td></tr>');
    setText($("pfhint"), "pfhint", "Alpaca live" + (p.updated ? " · " + etTime(p.updated, true) : "") + " · lifetime = vs avg cost; total = equity − net deposits");
  }
  // ---------------------------------------------------------------- desk
  function renderBots() {
    var d = S.desk;
    $("deskex").classList.toggle("hidden", !d.example);
    $("propex").classList.toggle("hidden", !d.example);
    var fs = d.feed || "missing", fe = $("feedst");
    var feedText = fs === "live" ? "FEED LIVE" : fs === "stale" ? "FEED STALE" : fs === "missing" ? "NO DESK FEED" : "";
    var feedClass = "badge " + (fs === "live" ? "ok" : fs === "example" ? "hidden" : "warn");
    if (fe.textContent !== feedText) fe.textContent = feedText;
    if (fe.className !== feedClass) fe.className = feedClass;
    setText($("desksrc"), "desksrc", d.updated ? "updated " + etTime(d.updated, true) : (fs === "missing" ? "Desk Lead is not writing status.json; Alpaca-derived tiles only" : ""));
    setHTML($("bots"), "bots", d.bots.map(function (b) {
      return '<div class="tile"><div class="n"><span>' + esc(b.name) + (b.derived ? '<span class="d" title="computed from Alpaca">auto</span>' : "") + '</span><span class="st ' + esc(String(b.status).replace(/\s+/g, "-")) + '">' + esc(b.status) +
        '</span></div><div class="a">' + esc(b.last_action || "—") + '</div><div class="t">' + etTime(b.ts, true) + "</div></div>";
    }).join(""));
    setHTML($("proposals"), "proposals", (d.proposals || []).map(function (p) {
      if (typeof p === "string") return '<div class="prop">' + esc(p) + "</div>";
      var head = p.out || p.in ? esc(p.out || "?") + " → " + esc(p.in || "?") : esc(p.symbol || p.id || "proposal");
      return '<div class="prop"><b>' + head + "</b> " + (p.status ? '<span class="st">' + esc(p.status) + "</span>" : "") +
        (p.score != null ? ' <span class="muted">score ' + esc(p.score) + "</span>" : "") +
        '<div class="r">' + esc(p.reason || "") + (p.ts ? " · " + etTime(p.ts, true) : "") + "</div></div>";
    }).join("") || '<div class="muted">' + (fs === "missing" ? "No desk feed yet: proposals appear when Desk Lead writes status.json" : "No proposals in the feed") + "</div>");
    renderThresholds(d);
    renderHoldings();
    renderPortfolio();
  }
  function renderThresholds(d) {
    var g = (S.gauges || []).map(function (x) {
      var u = x.unit || "", v = x.value, lim = x.limit;
      var vs = v == null ? "—" : (u === "%" ? (x.info ? pct(v) : v.toFixed(2) + "%") : num(v, 2) + u);
      if (x.info) return '<div class="gauge"><div class="lbl"><span>' + esc(x.name) + '</span><b class="' + cls(v) + '">' + vs +
        '</b></div><div class="note">' + esc(x.source) + "</div></div>";
      var ratio = (v != null && lim) ? v / lim : null, bad = false, c = "";
      if (x.zero_ok) { bad = v > 0; c = bad ? "bad" : ""; ratio = bad ? 1 : 0; }
      else if (x.invert) { bad = v != null && lim != null && v <= lim; c = bad ? "bad" : ""; ratio = bad ? 1 : (lim ? Math.min(1, lim / Math.max(v, 1e-9)) : 0); }
      else c = ratio == null ? "" : ratio >= 1 ? "bad" : ratio >= 0.8 ? "mid" : "";
      var w = ratio == null ? 0 : Math.min(100, ratio * 100);
      return '<div class="gauge"><div class="lbl"><span>' + esc(x.name) + "</span><b>" + vs +
        (x.zero_ok ? "" : " / " + (lim == null ? "cap unknown" : num(lim, 2) + u)) + '</b></div><div class="bar"><i class="' + c + '" style="width:' + w +
        '%"></i></div><div class="note">' + esc(x.source) + "</div></div>";
    });
    var th = (d.thresholds || []).filter(function (t) { return t && (t.current == null || t.limit == null); })
      .map(function (t) { return typeof t === "string" ? "<span>" + esc(t) + "</span><b></b>" : "<span>" + esc(t.name) + "</span><b>" + esc(t.value != null ? t.value : t.limit) + "</b>"; });
    setHTML($("thresholds"), "thresholds", '<div class="gauges">' + g.join("") + "</div>" + (th.length ? '<div class="thr">' + th.join("") + "</div>" : "") +
      (d.example ? '<div class="note">Feed values are EXAMPLE data.</div>' : ""));
  }

  // ---------------------------------------------------------------- room (Phase 2 preview)
  function botFor(ev) { return ev.bot || STAGE_BOT[ev.stage] || "Desk Lead"; }
  function renderRoom() {
    var svg = $("room"), d = S.desk, status = {};
    d.bots.forEach(function (b) { status[b.name] = b.status; });
    var byTrade = {};
    (d.pipeline || []).slice().sort(function (a, b) { return String(a.ts).localeCompare(String(b.ts)); })
      .forEach(function (ev) { (byTrade[ev.trade_id] = byTrade[ev.trade_id] || []).push(ev); });
    var lines = [], flash = {}, todayET = new Date().toLocaleDateString("en-US", { timeZone: "America/New_York" });
    Object.keys(byTrade).forEach(function (tid) {
      var evs = byTrade[tid];
      // a fill (from Alpaca or the feed) drops the packet on the Book shelf
      var lastEv = evs[evs.length - 1];
      if (lastEv.stage === "fill") {
        var fkey = tid + ":book:" + lastEv.ts, isToday = new Date(lastEv.ts).toLocaleDateString("en-US", { timeZone: "America/New_York" }) === todayET;
        lines.push({ a: botFor(lastEv), b: "Book", fresh: isToday, label: lastEv.symbol + " filled", key: fkey, isNew: !seenHandoffs[fkey] });
        if (!seenHandoffs[fkey] && roomSig) flash[botFor(lastEv)] = 1;
        seenHandoffs[fkey] = 1;
      }
      for (var i = 1; i < evs.length; i++) {
        var a = botFor(evs[i - 1]), b = botFor(evs[i]); if (a === b || !DESKS[a] || !DESKS[b]) continue;
        var key = tid + ":" + evs[i].stage + ":" + evs[i].ts;
        var fresh = i === evs.length - 1;
        lines.push({ a: a, b: b, fresh: fresh, label: evs[i].symbol + " " + evs[i].stage, key: key, isNew: !seenHandoffs[key] });
        seenHandoffs[key] = 1;
      }
    });
    var h = "";
    lines.forEach(function (l, i) {
      var A = DESKS[l.a], B = DESKS[l.b], id = "p" + i;
      h += '<path id="' + id + '" class="ho' + (l.fresh ? "" : " old") + '" d="M' + A[0] + " " + A[1] + " L" + B[0] + " " + B[1] + '"/>';
      if (l.fresh) {
        h += '<circle r="5" class="pkt" cx="0" cy="0"><animateMotion begin="indefinite" dur="' + (l.isNew ? "1.2s" : "2.4s") + '" repeatCount="indefinite"><mpath href="#' + id + '"/></animateMotion></circle>';
        h += '<text class="lbl" x="' + ((A[0] + B[0]) / 2) + '" y="' + ((A[1] + B[1]) / 2 - 6) + '">' + esc(l.label) + "</text>";
      }
    });
    var nfresh = lines.filter(function (l) { return l.fresh; }).length;
    setText($("roomhint"), "roomhint", "hand-offs · " + (d.pipeline || []).length + " events" + (nfresh ? " · " + nfresh + " active" : "") + (d.feed === "missing" ? " · fills only (no desk feed)" : ""));
    Object.keys(DESKS).forEach(function (n) {
      var p = DESKS[n], st = n === "Book" ? "book" : (status[n] || "unknown");
      if (n === "Book") { h += '<g class="desk book"><rect x="' + (p[0] - 62) + '" y="' + (p[1] - 18) + '" width="124" height="36" rx="6"/><text x="' + p[0] + '" y="' + (p[1] + 4) + '">Book · ' + S.positions.length + ' held</text></g>'; return; }
      h += '<g class="desk ' + esc(String(st).replace(/\s+/g, "-")) + (flash[n] ? " flash" : "") + '"><rect x="' + (p[0] - 62) + '" y="' + (p[1] - 22) + '" width="124" height="44" rx="6"/>' +
        '<text x="' + p[0] + '" y="' + (p[1] - 2) + '">' + esc(n) + '</text><text class="s" x="' + p[0] + '" y="' + (p[1] + 13) + '">' + esc(st) + "</text></g>";
    });
    setHTML(svg, "room", h);
    // SMIL timelines start at page load; start each packet now so it is always moving.
    Array.prototype.forEach.call(svg.querySelectorAll("animateMotion"), function (m) {
      try { m.beginElement(); } catch (e) { /* older browsers: static line only */ } });
  }

  // ---------------------------------------------------------------- loop
  var roomSig = "";
  var lastDetail = 0, detailTimer = null, detailPending = false, streamHold = false;
  function detailVisible() {
    var el = $("deskview");
    return !!(el && !el.classList.contains("hidden"));
  }
  function runDetail() {
    if (!S || !detailVisible()) return;
    lastDetail = Date.now();
    if (window.TDRenderCounts) window.TDRenderCounts.desk++;
    var desk = S.desk || {};
    var sig = JSON.stringify(desk.pipeline || []) + JSON.stringify((desk.bots || []).map(function (b) { return [b.name, b.status]; })) + (S.positions || []).length;
    var roomChanged = sig !== roomSig;
    try {
      renderTotals(); renderAccount(); renderRisk(); renderPositions(); renderCurve();
      renderFills(); renderOrders(); renderBots(); if (roomChanged) renderRoom(); renderHealth(); tickPrice();
    } catch (e) { console.error(e); }
    roomSig = sig;
  }
  function scheduleDetail(force) {
    if (!detailVisible() || !S) return;
    if (force) {
      if (detailTimer) { clearTimeout(detailTimer); detailTimer = null; }
      detailPending = false;
      runDetail();
      return;
    }
    if (detailPending) return;
    var wait = Math.max(0, 1000 - (Date.now() - lastDetail));
    detailPending = true;
    detailTimer = setTimeout(function () {
      detailTimer = null;
      detailPending = false;
      requestAnimationFrame(function () { if (detailVisible()) runDetail(); });
    }, wait);
  }
  function onState(raw) {
    lastMsg = Date.now();
    S = typeof raw === "string" ? JSON.parse(raw) : raw;
    window.TDShared = S;
    scheduleDetail(false);
    if (window.TDRenderMain) window.TDRenderMain(false);
    if (window.TDAssets && window.TDAssets.onState) window.TDAssets.onState();
  }
  window.TDApplyState = onState;
  window.TDRenderDetail = function () { scheduleDetail(true); };
  window.TDStop = function () { streamHold = true; if (es) { es.close(); es = null; } };
  function connect() {
    if (es) es.close();
    es = new EventSource("api/events");
    es.addEventListener("state", function (e) { onState(e.data); });
    es.onopen = function () { lastMsg = Date.now(); };
  }
  // Watchdog: server pings every 15 s; if nothing for 45 s, reopen the stream.
  setInterval(function () {
    $("clock").textContent = new Date().toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit", second: "2-digit" }) + " ET";
    if (S) renderHealth();
    if (!streamHold && Date.now() - lastMsg > 45000) { lastMsg = Date.now(); connect(); }
  }, 1000);
  window.addEventListener("resize", function () {
    [[curvePlot, "curve"], [pricePlot, "pricechart"]].forEach(function (p) {
      if (p[0]) { var el = $(p[1]); p[0].setSize({ width: el.clientWidth || 300, height: el.clientHeight || 220 }); } });
  });
  // EventSource only reports comment pings implicitly; count any open as alive.
  connect();
  fetch("api/state").then(function (r) { return r.json(); }).then(function (d) { if (!S) onState(JSON.stringify(d)); });
})();
