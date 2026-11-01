/* Assets tab: GET api/assets (relative, works under Ingress). Read-only; renders a fed snapshot. */
(function () {
  const view = document.getElementById("assetsview");
  let timer = null;
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const M = (x, s) => (s && x > 0 ? "+" : x < 0 ? "-" : "") + "$" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const P = (x) => "(" + (x > 0 ? "+" : x < 0 ? "-" : "") + Math.abs(x).toFixed(2) + "%)";
  const C = (x) => (x > 0 ? "as-up" : x < 0 ? "as-dn" : "as-mu");
  const N = (x, d) => Number(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

  function render(r) {
    if (!r || !r.ok) {
      view.innerHTML = '<div class="as-empty">No assets snapshot yet. ' + esc((r && r.error) || "") +
        '<br>Feed one to <b>/share/trading_desk/assets.json</b> (or via the Life HQ feed).</div>';
      return;
    }
    const a = r.assets, t = a.equities_total || {}, c = a.cash || {};
    const d = new Date(a.as_of);
    const ds = isNaN(d) ? "?" : d.toLocaleDateString("en-US", { month: "numeric", day: "numeric", year: "numeric", timeZone: "America/New_York" }) +
      " " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET";
    let h = '<div class="as-hd"><h1>' + esc(a.account || "Account") + '<span class="as-pill ' + (a.stale ? "stale" : "fresh") + '">' + (a.stale ? "STALE" : "fresh") +
      '</span></h1><small>as of ' + esc(ds) + "</small></div>";
    h += '<div class="as-sum"><div class="as-row"><span class="l">Account Value</span><span class="as-big">' + M(a.account_value) + "</span></div>" +
      '<div class="as-row"><span class="l">Day Change</span><span class="as-v ' + C(a.day_change) + '">' + M(a.day_change, 1) + " " + P(a.day_change_pct) + "</span></div>" +
      '<div class="as-row"><span class="l">Unrealized Gain/Loss</span><span class="as-v ' + C(a.unrealized) + '">' + M(a.unrealized, 1) + " " + P(a.unrealized_pct) + "</span></div></div>";
    h += '<table class="as-pos"><thead><tr><th>Symbol</th><th>Qty<br>Price<br>Chg</th><th>Mkt Val<br>Day Chg<br>Gain/Loss</th></tr></thead><tbody>' +
      '<tr class="sec"><td>Equities</td><td colspan="2">' + esc(t.alloc_pct) + "%</td></tr>";
    (a.positions || []).forEach((x) => {
      h += '<tr><td><div class="s">' + esc(x.symbol) + '</div><div class="n">' + esc(x.name) + "</div></td><td><div>" + N(x.qty, 4) + "</div><div>$" + N(x.price, 2) +
        '</div><div class="' + C(x.price_chg) + '">' + (x.price_chg > 0 ? "+" : "-") + "$" + Math.abs(x.price_chg).toFixed(2) + "</div></td><td><div>" + M(x.mkt_val) +
        '</div><div class="' + C(x.day_chg) + '">' + M(x.day_chg, 1) + " " + P(x.day_pct) + '</div><div class="' + C(x.gain) + '">' + M(x.gain, 1) + " " + P(x.gain_pct) + "</div></td></tr>";
    });
    h += '<tr class="tot"><td><div class="s" style="font-weight:600">Equities Total</div></td><td colspan="2"><div>' + M(t.mkt_val) + '</div><div class="' + C(t.day_chg) + '">' +
      M(t.day_chg, 1) + " " + P(t.day_pct) + '</div><div class="' + C(t.gain) + '">' + M(t.gain, 1) + " " + P(t.gain_pct) + "</div></td></tr>" +
      '<tr class="sec"><td>Cash &amp; Money Market</td><td colspan="2">' + esc(c.alloc_pct) + "%</td></tr><tr><td>Cash &amp; Cash Investments</td><td colspan=\"2\">" + M(c.value) + "</td></tr></tbody></table>";
    h += '<div class="as-note">Manual snapshot (' + esc(a.source || "fed file") + "). No brokerage login. Read-only, behind the HA login.</div>";
    view.innerHTML = h;
  }

  async function refresh() {
    try {
      const res = await fetch("api/assets", { cache: "no-store" });
      render(await res.json());
    } catch (e) {
      view.innerHTML = '<div class="as-empty">Could not load the assets snapshot.</div>';
    }
  }

  // Tab switching lives in main.js (0.2.0); this file only renders the Schwab tab.
  window.TDAssets = {
    start: function () { refresh(); if (!timer) timer = setInterval(refresh, 60000); },
    stop: function () { if (timer) { clearInterval(timer); timer = null; } },
  };
})();
