(function () {
  "use strict";
  var team = "all";
  var shown = null;
  var banner = document.getElementById("banner");
  var teams = document.getElementById("teams");
  var updated = document.getElementById("updated");
  var buttons = Array.prototype.slice.call(document.querySelectorAll(".seg button"));
  var classes = {
    "Seattle Seahawks": "team-seahawks",
    "Navy Midshipmen": "team-navy",
    "Gonzaga Bulldogs": "team-gonzaga"
  };

  function allowedPostUrl(url, handle, id) {
    var parsed;
    try { parsed = new URL(url); } catch (err) { return false; }
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port || parsed.search || parsed.hash) return false;
    var host = parsed.hostname.toLowerCase();
    if (host !== "x.com" && host !== "twitter.com") return false;
    var parts = parsed.pathname.split("/");
    if (parts.length !== 4 || parts[0] !== "" || parts[2] !== "status") return false;
    if (parts[1].toLowerCase() !== String(handle).toLowerCase()) return false;
    if (parts[3] !== String(id)) return false;
    return true;
  }

  function addText(parent, tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text;
    parent.appendChild(node);
    return node;
  }

  function messageClass(text) {
    if (text.indexOf("Starter snapshot") === 0) return "starter";
    if (text.indexOf("Content age:") === 0) return "age";
    if (text.indexOf("Source error") === 0 || text.indexOf("Inbox rejected") === 0) return "error";
    if (text.indexOf("Note:") === 0) return "note";
    return "";
  }

  function render(snapshot) {
    shown = snapshot;
    updated.textContent = snapshot.collected_at_et
      ? "Content collected " + snapshot.collected_at_et
      : "No content collection time";
    banner.textContent = "";
    (snapshot.messages || []).forEach(function (line) {
      addText(banner, "p", messageClass(line), line);
    });
    teams.textContent = "";
    (snapshot.teams || []).forEach(function (row) {
      var section = document.createElement("section");
      section.className = "team " + (classes[row.team] || "");
      if (team !== "all" && team !== row.team) section.hidden = true;
      var head = document.createElement("div");
      head.className = "team-head";
      var title = document.createElement("div");
      addText(title, "h2", "", row.team + " · " + row.sport);
      if (row.status === "error") addText(title, "p", "state error", "Source error");
      head.appendChild(title);
      var count = document.createElement("div");
      addText(count, "p", "figure", String(row.count));
      addText(count, "p", "figure-label", "posts in this snapshot");
      head.appendChild(count);
      section.appendChild(head);
      if (row.detail) addText(section, "p", "detail", row.detail);
      if (row.last_success_at_et) addText(section, "p", "time", "Content collected " + row.last_success_at_et);
      var posts = (snapshot.items || []).filter(function (item) { return item.team === row.team; });
      if (!posts.length && row.status !== "error") addText(section, "p", "detail", "No posts in this snapshot.");
      posts.forEach(function (item) {
        var card = document.createElement("article");
        card.className = "card";
        addText(card, "p", "role " + (item.author_role === "official" ? "official" : "reporter"), item.role_label || "Attributed");
        if (item.older) addText(card, "p", "older", "Older news");
        addText(card, "h3", "", item.summary || "");
        addText(card, "p", "by", (item.author_name || "") + " @" + (item.author_handle || ""));
        addText(card, "p", "time", "Published " + (item.published_at_et || "unknown time"));
        addText(card, "p", "source", item.source_text || "");
        if (allowedPostUrl(item.url, item.author_handle, item.id)) {
          var link = document.createElement("a");
          link.className = "view";
          link.href = item.url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.referrerPolicy = "no-referrer";
          link.textContent = "View post on X";
          card.appendChild(link);
        } else {
          addText(card, "p", "detail", "Post link blocked");
        }
        section.appendChild(card);
      });
      teams.appendChild(section);
    });
  }

  function select(button) {
    team = button.getAttribute("data-team");
    buttons.forEach(function (item) {
      var on = item === button;
      item.classList.toggle("on", on);
      item.setAttribute("aria-pressed", on ? "true" : "false");
    });
    if (shown) render(shown);
  }

  buttons.forEach(function (button, index) {
    button.addEventListener("click", function () { select(button); });
    button.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault();
      var next = (index + (event.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length;
      buttons[next].focus();
      select(buttons[next]);
    });
  });

  function fail() {
    var text = shown
      ? "Can't read the snapshot. Showing the last page result. The collection time is unchanged."
      : "Can't read the snapshot. Nothing is shown. This is a page error, not a source collection.";
    if (!shown) {
      updated.textContent = "Snapshot unavailable";
      teams.textContent = "";
    }
    banner.textContent = "";
    addText(banner, "p", "error", text);
  }

  function load() {
    fetch("api/snapshot", {cache: "no-store"}).then(function (response) {
      if (!response.ok) throw new Error("status");
      return response.json();
    }).then(render).catch(fail);
  }

  load();
  setInterval(load, 60000);
})();
