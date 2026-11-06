/* Renders the view model from api/snapshot. Usage figures are not stored in this file. */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var DONUT = ["#e7ebf0", "#8eafc4", "#6a7c8c", "#556270"];
  var SEG = { "Chats": "chats", "Routines": "routines", "Bot-to-Bot": "b2b" };
  var loaded = false;

  function $(id) { return document.getElementById(id); }

  function endpoint(name) {
    var path = window.location.pathname;
    if (path.slice(-1) !== "/") path = path.replace(/[^/]*$/, "");
    return path + name;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function para(text, className) {
    var node = document.createElement("p");
    if (className) node.className = className;
    node.textContent = text;
    return node;
  }

  function show(id, text, on) {
    var node = $(id);
    node.hidden = !on;
    node.textContent = on ? text : "";
  }

  function legend(host, items) {
    clear(host);
    items.forEach(function (item) {
      var li = document.createElement("span");
      var sw = document.createElement("i");
      sw.className = "swatch " + item.className;
      li.appendChild(sw);
      li.appendChild(document.createTextNode(item.label));
      host.appendChild(li);
    });
  }

  function renderMetrics(metrics) {
    var host = $("metrics");
    clear(host);
    metrics.forEach(function (metric) {
      var card = document.createElement("article");
      card.className = "metric";
      var label = document.createElement("div");
      label.className = "label";
      label.textContent = metric.label;
      var value = document.createElement("div");
      value.className = "value";
      value.textContent = metric.value;
      var detail = document.createElement("div");
      detail.className = "detail";
      detail.textContent = metric.detail;
      card.appendChild(label);
      card.appendChild(value);
      card.appendChild(detail);
      host.appendChild(card);
    });
  }

  function renderDonut(donut) {
    var host = $("donut");
    clear(host);
    var wrap = document.createElement("div");
    wrap.className = "donut-wrap";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 220 220");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", donut.parts.map(function (part) {
      return part.label + " " + part.percent + "%";
    }).join(", "));
    var group = document.createElementNS(NS, "g");
    group.setAttribute("transform", "rotate(-90 110 110)");
    var radius = 78;
    var circ = 2 * Math.PI * radius;
    var total = donut.parts.reduce(function (sum, part) { return sum + part.percent; }, 0) || 1;
    var offset = 0;
    var gap = donut.parts.length > 1 ? 3 : 0;
    donut.parts.forEach(function (part, index) {
      var length = (part.percent / total) * circ;
      var draw = Math.max(0, length - gap);
      var circle = document.createElementNS(NS, "circle");
      circle.setAttribute("cx", "110");
      circle.setAttribute("cy", "110");
      circle.setAttribute("r", String(radius));
      circle.setAttribute("fill", "none");
      circle.setAttribute("stroke", DONUT[index % DONUT.length]);
      circle.setAttribute("stroke-width", "18");
      circle.setAttribute("stroke-dasharray", draw + " " + (circ - draw));
      circle.setAttribute("stroke-dashoffset", String(-offset));
      var title = document.createElementNS(NS, "title");
      title.textContent = part.label + ": " + part.percent + "% of Grok Bot usage";
      circle.appendChild(title);
      group.appendChild(circle);
      offset += length;
    });
    svg.appendChild(group);
    var center = document.createElementNS(NS, "text");
    center.setAttribute("x", "110");
    center.setAttribute("y", "104");
    center.setAttribute("text-anchor", "middle");
    center.setAttribute("fill", "#f4f6f8");
    center.setAttribute("font-size", "28");
    center.setAttribute("font-weight", "300");
    center.textContent = donut.center;
    var caption = document.createElementNS(NS, "text");
    caption.setAttribute("x", "110");
    caption.setAttribute("y", "126");
    caption.setAttribute("text-anchor", "middle");
    caption.setAttribute("fill", "#b7c0c8");
    caption.setAttribute("font-size", "11");
    caption.setAttribute("letter-spacing", "0.12em");
    caption.textContent = donut.center_caption.toUpperCase();
    svg.appendChild(center);
    svg.appendChild(caption);
    wrap.appendChild(svg);
    var list = document.createElement("div");
    list.className = "legend";
    donut.parts.forEach(function (part, index) {
      var item = document.createElement("span");
      var sw = document.createElement("i");
      sw.className = "swatch d" + (index % 4);
      item.appendChild(sw);
      item.appendChild(document.createTextNode(part.label + " " + part.percent + "%"));
      list.appendChild(item);
    });
    wrap.appendChild(list);
    host.appendChild(wrap);
  }

  function renderBars(view, selected) {
    var host = $("bars");
    clear(host);
    var rows = selected === "All bots" ? view.bots : view.bots.filter(function (bot) {
      return bot.name === selected;
    });
    var max = 1;
    rows.forEach(function (bot) {
      var weight = bot.share_percent || 0;
      if (weight > max) max = weight;
    });
    rows.forEach(function (bot) {
      var row = document.createElement("div");
      row.className = "bot-row";
      var head = document.createElement("div");
      head.className = "bot-head";
      var name = document.createElement("span");
      name.textContent = bot.name;
      var share = document.createElement("span");
      share.textContent = bot.share_label;
      share.className = "share" + (bot.share_percent == null ? " small" : "");
      head.appendChild(name);
      head.appendChild(share);
      row.appendChild(head);
      if (bot.share_percent != null) {
        var actSum = bot.segments.reduce(function (sum, seg) { return sum + (seg.percent || 0); }, 0);
        var basis = Math.max(bot.share_percent, actSum, 1);
        var bar = document.createElement("div");
        bar.className = "bar";
        bar.style.width = (100 * basis / max) + "%";
        bot.segments.forEach(function (seg) {
          if (!seg.percent) return;
          var piece = document.createElement("span");
          piece.className = "seg " + (SEG[seg.name] || "");
          piece.style.width = (100 * seg.percent / basis) + "%";
          piece.title = bot.name + " → " + seg.name + ": " + seg.percent + "% of all Grok Bot usage";
          bar.appendChild(piece);
        });
        row.appendChild(bar);
      }
      row.appendChild(para(bot.activity_line, "activity-line"));
      host.appendChild(row);
    });
  }

  function ribbon(x1, y1, x2, y2, h) {
    var cx = (x1 + x2) / 2;
    return "M " + x1 + " " + y1 +
      " C " + cx + " " + y1 + ", " + cx + " " + y2 + ", " + x2 + " " + y2 +
      " L " + x2 + " " + (y2 + h) +
      " C " + cx + " " + (y2 + h) + ", " + cx + " " + (y1 + h) + ", " + x1 + " " + (y1 + h) + " Z";
  }

  function svgEl(name, attrs) {
    var node = document.createElementNS(NS, name);
    Object.keys(attrs).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    return node;
  }

  function flowName(name) {
    return name.indexOf("Director, ") === 0 ? name.slice("Director, ".length) : name;
  }

  function renderFlow(view, selected) {
    var host = $("flow");
    clear(host);
    var pool = selected === "All bots" ? view.bots : view.bots.filter(function (bot) {
      return bot.name === selected;
    });
    var shown = pool.filter(function (bot) { return bot.share_percent != null; });
    if (!shown.length) {
      var who = selected === "All bots" ? "These bots are" : selected + " is";
      host.appendChild(para(who + " under 1%, so no ribbon is drawn.", "fine"));
      return;
    }
    var scale = 3.8;
    var gap = 22;
    var top = 40;
    var sourceX = 36;
    var botX = 500;
    var actX = 900;
    var total = shown.reduce(function (sum, bot) { return sum + bot.share_percent; }, 0);
    var body = shown.reduce(function (sum, bot) { return sum + bot.share_percent * scale; }, 0) + gap * (shown.length - 1);
    var sums = view.activities.map(function (name) {
      return shown.reduce(function (sum, bot) {
        var seg = bot.segments.filter(function (item) { return item.name === name; })[0];
        return sum + ((seg && seg.percent) || 0);
      }, 0);
    });
    var rightGap = 40;
    var rightBody = sums.reduce(function (sum, value) {
      return sum + (value > 0 ? value * scale : 18) + rightGap;
    }, -rightGap);
    var height = top + Math.max(body, rightBody) + 16;
    var sourceH = total * scale;
    var sourceY = top + Math.max(0, (body - sourceH) / 2);
    var y = top;
    var sy = sourceY;
    var nodes = shown.map(function (bot) {
      var h = bot.share_percent * scale;
      var node = { bot: bot, y: y, h: h, sy: sy };
      y += h + gap;
      sy += h;
      return node;
    });
    var rightY = top + Math.max(0, (body - rightBody) / 2);
    var rightYs = sums.map(function (value) {
      var here = rightY;
      rightY += (value > 0 ? value * scale : 18) + rightGap;
      return here;
    });
    var colors = ["#d5dbe2", "#8eafc4", "#6a7c8c"];

    function marked(attrs, text) {
      var node = svgEl("text", attrs);
      node.setAttribute("stroke", "#16191d");
      node.setAttribute("stroke-width", "4");
      node.setAttribute("paint-order", "stroke");
      node.setAttribute("stroke-linejoin", "round");
      node.textContent = text;
      return node;
    }

    var svg = svgEl("svg", {
      viewBox: "0 0 1120 " + height,
      role: "img",
      "aria-label": "Usage flow from this week through each bot to chats, routines, and bot-to-bot. Ribbon widths are reported whole percentages."
    });
    svg.appendChild(svgEl("text", { x: "8", y: "18", fill: "#b7c0c8", "font-size": "11", "letter-spacing": "1.6" })).textContent = "THIS WEEK";
    svg.appendChild(svgEl("text", { x: "430", y: "18", fill: "#b7c0c8", "font-size": "11", "letter-spacing": "1.6" })).textContent = "BY BOT";
    svg.appendChild(svgEl("text", { x: "918", y: "18", fill: "#b7c0c8", "font-size": "11", "letter-spacing": "1.6" })).textContent = "ACTIVITY";

    svg.appendChild(svgEl("rect", {
      x: String(sourceX), y: String(sourceY), width: "8", height: String(sourceH), fill: "#8eafc4"
    }));
    svg.appendChild(svgEl("text", {
      x: "8", y: String(Math.max(16, sourceY - 8)), fill: "#f4f6f8", "font-size": "13", "font-weight": "300"
    })).textContent = total + "%";

    nodes.forEach(function (node) {
      var path = svgEl("path", {
        d: ribbon(sourceX + 8, node.sy, botX, node.y, node.h),
        fill: "#8eafc4",
        opacity: "0.24"
      });
      var title = document.createElementNS(NS, "title");
      title.textContent = node.bot.name + ": " + node.bot.share_percent + "% of all Grok Bot usage";
      path.appendChild(title);
      svg.appendChild(path);
    });

    var cursors = rightYs.slice();
    nodes.forEach(function (node) {
      var local = 0;
      node.bot.segments.forEach(function (seg, index) {
        if (!seg.percent) return;
        var from = node.y + local * scale;
        local += seg.percent;
        var to = cursors[index];
        cursors[index] += seg.percent * scale;
        var path = svgEl("path", {
          d: ribbon(botX + 8, from, actX, to, seg.percent * scale),
          fill: colors[index],
          opacity: "0.32"
        });
        var title = document.createElementNS(NS, "title");
        title.textContent = node.bot.name + " → " + seg.name + ": " + seg.percent + "% of all Grok Bot usage";
        path.appendChild(title);
        svg.appendChild(path);
      });
    });

    nodes.forEach(function (node) {
      svg.appendChild(svgEl("rect", {
        x: String(botX), y: String(node.y), width: "8", height: String(Math.max(node.h, 2)), fill: "#c5d0d8"
      }));
    });
    view.activities.forEach(function (name, index) {
      var sum = sums[index];
      var barH = sum > 0 ? sum * scale : 0;
      if (barH > 0) {
        svg.appendChild(svgEl("rect", {
          x: String(actX), y: String(rightYs[index]), width: "8", height: String(barH), fill: colors[index]
        }));
      }
      svg.appendChild(marked({
        x: String(actX + 16),
        y: String(rightYs[index] + Math.max(barH, 12) / 2),
        fill: "#f4f6f8",
        "font-size": "12",
        "dominant-baseline": "middle"
      }, sum > 0 ? name : name + " <1%"));
    });
    nodes.forEach(function (node) {
      svg.appendChild(marked({
        x: String(botX - 10),
        y: String(node.y + node.h / 2),
        fill: "#f4f6f8",
        "font-size": "12",
        "text-anchor": "end",
        "dominant-baseline": "middle"
      }, flowName(node.bot.name) + " · " + node.bot.share_label));
    });

    host.appendChild(svg);
  }

  function renderBelow(view, selected) {
    var host = $("below");
    var section = $("below-section");
    clear(host);
    var lines = view.below.filter(function (line) {
      return selected === "All bots" || line.bot === selected;
    });
    section.hidden = lines.length === 0;
    lines.forEach(function (line) { host.appendChild(para(line.text, "below-line")); });
  }

  function fillFocus(view) {
    var select = $("focus");
    var current = select.value || "All bots";
    var names = ["All bots"].concat(view.bots.map(function (bot) { return bot.name; }));
    if (names.indexOf(current) === -1) current = "All bots";
    clear(select);
    names.forEach(function (name) {
      var option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      if (name === current) option.selected = true;
      select.appendChild(option);
    });
    return current;
  }

  function render(payload) {
    var view = payload.view;
    $("kicker").textContent = view.kicker;
    $("title").textContent = view.title;
    $("summary").textContent = view.summary;
    $("provenance").textContent = view.provenance_line;
    $("provenance-detail").textContent = view.provenance_detail;
    $("reread").textContent = view.reread_label;
    show("warning", payload.warning || "", Boolean(payload.warning));
    show("error", "", false);
    renderMetrics(view.metrics);
    renderDonut(view.donut);
    var notes = $("notes");
    clear(notes);
    view.notes.forEach(function (note) { notes.appendChild(para(note, "note")); });
    $("flow-caption").textContent = view.flow_caption;
    $("flow-note").textContent = view.flow_note;
    $("flow-omit").textContent = view.flow_omit_note;
    var items = view.activities.map(function (name) {
      return { className: SEG[name], label: name };
    });
    legend($("bar-legend"), items);
    legend($("flow-legend"), items);
    var selected = fillFocus(view);
    renderBars(view, selected);
    renderFlow(view, selected);
    renderBelow(view, selected);
    var context = $("context");
    clear(context);
    view.context.forEach(function (line) { context.appendChild(para(line)); });
    document.title = view.title;
    document.body.dataset.ready = "1";
    document.body.dataset.provenance = view.provenance;
    loaded = true;
  }

  function focusChanged() {
    if (!window.__usagePayload) return;
    var selected = $("focus").value || "All bots";
    renderBars(window.__usagePayload.view, selected);
    renderFlow(window.__usagePayload.view, selected);
    renderBelow(window.__usagePayload.view, selected);
  }

  function fail(message) {
    show("error", message, true);
    if (!loaded) {
      $("provenance").textContent = "The snapshot file could not be read.";
      clear($("metrics"));
    }
  }

  function load(fromClick) {
    $("reread").disabled = true;
    fetch(endpoint("api/snapshot"), { cache: "no-store", headers: { "Accept": "application/json" } })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        if (!payload || !payload.ok || !payload.view) {
          fail((payload && payload.error) || "The snapshot file could not be read.");
          return;
        }
        window.__usagePayload = payload;
        render(payload);
        if (fromClick) show("reread-result", payload.view.reread_result, true);
      })
      .catch(function () {
        fail("The snapshot file could not be read.");
      })
      .then(function () {
        $("reread").disabled = false;
      });
  }

  $("focus").addEventListener("change", focusChanged);
  $("reread").addEventListener("click", function () { load(true); });
  load(false);
})();
