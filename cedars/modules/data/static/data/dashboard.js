// The portfolio dashboard's pages (data/dashboard.html). They render on the server and work without scripts; this adds two things.
//
// 1. Chart values on hover and focus. Without this script a chart is one image whose values are in the table below it. With it, each
//    chart is a group with one tab stop: the up and down arrow keys (Home, End) move through its rows, and the row with the focus, or the
//    row under the pointer, shows its values beside it (1.4.13: the values stay while the pointer is over them, and Escape hides them).
//    Each row's values are the same text a screen reader reads when the row takes the focus, so nothing is shown that is not also said.
// 2. Sections that fold away (native <details>: Filters and view, each chart and each table). Whether each is open is remembered in this
//    browser: the filters for every dashboard page, a chart or table for its own page and place on it. Storage that is blocked or cleared
//    simply leaves every section open.
(function () {
  "use strict";

  // --- sections that fold away
  var KEY = "cedars.dashboard.sections";
  var saved = {};
  try { saved = JSON.parse(window.localStorage.getItem(KEY) || "{}") || {}; } catch (e) { saved = {}; }
  function save() { try { window.localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) { /* private mode: nothing kept */ } }

  document.querySelectorAll("details.dash-part[data-part]").forEach(function (d) {
    var key = d.getAttribute("data-part");
    // A view that could not be shown keeps its controls open, whatever was remembered, so the way back is in sight.
    if (key in saved && !d.hasAttribute("data-keep-open")) d.open = saved[key];
    d.addEventListener("toggle", function () {
      if (d.open) delete saved[key]; else saved[key] = false;   // open is the default, so only folded sections are kept
      save();
    });
  });

  // --- chart values
  var tipCount = 0;
  document.querySelectorAll(".chart[data-chart]").forEach(function (chart) {
    var list = chart.querySelector(".chart-rows");
    var rows = Array.prototype.filter.call(list ? list.children : [], function (li) { return li.querySelector(".chart-tip"); });
    if (!rows.length) return;
    var hint = chart.querySelector(".chart-hint");
    tipCount += 1;
    hint.id = "chart-hint-" + tipCount;
    hint.hidden = false;
    chart.setAttribute("role", "group");
    chart.setAttribute("aria-label", chart.getAttribute("data-name") + ", " + rows.length + " row" + (rows.length === 1 ? "" : "s") +
      "; the table below lists the same values");
    chart.setAttribute("aria-describedby", hint.id);
    list.removeAttribute("aria-hidden");
    rows.forEach(function (li, i) { li.tabIndex = i === 0 ? 0 : -1; });

    var pointed = null, focused = null, dismissed = false;

    function hide(li) {
      if (!li) return;
      var tip = li.querySelector(".chart-tip");
      li.classList.remove("chart-active");
      tip.classList.remove("shown");
      tip.classList.add("sr-only");
      tip.style.left = "";
    }
    function show(li, clientX) {
      rows.forEach(function (r) { if (r !== li) hide(r); });
      if (dismissed) return;
      var tip = li.querySelector(".chart-tip");
      li.classList.add("chart-active");
      tip.classList.remove("sr-only");
      tip.classList.add("shown");
      // Beside the pointer, or at the start of the bars for the keyboard, kept inside the row.
      var box = li.getBoundingClientRect(), bars = li.querySelector(".chart-bars").getBoundingClientRect();
      var x = clientX == null ? bars.left - box.left : clientX - box.left + 12;
      tip.style.left = Math.max(0, Math.min(x, box.width - tip.offsetWidth)) + "px";
    }
    function update() {
      var li = focused || pointed;
      if (li) show(li, li === pointed ? lastX : null); else rows.forEach(hide);
    }
    function mark(target) {
      var series = target && target.getAttribute && target.getAttribute("data-series");
      rows.forEach(function (li) {
        li.querySelectorAll(".tip-row").forEach(function (r) { r.classList.toggle("hot", li === pointed && r.getAttribute("data-series") === series); });
      });
    }

    var lastX = null;
    rows.forEach(function (li) {
      li.addEventListener("pointerenter", function (e) { pointed = li; lastX = e.clientX; dismissed = false; update(); });
      li.addEventListener("pointermove", function (e) {
        if (pointed !== li) return;
        // Over the values themselves they stay put, so the pointer can rest on them (1.4.13 hoverable).
        if (!e.target.closest(".chart-tip")) { lastX = e.clientX; update(); mark(e.target); }
      });
      li.addEventListener("pointerleave", function () { if (pointed === li) { pointed = null; mark(null); update(); } });
      li.addEventListener("focus", function () {
        rows.forEach(function (r) { r.tabIndex = r === li ? 0 : -1; });
        focused = li; dismissed = false; update();
      });
      li.addEventListener("blur", function () { if (focused === li) { focused = null; update(); } });
    });

    list.addEventListener("keydown", function (e) {
      var i = rows.indexOf(document.activeElement);
      if (i < 0) return;
      var to = null;
      if (e.key === "ArrowDown" || e.key === "ArrowRight") to = Math.min(i + 1, rows.length - 1);
      else if (e.key === "ArrowUp" || e.key === "ArrowLeft") to = Math.max(i - 1, 0);
      else if (e.key === "Home") to = 0;
      else if (e.key === "End") to = rows.length - 1;
      else if (e.key === "Escape" && (focused || pointed)) { dismissed = true; rows.forEach(hide); e.preventDefault(); return; }
      if (to === null) return;
      e.preventDefault();
      rows[to].focus();
    });
    // Escape also hides values shown by the pointer alone, wherever the focus is.
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && pointed && !focused) { dismissed = true; rows.forEach(hide); }
    });
  });
})();
