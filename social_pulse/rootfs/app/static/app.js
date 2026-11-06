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
  function src(m) {
    if (!m || !m.source) return "";
    if (m.source === "web_analytics") return "Sampled";
    if (m.source === "edge") return "Edge";
    return "";
  }
  function metric(label, m, fmt, where) {
    if (!m) return "";
    var badge = src(m) ? '<div class="src">' + src(m) + "</div>" : "";
    if (!m.available) {
      NA.push([where + " · " + label, m.reason]);
      return '<div class="m na" title="' + esc(m.reason) + '">' + badge + '<div class="k">' + esc(label) + '</div><div class="v">Not available</div></div>';
    }
    var note = m.note ? '<div class="note">' + esc(m.note) + "</div>" : "";
    return '<div class="m">' + badge + '<div class="k">' + esc(label) + '</div><div class="v">' + (fmt || num)(m.value) + "</div>" + note + "</div>";
  }
  function val(m) { return m && m.available ? m.value : null; }
  function sum(list, f) { var t = 0, any = false; list.forEach(function (x) { var v = f(x); if (v != null) { t += v; any = true; } }); return any ? t : null; }

  function hero() {
    var xs = (snap.x || []).filter(function (a) { return !a.error; });
    var ss = snap.sites || [];
    var tiles = [
      ["Followers", sum(xs, function (a) { return val(a.followers); }), xs.length + " X accounts"],
      ["Post impressions", sum(xs, function (a) { return val(a.post_impressions_7d); }), "public, last 7 days"],
      ["Edge page views", sum(ss, function (s) { return val(s["page_views_" + range]); }), "Edge HTML, not sampled visits, " + range],
      ["Summed daily uniques", sum(ss, function (s) { return val(s["uniques_" + range]); }), "Not deduplicated across " + range]
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
      '<div class="legend"><span><i style="background:#2a9d8f"></i>Edge requests</span><span><i style="background:#1e2a36"></i>Edge page views</span><span>' + esc(d[0].date) + " → " + esc(d[d.length - 1].date) + "</span></div>";
  }
  function shareLine(row) {
    if (!row || !row.available) return "";
    return '<div class="cap">' + esc(row.label) + ", last 7 days are " + row.pct + "% of the fetched 30-day window (" + num(row.part) + " of " + num(row.whole) + ").</div>";
  }
  function prevLine(s) {
    var p = s.prev7;
    if (!p) return "";
    if (!p.available) return '<div class="cap">' + esc(p.reason) + "</div>";
    function one(label, row) {
      if (!row) return "";
      if (row.change_pct == null) return esc(label) + ": prior 7 days " + num(row.prior) + ". " + esc(row.change_reason) + ".";
      var sign = row.change_pct > 0 ? "+" : "";
      return esc(label) + " " + sign + row.change_pct + "% versus " + esc(row.prior_start) + " through " + esc(row.prior_last) + " (" + num(row.prior) + ").";
    }
    return '<div class="cap">' + one("Edge requests", p.requests) + "</div><div class=\"cap\">" + one("Edge page views", p.page_views) + "</div>";
  }
  var KNOWN = ["winchestergetaway.com", "hideawaytavernlcv.com"];
  function emptySite(name) {
    return '<div class="card site"><h3>' + esc(name) + '</h3><div class="err">No Cloudflare data for this site.</div><div class="cap">Add the zone name, zone id, and Web Analytics site tag in Configuration, plus the Cloudflare API token. This card stays empty until Cloudflare returns data. Nothing here is estimated.</div></div>';
  }
  function list(title, m, where, fmtKey) {
    if (!m) return "";
    if (!m.available) { NA.push([where + " · " + title, m.reason]); return '<div><h4>' + esc(title) + '</h4><div class="cap">Not available</div></div>'; }
    var rows = m.value || [], mx = Math.max.apply(null, rows.map(function (r) { return r[1]; }).concat([1]));
    return "<div><h4>" + esc(title) + "</h4>" + (rows.length ? rows.slice(0, 6).map(function (r) {
      return '<div class="row"><span>' + esc(fmtKey ? fmtKey(r[0]) : r[0]) + "</span><span>" + num(r[1]) + '</span></div><div class="meter"><i style="width:' + (100 * r[1] / mx) + '%"></i></div>';
    }).join("") : '<div class="cap">None in range</div>') + "</div>";
  }
  function rumKey(s, base) {
    var key = base + "_" + range;
    if (s[key]) return s[key];
    if (range === "30d" && s[base + "_30d"]) return s[base + "_30d"];
    if (range === "30d" && base === "rum_top_pages") return s.rum_top_pages;
    if (range === "30d" && base === "rum_referrers") return s.rum_referrers;
    return {value: null, available: false, source: "web_analytics", reason: "This snapshot has no " + range + " Web Analytics field."};
  }
  function sites() {
    var rows = snap.sites || [];
    function norm(s) { return String(s || "").toLowerCase().replace(/[^a-z0-9]/g, ""); }
    var configured = rows.map(function (s) { return norm(s.name); });
    function knownCovered(host) {
      var h = norm(host);
      return configured.some(function (n) {
        if (!n) return false;
        if (n === h) return true;
        var short = n.length < h.length ? n : h, long = n.length < h.length ? h : n;
        return short.length >= 10 && long.indexOf(short) >= 0;
      });
    }
    var html = rows.map(function (s) {
      var w = s.name, r = range, win = s.windows || {};
      var head = '<div class="card site"><h3>' + esc(s.name) + '</h3><div class="cap">' + (s.data_since ? "Cloudflare data since " + esc(s.data_since) + ". " : "") + (s.error ? '<span class="err">' + esc(s.error) + "</span>" : "") + "</div>";
      var uniq = s["uniques_" + r] || {value: null, available: false, reason: "Summed daily uniques were not returned.", source: "edge", note: "Sum of daily unique IPs. Not deduplicated across the period."};
      var countries = s[r === "7d" ? "countries_7d" : "countries_30d"] || s.countries_7d;
      var countryLabel = r === "7d" ? "Countries (edge requests, 7 days)" : "Countries (edge requests, 30 days)";
      var sampledNote = (s.rum_status && s.rum_status !== "ok" && s.rum_status_detail) ? s.rum_status_detail : "Sampled visits are Web Analytics beacon events, not edge requests.";
      var probes = (s.bot_probe_requests_7d && s.bot_probe_requests_7d.available) ? " " + num(s.bot_probe_requests_7d.value) + " WordPress/PHP probe hits are hidden from Top pages." : "";
      return head + chart(s.daily || []) +
        '<div class="group-label">Edge · HTTP at Cloudflare</div><div class="metrics">' +
        metric("Requests", s["requests_" + r], null, w) + metric("Page views", s["page_views_" + r], null, w) + metric("Summed daily uniques", uniq, null, w) +
        metric("Bandwidth", s["bandwidth_bytes_" + r], bytes, w) + metric("Threats blocked", s["threats_" + r], null, w) + metric("Probe hits (7 days)", s.bot_probe_requests_7d, null, w) + "</div>" +
        '<div class="group-label">Sampled · Web Analytics</div><div class="metrics">' +
        metric("Visits (" + r + ")", rumKey(s, "rum_visits"), null, w) + metric("Page loads (" + r + ")", rumKey(s, "rum_pageloads"), null, w) + "</div>" +
        '<div class="compare">' + shareLine(win.edge_requests) + shareLine(win.edge_page_views) + shareLine(win.rum_visits) + shareLine(win.rum_pageloads) + prevLine(s) + "</div>" +
        '<div class="lists">' + list("Top pages (edge requests, rolling 7 days)", s.top_paths_7d, w) + list(countryLabel, countries, w) +
        list("Referrers (sampled, " + r + ")", rumKey(s, "rum_referrers"), w) + list("Pages (sampled, " + r + ")", rumKey(s, "rum_top_pages"), w) + "</div>" +
        '<div class="cap" style="margin-top:10px">Edge requests count every HTTP request, including bots and assets. Edge page views are the HTML class at the edge, not browser visits. ' +
        esc((uniq.available && uniq.note) ? uniq.note : "Summed daily uniques are not deduplicated across the 7 or 30 days.") + probes + " " + esc(sampledNote) + "</div></div>";
    }).join("");
    KNOWN.forEach(function (name) { if (!knownCovered(name)) html += emptySite(name); });
    if (!html) html = KNOWN.map(emptySite).join("");
    $("sites").innerHTML = html;
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
