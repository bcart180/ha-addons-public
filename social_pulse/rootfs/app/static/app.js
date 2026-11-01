(function () {
  "use strict";
  var range = "7d", snap = null;
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; }); }
  function num(n) {
    if (n == null) return "—";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e4) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return n.toLocaleString();
  }
  function bytes(b) { if (b == null) return "—"; var u = ["B", "KB", "MB", "GB", "TB"], i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return b.toFixed(i ? 1 : 0) + " " + u[i]; }
  var NA = [];
  function metric(label, m, fmt, where) {
    if (!m) return "";
    if (!m.available) {
      NA.push([where + " · " + label, m.reason]);
      return '<div class="m na" title="' + esc(m.reason) + '"><div class="k">' + esc(label) + '</div><div class="v">Not available</div></div>';
    }
    return '<div class="m"><div class="k">' + esc(label) + '</div><div class="v">' + (fmt || num)(m.value) + "</div></div>";
  }
  function val(m) { return m && m.available ? m.value : null; }
  function sum(list, f) { var t = 0, any = false; list.forEach(function (x) { var v = f(x); if (v != null) { t += v; any = true; } }); return any ? t : null; }

  function hero() {
    var xs = (snap.x || []).filter(function (a) { return !a.error; });
    var ss = snap.sites || [];
    var tiles = [
      ["Followers", sum(xs, function (a) { return val(a.followers); }), xs.length + " X accounts"],
      ["Post impressions", sum(xs, function (a) { return val(a.post_impressions_7d); }), "public, last 7 days"],
      ["Page views", sum(ss, function (s) { return val(s["page_views_" + range]); }), "Cloudflare edge, " + range],
      ["Visitors", sum(ss, function (s) { return val(s["uniques_" + range]); }), "daily uniques summed, " + range]
    ];
    $("hero").innerHTML = tiles.map(function (t) {
      return '<div class="tile"><div class="k">' + t[0] + '</div><div class="v">' + (t[1] == null ? "—" : num(t[1])) + '</div><div class="s">' + t[2] + "</div></div>";
    }).join("");
  }

  function xcards() {
    $("x").innerHTML = (snap.x || []).map(function (a) {
      if (a.error) return '<div class="card"><div class="acct"><div class="av">@</div><div><div class="n">@' + esc(a.handle) + '</div></div></div><div class="err">' + esc(a.error) + "</div></div>";
      var w = "@" + a.handle, posts = (a.recent_posts || []).slice().reverse();
      var max = Math.max.apply(null, posts.map(function (p) { return p.impressions || 0; }).concat([1]));
      var bars = posts.map(function (p) { return '<span title="' + esc((p.impressions || 0) + " impressions") + '" style="height:' + Math.max(4, Math.round(56 * (p.impressions || 0) / max)) + 'px"></span>'; }).join("");
      var recent = (a.recent_posts || []).slice(0, 3).map(function (p) {
        return '<div class="post"><a href="' + esc(p.url) + '" target="_blank" rel="noopener">' + esc(p.text) + "</a><b>" + num(p.impressions) + " views · " + num((p.likes || 0) + (p.reposts || 0) + (p.replies || 0)) + " eng</b></div>";
      }).join("");
      return '<div class="card"><div class="acct"><div class="av">' + esc((a.name || a.handle).charAt(0)) + '</div><div><div class="n">' + esc(a.name) +
        '</div><a href="https://x.com/' + esc(a.handle) + '" target="_blank" rel="noopener">@' + esc(a.handle) + "</a></div></div>" +
        '<div class="metrics">' + metric("Followers", a.followers, null, w) + metric("Posts (7d)", a.posts_7d, null, w) + metric("Impressions (7d)", a.post_impressions_7d, null, w) +
        metric("Engagements (7d)", a.engagements_7d, null, w) + metric("Eng. rate", a.engagement_rate_7d, function (v) { return v + "%"; }, w) + metric("Total posts", a.posts_total, null, w) +
        metric("Profile visits", a.profile_visits, null, w) + metric("Link clicks", a.link_clicks, null, w) + metric("Account impressions", a.account_impressions, null, w) + "</div>" +
        (posts.length ? '<div class="bars">' + bars + '</div><div class="cap">Impressions per post, last ' + posts.length + " posts</div>" : '<div class="cap" style="margin-top:12px">No original posts yet</div>') +
        recent + "</div>";
    }).join("");
  }

  function chart(daily) {
    var d = range === "7d" ? daily.slice(-7) : daily;
    if (!d.length) return '<div class="cap">No daily data</div>';
    var W = 520, H = 130, P = 6;
    var mx = Math.max.apply(null, d.map(function (r) { return r.requests; }).concat([1]));
    function line(key) {
      return d.map(function (r, i) { var x = P + (d.length === 1 ? 0 : i * (W - 2 * P) / (d.length - 1)); var y = H - P - (H - 2 * P) * r[key] / mx; return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1); }).join(" ");
    }
    var area = line("requests") + " L" + (W - P) + " " + (H - P) + " L" + P + " " + (H - P) + " Z";
    return '<svg class="chart" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none"><path d="' + area + '" fill="#d5f0ec"/><path d="' + line("requests") +
      '" fill="none" stroke="#2a9d8f" stroke-width="2.5"/><path d="' + line("page_views") + '" fill="none" stroke="#1e2a36" stroke-width="2" stroke-dasharray="5 4"/></svg>' +
      '<div class="legend"><span><i style="background:#2a9d8f"></i>Requests</span><span><i style="background:#1e2a36"></i>Page views</span><span>' + esc(d[0].date) + " → " + esc(d[d.length - 1].date) + "</span></div>";
  }
  function list(title, m, where, fmtKey) {
    if (!m) return "";
    if (!m.available) { NA.push([where + " · " + title, m.reason]); return '<div><h4>' + esc(title) + '</h4><div class="cap">Not available</div></div>'; }
    var rows = m.value || [], mx = Math.max.apply(null, rows.map(function (r) { return r[1]; }).concat([1]));
    return "<div><h4>" + esc(title) + "</h4>" + (rows.length ? rows.slice(0, 6).map(function (r) {
      return '<div class="row"><span>' + esc(fmtKey ? fmtKey(r[0]) : r[0]) + "</span><span>" + num(r[1]) + '</span></div><div class="meter"><i style="width:' + (100 * r[1] / mx) + '%"></i></div>';
    }).join("") : '<div class="cap">None in range</div>') + "</div>";
  }
  function sites() {
    $("sites").innerHTML = (snap.sites || []).map(function (s) {
      var w = s.name, r = range;
      return '<div class="card site"><h3>' + esc(s.name) + '</h3><div class="cap">' + (s.data_since ? "Cloudflare data since " + esc(s.data_since) : "") + (s.error ? ' <span class="err">' + esc(s.error) + "</span>" : "") + "</div>" +
        chart(s.daily || []) + '<div class="metrics" style="margin-top:12px">' +
        metric("Requests", s["requests_" + r], null, w) + metric("Page views", s["page_views_" + r], null, w) + metric("Visitors*", s["uniques_" + r], null, w) +
        metric("Bandwidth", s["bandwidth_bytes_" + r], bytes, w) + metric("Threats blocked", s["threats_" + r], null, w) + metric("Real page loads (30d)", s.rum_pageloads_30d, null, w) + "</div>" +
        '<div class="lists">' + list("Top pages (7d, edge)", s.top_paths_7d, w) + list("Countries (7d)", s.countries_7d, w) +
        list("Referrers (Web Analytics, 30d)", s.rum_referrers, w) + list("Pages (Web Analytics, 30d)", s.rum_top_pages, w) + "</div>" +
        '<div class="cap" style="margin-top:10px">*Sum of daily unique IPs. Edge counts include bots' + (s.bot_probe_requests_7d && s.bot_probe_requests_7d.available ? " (" + num(s.bot_probe_requests_7d.value) + " WordPress/PHP probe hits filtered from Top pages)" : "") + ". Web Analytics is sampled.</div></div>";
    }).join("");
  }
  function render() {
    NA = [];
    var t = snap.generated_at ? new Date(snap.generated_at) : null;
    $("updated").textContent = t ? "Updated " + t.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Waiting for first refresh";
    hero(); xcards(); sites();
    (snap.errors || []).forEach(function (e) { NA.push(["Refresh error", e]); });
    var groups = {}, order = [];
    NA.forEach(function (n) { var parts = n[0].split(" · "), k = (parts[1] || parts[0]) + "|" + n[1];
      if (!groups[k]) { groups[k] = { label: parts[1] || parts[0], reason: n[1], who: [] }; order.push(k); }
      if (parts[1] && groups[k].who.indexOf(parts[0]) < 0) groups[k].who.push(parts[0]); });
    var rows = order.map(function (k) { var g = groups[k]; return [g.label + (g.who.length ? " (" + g.who.join(", ") + ")" : ""), g.reason]; });
    $("na").innerHTML = rows.length ? rows.map(function (n) { return '<div class="row"><span>' + esc(n[0]) + "</span><span>" + esc(n[1]) + "</span></div>"; }).join("") : '<div class="cap">Everything configured is available.</div>';
  }
  document.querySelectorAll(".seg button").forEach(function (b) {
    b.addEventListener("click", function () { document.querySelectorAll(".seg button").forEach(function (x) { x.classList.toggle("on", x === b); }); range = b.dataset.range; if (snap) render(); });
  });
  function load() { fetch("api/snapshot", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) { snap = j; render(); }).catch(function () { $("updated").textContent = "Can't reach the add-on"; }); }
  load(); setInterval(load, 5 * 60 * 1000);
})();
