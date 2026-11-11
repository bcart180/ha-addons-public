(function () {
  "use strict";
  var range = "7d", snap = null;
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; });
  }
  function safeUrl(u) {
    var s = String(u || "");
    return /^https?:\/\//i.test(s) ? s : "";
  }
  function num(n) {
    if (n == null || n !== n) return "—";
    var v = Number(n);
    if (v !== v) return "—";
    if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (v >= 1e4) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return v.toLocaleString();
  }
  function bytes(b) {
    if (b == null) return "—";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0, n = Number(b);
    while (n >= 1024 && i < 4) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }
  var NA = [];
  function src(m) {
    if (!m || !m.source) return null;
    if (m.source === "web_analytics") return {k: "Sampled", c: "sampled"};
    if (m.source === "edge") return {k: "Edge", c: "edge"};
    return null;
  }
  function metric(label, m, fmt, where) {
    if (!m) return "";
    var s = src(m);
    var badge = s ? '<div class="src ' + s.c + '">' + s.k + "</div>" : "";
    if (!m.available) {
      NA.push([where + " · " + label, m.reason]);
      return '<div class="m na" title="' + esc(m.reason) + '">' + badge + '<div class="k">' + esc(label) + '</div><div class="v">Not available</div></div>';
    }
    var note = m.note ? '<div class="note">' + esc(m.note) + "</div>" : "";
    return '<div class="m">' + badge + '<div class="k">' + esc(label) + '</div><div class="v">' + (fmt || num)(m.value) + "</div>" + note + "</div>";
  }
  function val(m) { return m && m.available ? m.value : null; }
  function sum(list, f) {
    var t = 0, any = false;
    list.forEach(function (x) { var v = f(x); if (v != null) { t += v; any = true; } });
    return any ? t : null;
  }

  function isDoc(el) {
    return !el || el === document.scrollingElement || el === document.documentElement || el === document.body;
  }
  function documentOffset() {
    var y = window.pageYOffset || window.scrollY || 0;
    var h = document.documentElement ? document.documentElement.scrollTop || 0 : 0;
    var b = document.body ? document.body.scrollTop || 0 : 0;
    return Math.max(y, h, b);
  }
  function scrollRoot() {
    var se = document.scrollingElement || document.documentElement;
    if (documentOffset() > 0) return se;
    var node = document.querySelector(".page");
    while (node && node !== document.documentElement) {
      if (node.scrollTop > 0) return node;
      node = node.parentElement;
    }
    node = document.querySelector(".page");
    var overflow = null;
    while (node && node !== document) {
      var oy = "";
      try { oy = window.getComputedStyle(node).overflowY; } catch (e) { oy = ""; }
      if ((oy === "auto" || oy === "scroll" || oy === "overlay") && node.scrollHeight > node.clientHeight + 1) overflow = node;
      node = node.parentElement;
    }
    if (se && (se.scrollHeight > (window.innerHeight || 0) + 1 || se.scrollHeight > se.clientHeight + 1)) return se;
    return overflow || se;
  }
  function readTop(el) {
    if (isDoc(el)) return documentOffset();
    return el.scrollTop || 0;
  }
  function documentMax() {
    var se = document.scrollingElement || document.documentElement;
    var h = Math.max(se ? se.scrollHeight : 0, document.body ? document.body.scrollHeight : 0);
    var view = window.innerHeight || (se ? se.clientHeight : 0) || 0;
    return Math.max(0, h - view);
  }
  function writeTop(el, top) {
    var max, next;
    if (isDoc(el)) {
      max = documentMax();
      if (top > 0 && max <= 0) return;
      next = Math.min(Math.max(0, top), max);
      if (document.documentElement) document.documentElement.scrollTop = next;
      if (document.body) document.body.scrollTop = next;
      window.scrollTo(0, next);
      return;
    }
    max = Math.max(0, el.scrollHeight - el.clientHeight);
    if (top > 0 && max <= 0) return;
    next = Math.min(Math.max(0, top), max);
    el.scrollTop = next;
  }
  function dropContentFocus() {
    var active = document.activeElement;
    if (!active || active === document.body || active === document.documentElement) return;
    if (active.closest && active.closest("#hero, #x, #sites, #na") && active.blur) active.blur();
  }

  function hero() {
    var xs = (snap.x || []).filter(function (a) { return !a.error; });
    var ss = snap.sites || [];
    var tiles = [
      ["Followers", sum(xs, function (a) { return val(a.followers); }), xs.length + " X accounts", "followers"],
      ["Post impressions", sum(xs, function (a) { return val(a.post_impressions_7d); }), "public, last 7 days", "impressions"],
      ["Edge page views", sum(ss, function (s) { return val(s["page_views_" + range]); }), "Edge HTML, not sampled visits, " + range, "edge"],
      ["Summed daily uniques", sum(ss, function (s) { return val(s["uniques_" + range]); }), "Not deduplicated across " + range, "uniques"]
    ];
    $("hero").innerHTML = tiles.map(function (t) {
      return '<div class="tile cat-' + t[3] + '"><div class="k"><i></i>' + t[0] + '</div><div class="v">' + (t[1] == null ? "—" : num(t[1])) + '</div><div class="s">' + esc(t[2]) + "</div></div>";
    }).join("");
  }

  function xcards() {
    var rows = snap.x || [];
    if (!rows.length) {
      $("x").innerHTML = '<div class="card"><p class="empty">No X accounts in this snapshot.</p></div>';
      return;
    }
    $("x").innerHTML = rows.map(function (a) {
      if (a.error) {
        return '<div class="card"><div class="acct"><div class="av">@</div><div class="who"><div class="n">@' + esc(a.handle) + '</div></div></div><div class="err">' + esc(a.error) + "</div></div>";
      }
      var w = "@" + (a.handle || "");
      var posts = (a.recent_posts || []).slice().reverse();
      var max = 1;
      posts.forEach(function (p) { if ((p.impressions || 0) > max) max = p.impressions || 0; });
      var bars = posts.map(function (p) {
        return '<span title="' + esc((p.impressions || 0) + " impressions") + '" style="height:' + Math.max(4, Math.round(56 * (p.impressions || 0) / max)) + 'px"></span>';
      }).join("");
      var recent = (a.recent_posts || []).slice(0, 3).map(function (p) {
        var href = safeUrl(p.url);
        var text = esc(p.text);
        var link = href ? '<a href="' + esc(href) + '" target="_blank" rel="noopener">' + text + "</a>" : "<span>" + text + "</span>";
        return '<div class="post">' + link + "<b>" + num(p.impressions) + " views · " + num((p.likes || 0) + (p.reposts || 0) + (p.replies || 0)) + " eng</b></div>";
      }).join("");
      return '<div class="card"><div class="acct"><div class="av">' + esc((a.name || a.handle || "?").charAt(0)) + '</div><div class="who"><div class="n">' + esc(a.name || a.handle) +
        '</div><div class="handle">@' + esc(a.handle) + "</div></div></div>" +
        '<div class="metrics">' + metric("Followers", a.followers, null, w) + metric("Posts (7d)", a.posts_7d, null, w) + metric("Impressions (7d)", a.post_impressions_7d, null, w) +
        metric("Engagements (7d)", a.engagements_7d, null, w) + metric("Eng. rate", a.engagement_rate_7d, function (v) { return v + "%"; }, w) + metric("Total posts", a.posts_total, null, w) +
        metric("Profile visits", a.profile_visits, null, w) + metric("Link clicks", a.link_clicks, null, w) + metric("Account impressions", a.account_impressions, null, w) + "</div>" +
        (posts.length ? '<div class="bars">' + bars + '</div><div class="cap">Impressions per post, last ' + posts.length + " posts</div>" : '<div class="cap">No original posts yet</div>') +
        recent + "</div>";
    }).join("");
  }

  function chart(daily) {
    var d = range === "7d" ? daily.slice(-7) : daily;
    if (!d.length) return '<div class="cap">No daily data</div>';
    var W = 520, H = 130, P = 6, mx = 1;
    d.forEach(function (r) { if ((r.requests || 0) > mx) mx = r.requests; });
    function line(key) {
      return d.map(function (r, i) {
        var x = P + (d.length === 1 ? 0 : i * (W - 2 * P) / (d.length - 1));
        var y = H - P - (H - 2 * P) * (r[key] || 0) / mx;
        return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
      }).join(" ");
    }
    var area = line("requests") + " L" + (W - P) + " " + (H - P) + " L" + P + " " + (H - P) + " Z";
    return '<svg class="chart" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="Edge requests and edge page views"><path d="' + area + '" fill="rgba(0,90,255,.16)"/><path d="' + line("requests") +
      '" fill="none" stroke="#005aff" stroke-width="2.5"/><path d="' + line("page_views") + '" fill="none" stroke="#9747ff" stroke-width="2" stroke-dasharray="5 4"/></svg>' +
      '<div class="legend"><span><i class="req"></i>Edge requests</span><span><i class="pv"></i>Edge page views</span><span>' + esc(d[0].date) + " → " + esc(d[d.length - 1].date) + "</span></div>";
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
    return '<div class="cap">' + one("Edge requests", p.requests) + '</div><div class="cap">' + one("Edge page views", p.page_views) + "</div>";
  }
  function list(title, m, where, fmtKey) {
    if (!m) return "";
    if (!m.available) {
      NA.push([where + " · " + title, m.reason]);
      return '<div><h4>' + esc(title) + '</h4><div class="cap">Not available</div></div>';
    }
    var rows = m.value || [], mx = 1;
    rows.forEach(function (r) { if (r[1] > mx) mx = r[1]; });
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
    if (!rows.length) {
      $("sites").innerHTML = '<div class="card"><p class="empty">No websites in this snapshot.</p></div>';
      return;
    }
    $("sites").innerHTML = rows.map(function (s) {
      var w = s.name, r = range, win = s.windows || {};
      var head = '<div class="card site"><h3>' + esc(s.name) + '</h3><div class="cap">' + (s.data_since ? "Cloudflare data since " + esc(s.data_since) + ". " : "") + (s.error ? '<span class="err">' + esc(s.error) + "</span>" : "") + "</div>";
      var uniq = s["uniques_" + r] || {value: null, available: false, reason: "Summed daily uniques were not returned.", source: "edge", note: "Sum of daily unique IPs. Not deduplicated across the period."};
      var countries = s[r === "7d" ? "countries_7d" : "countries_30d"] || s.countries_7d;
      var countryLabel = r === "7d" ? "Countries (edge requests, 7 days)" : "Countries (edge requests, 30 days)";
      var sampledNote = (s.rum_status && s.rum_status !== "ok" && s.rum_status_detail) ? s.rum_status_detail : "Sampled visits are Web Analytics beacon events, not edge requests.";
      var probes = (s.bot_probe_requests_7d && s.bot_probe_requests_7d.available) ? " " + num(s.bot_probe_requests_7d.value) + " WordPress/PHP probe hits are hidden from Top pages." : "";
      return head + chart(s.daily || []) +
        '<div class="group-label edge"><i></i>Edge · HTTP at Cloudflare</div><div class="metrics">' +
        metric("Requests", s["requests_" + r], null, w) + metric("Page views", s["page_views_" + r], null, w) + metric("Summed daily uniques", uniq, null, w) +
        metric("Bandwidth", s["bandwidth_bytes_" + r], bytes, w) + metric("Threats blocked", s["threats_" + r], null, w) + metric("Probe hits (7 days)", s.bot_probe_requests_7d, null, w) + "</div>" +
        '<div class="group-label sampled"><i></i>Sampled · Web Analytics</div><div class="metrics">' +
        metric("Visits (" + r + ")", rumKey(s, "rum_visits"), null, w) + metric("Page loads (" + r + ")", rumKey(s, "rum_pageloads"), null, w) + "</div>" +
        '<div class="compare">' + shareLine(win.edge_requests) + shareLine(win.edge_page_views) + shareLine(win.rum_visits) + shareLine(win.rum_pageloads) + prevLine(s) + "</div>" +
        '<div class="lists">' + list("Top pages (edge requests, rolling 7 days)", s.top_paths_7d, w) + list(countryLabel, countries, w) +
        list("Referrers (sampled, " + r + ")", rumKey(s, "rum_referrers"), w) + list("Pages (sampled, " + r + ")", rumKey(s, "rum_top_pages"), w) + "</div>" +
        '<div class="cap">Edge requests count every HTTP request, including bots and assets. Edge page views are the HTML class at the edge, not browser visits. ' +
        esc((uniq.available && uniq.note) ? uniq.note : "Summed daily uniques are not deduplicated across the 7 or 30 days.") + probes + " " + esc(sampledNote) + "</div></div>";
    }).join("");
  }
  function paint() {
    NA = [];
    var t = snap.generated_at ? new Date(snap.generated_at) : null;
    var when = "Waiting for first refresh";
    if (t && !isNaN(t.getTime())) {
      try { when = "Updated " + t.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
      catch (e) { when = "Updated " + t.toLocaleString(); }
    }
    $("updated").textContent = when;
    hero();
    xcards();
    sites();
    (snap.errors || []).forEach(function (e) { NA.push(["Refresh error", e]); });
    var groups = {}, order = [];
    NA.forEach(function (n) {
      var parts = String(n[0]).split(" · "), k = (parts[1] || parts[0]) + "|" + n[1];
      if (!groups[k]) { groups[k] = { label: parts[1] || parts[0], reason: n[1], who: [] }; order.push(k); }
      if (parts[1] && groups[k].who.indexOf(parts[0]) < 0) groups[k].who.push(parts[0]);
    });
    var rows = order.map(function (k) {
      var g = groups[k];
      return [g.label + (g.who.length ? " (" + g.who.join(", ") + ")" : ""), g.reason];
    });
    $("na").innerHTML = rows.length ? rows.map(function (n) {
      return '<div class="row"><span>' + esc(n[0]) + "</span><span>" + esc(n[1]) + "</span></div>";
    }).join("") : '<div class="cap">Everything configured is available.</div>';
  }
  function render() {
    var root = scrollRoot();
    var top = readTop(root);
    dropContentFocus();
    paint();
    writeTop(root, top);
    requestAnimationFrame(function () {
      writeTop(root, top);
      requestAnimationFrame(function () { writeTop(root, top); });
    });
  }
  function onRange(b) {
    document.querySelectorAll(".range button").forEach(function (x) {
      var on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-selected", on ? "true" : "false");
    });
    range = b.dataset.range;
    if (snap) render();
  }
  document.querySelectorAll(".range button").forEach(function (b) {
    b.addEventListener("click", function () { onRange(b); });
  });
  function tickClock() {
    var el = $("clock");
    if (!el) return;
    var d = new Date();
    try { el.textContent = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
    catch (e) { el.textContent = d.toLocaleTimeString(); }
  }
  function load() {
    fetch("api/snapshot", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
      snap = j;
      render();
    }).catch(function () {
      var el = $("updated");
      if (el) el.textContent = "Can't reach the add-on";
    });
  }
  tickClock();
  setInterval(tickClock, 30 * 1000);
  load();
  setInterval(load, 5 * 60 * 1000);
})();
