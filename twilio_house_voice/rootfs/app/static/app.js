"use strict";
(function () {
  var state = { recent: [], selected: null };
  var $ = function (id) { return document.getElementById(id); };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function when(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  function secs(v) {
    var n = Math.floor(Number(v) || 0);
    if (n < 60) return n + (n === 1 ? " second" : " seconds");
    var m = Math.floor(n / 60), r = n % 60;
    return m + (m === 1 ? " minute" : " minutes") + (r ? " " + r + (r === 1 ? " second" : " seconds") : "");
  }
  function fill(list, items, render, emptyText) {
    list.replaceChildren();
    if (!items.length) { list.appendChild(el("li", "empty", emptyText)); return; }
    items.forEach(function (it, i) { list.appendChild(render(it, i)); });
  }
  function check(ok, label, detail) {
    var li = el("li", "check " + (ok ? "ok" : "miss"));
    li.appendChild(el("span", "dot"));
    li.appendChild(el("b", null, label));
    if (detail) li.appendChild(el("span", "detail", detail));
    return li;
  }
  function showDetail(c) {
    state.selected = c ? c.call_id : null;
    document.body.classList.toggle("has-detail", !!c);
    $("placeholder").hidden = !!c;
    $("detail-body").hidden = !c;
    Array.prototype.forEach.call(document.querySelectorAll("#recent .row"), function (r) {
      r.classList.toggle("active", !!c && r.dataset.id === c.call_id);
    });
    if (!c) return;
    $("d-from").textContent = c.from || "Unknown caller";
    $("d-when").textContent = when(c.ended_at);
    var dl = $("d-facts");
    dl.replaceChildren();
    [["Duration", secs(c.duration_s)], ["Recording", c.audio ? "Saved" : "None"],
     ["Home Assistant", c.ha_ok ? "Delivered" : "Pending"], ["Caller ID check", c.stir || "None"],
     ["Screening", c.trust || "screen"], ["Bridge", c.bridge || ""]].forEach(function (kv) {
      dl.appendChild(el("dt", null, kv[0]));
      dl.appendChild(el("dd", null, kv[1]));
    });
  }
  function render(d) {
    $("version").textContent = "v" + d.version + " · " + (d.bridge === "live" ? "agent live" : "recording only") +
      " · " + (d.calls_handled || 0) + " handled";
    fill($("checks"), [
      [d.twilio_configured, "Phone line", d.signature_validation ? "Signed requests only" : "Signature checks off"],
      [d.xai_configured, "Assistant", d.bridge === "live" ? (d.voice || "") : "Records, does not speak"],
      [d.public_base_url_set, "Public address", ""],
      [d.webhooks_configured, "Home Assistant", d.phone_masked ? "Line " + d.phone_masked : ""]
    ], function (c) { return check(c[0], c[1], c[2]); }, "");
    fill($("active"), d.active_calls || [], function (c) {
      var li = el("li", "row live");
      li.appendChild(el("span", "pulse"));
      li.appendChild(el("b", null, c.from || "Unknown caller"));
      li.appendChild(el("span", "detail", c.xai_connected ? "Assistant connected" : "Recording"));
      return li;
    }, "No active calls");
    state.recent = d.recent || [];
    fill($("recent"), state.recent, function (c) {
      var li = el("li", "row");
      li.dataset.id = c.call_id;
      var btn = el("button", "row-btn");
      btn.type = "button";
      btn.appendChild(el("b", null, c.from || "Unknown caller"));
      btn.appendChild(el("span", "detail", when(c.ended_at) + " · " + secs(c.duration_s)));
      btn.addEventListener("click", function () { showDetail(c); });
      li.appendChild(btn);
      return li;
    }, "No calls yet");
    if (state.selected) {
      var cur = state.recent.filter(function (c) { return c.call_id === state.selected; })[0];
      showDetail(cur || null);
    }
  }
  async function tick() {
    try {
      var res = await fetch("api/status", { cache: "no-store" });
      render(await res.json());
    } catch (e) {
      $("version").textContent = "Status unavailable";
    }
  }
  $("back").addEventListener("click", function () { showDetail(null); });
  tick();
  setInterval(tick, 2000);
})();
