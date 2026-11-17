// The front page's portfolio charts (data/front.html, data/front.py). They render on the server and work without scripts: each chart is
// an image named by its title, and "Show as a table" under it lists the same values. This adds values on hover and from the keyboard.
//
// Each chart becomes a group with one tab stop. The left and right (or up and down) arrow keys, Home and End move through its points
// (a year, or a PA); the point with the focus, or the one under the pointer, shows its values in a box over the chart, and the same text
// is announced through the page's one live region (window.cedarsAnnounce). Escape hides the box (1.4.13); the box stays while the pointer
// is over it.
(function () {
  "use strict";

  var count = 0;
  document.querySelectorAll(".front-trends figure[data-chart] .trend-plot").forEach(function (plot) {
    var points = Array.prototype.slice.call(plot.querySelectorAll("g.pt[data-tip]"));
    if (!points.length) return;
    var figure = plot.closest("figure");
    count += 1;
    var tip = document.createElement("p");
    tip.className = "trend-tip";
    tip.hidden = true;
    tip.setAttribute("aria-hidden", "true");   // what it says is announced; the box is for sighted readers
    plot.appendChild(tip);
    // One hint per chart, after its plots (the by-year chart has a second, shorter plot for narrow screens).
    var hint = figure.querySelector(".trend-hint");
    if (!hint) {
      hint = document.createElement("p");
      hint.className = "trend-hint";
      hint.id = "trend-hint-" + count;
      hint.textContent = "Point at the chart, or select it and use the arrow keys, to see each value.";
      var plots = figure.querySelectorAll(".trend-plot");
      plots[plots.length - 1].insertAdjacentElement("afterend", hint);
    }
    plot.tabIndex = 0;
    plot.setAttribute("role", "group");
    plot.setAttribute("aria-label", figure.getAttribute("data-chart") + ", " + points.length + " points; the table below lists the same values");
    plot.setAttribute("aria-describedby", hint.id);
    var svg = plot.querySelector("svg");
    svg.setAttribute("aria-hidden", "true");   // the group above names it now; its title would be read twice

    var active = -1, dismissed = false, overTip = false;

    function place(g, clientX) {
      var box = plot.getBoundingClientRect(), mark = g.getBoundingClientRect();
      var x = clientX == null ? mark.left + mark.width / 2 - box.left : clientX - box.left;
      tip.style.left = Math.max(0, Math.min(x + 12, box.width - tip.offsetWidth)) + "px";
      tip.style.top = Math.max(0, mark.top - box.top - tip.offsetHeight - 6) + "px";
    }
    function show(i, clientX, announce) {
      if (i < 0 || i >= points.length) return;
      points.forEach(function (p, j) { p.classList.toggle("active", j === i); });
      active = i;
      if (dismissed) return;
      tip.textContent = points[i].getAttribute("data-tip");
      tip.hidden = false;
      place(points[i], clientX);
      if (announce && window.cedarsAnnounce) window.cedarsAnnounce(tip.textContent);
    }
    function hide() {
      tip.hidden = true;
      points.forEach(function (p) { p.classList.remove("active"); });
    }

    points.forEach(function (g, i) {
      g.addEventListener("pointerenter", function (e) { dismissed = false; show(i, e.clientX, false); });
      g.addEventListener("pointermove", function (e) { if (active === i && !dismissed) place(g, e.clientX); });
    });
    plot.addEventListener("pointerleave", function () {
      setTimeout(function () { if (!overTip && document.activeElement !== plot) hide(); }, 50);
    });
    tip.addEventListener("pointerenter", function () { overTip = true; });
    tip.addEventListener("pointerleave", function () { overTip = false; if (document.activeElement !== plot) hide(); });
    plot.addEventListener("focus", function () { dismissed = false; show(active < 0 ? points.length - 1 : active, null, true); });
    plot.addEventListener("blur", hide);
    plot.addEventListener("keydown", function (e) {
      var to = null;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") to = Math.min(active + 1, points.length - 1);
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") to = Math.max(active - 1, 0);
      else if (e.key === "Home") to = 0;
      else if (e.key === "End") to = points.length - 1;
      else if (e.key === "Escape") { if (!tip.hidden) { dismissed = true; hide(); e.preventDefault(); } return; }
      if (to === null) return;
      e.preventDefault();
      dismissed = false;
      show(to, null, true);
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !tip.hidden && document.activeElement !== plot) { dismissed = true; hide(); } });
  });
})();
