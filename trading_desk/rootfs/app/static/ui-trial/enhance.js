/* Trial only. Labels each narrow-table cell from its column header so 390px
   can stack rows without clipping names. No network calls. */
(function () {
  "use strict";
  function label(table) {
    var ths = table.querySelectorAll("thead th"), heads = [], i;
    for (i = 0; i < ths.length; i++) heads.push(ths[i].innerText.replace(/\s+/g, " ").trim());
    if (!heads.length) return;
    var rows = table.querySelectorAll("tbody tr"), r, cells, c;
    for (r = 0; r < rows.length; r++) {
      cells = rows[r].children;
      if (cells.length !== heads.length) continue;
      for (c = 0; c < cells.length; c++) {
        if (cells[c].getAttribute("data-l") !== heads[c]) cells[c].setAttribute("data-l", heads[c]);
      }
    }
  }
  function run() {
    var tables = document.querySelectorAll("#mainview table, #deskview table, #assetsview table"), i;
    for (i = 0; i < tables.length; i++) label(tables[i]);
  }
  var obs = new MutationObserver(run);
  ["mainview", "deskview", "assetsview"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) obs.observe(el, { childList: true, subtree: true });
  });
  run();
})();
