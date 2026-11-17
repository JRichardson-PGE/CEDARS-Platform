// The walkthrough's charts (calculations/_walk_chart.html) render on the server and work without scripts: each is one image named by its
// summary, the numbers are in the table under it, and each year's values are its band's <title>. This adds a tooltip for each year (month,
// hour) of a chart:
// - with the pointer: the band under it shows its values beside it. They stay while the pointer rests on them (hoverable), a tap on a touch
//   screen keeps them until the next tap elsewhere, and Escape hides them.
// - with the keyboard: the chart is one tab stop; once it has the focus the left and right arrow keys (Home, End) move from year to year,
//   showing each year's values and saying them through the page's one live region (window.cedarsAnnounce). Escape hides them.
// The words shown are the words spoken, and the same values are in the table under the chart.
(function () {
  "use strict";

  document.querySelectorAll(".walk-chart").forEach(function (fig) {
    var plot = fig.querySelector(".walk-chart-plot[data-slots]");
    var tip = fig.querySelector(".walk-tip");
    var list = fig.querySelector(".walk-tips");
    if (!plot || !tip || !list) return;
    var box = fig.querySelector(".walk-chart-box");
    var items = Array.prototype.slice.call(list.children);
    var hits = Array.prototype.slice.call(plot.querySelectorAll("rect.hit"));
    var bands = plot.querySelectorAll("rect.band");
    var pts = plot.querySelectorAll("g.pts");
    if (!items.length || items.length !== hits.length) return;

    var hint = fig.querySelector(".walk-chart-hint");
    if (hint) {
      hint.hidden = false;
      plot.setAttribute("aria-describedby", hint.id);
    }
    // The <title>s are the tooltip without scripts; with it they would show a second, native tooltip on top of this one.
    hits.forEach(function (h) {
      var t = h.querySelector("title");
      if (t) t.remove();
    });

    var current = -1;   // the slot whose values are shown, -1 for none
    var keyIndex = 0;   // where the arrow keys start
    var pointed = false, focused = false, pinned = false, hideTimer = null;

    function mark(i) {
      [bands, pts].forEach(function (group) {
        Array.prototype.forEach.call(group, function (el) {
          el.classList.toggle("on", Number(el.getAttribute("data-i")) === i);
        });
      });
    }

    function hide() {
      current = -1;
      tip.hidden = true;
      tip.classList.remove("shown");
      mark(-1);
    }

    function place(i) {
      var outer = box.getBoundingClientRect();
      var band = hits[i].getBoundingClientRect();
      var w = tip.offsetWidth, gap = 2;
      // Beside the band, on its right when it fits and on its left otherwise, kept inside the chart's box.
      var x = band.right - outer.left + gap;
      if (x + w > outer.width) x = band.left - outer.left - gap - w;
      tip.style.left = Math.max(0, Math.min(x, outer.width - w)) + "px";
      tip.style.top = Math.max(0, band.top - outer.top) + "px";
    }

    function show(i) {
      clearTimeout(hideTimer);
      if (i === current) return;
      current = i;
      tip.innerHTML = items[i].innerHTML;
      tip.hidden = false;
      tip.classList.add("shown");
      mark(i);
      place(i);
    }

    // Keeps the year picked with the keyboard in sight when the chart scrolls sideways (a phone, or 200% zoom).
    function reveal(i) {
      var view = plot.getBoundingClientRect(), band = hits[i].getBoundingClientRect();
      if (band.left < view.left) plot.scrollLeft -= view.left - band.left + 8;
      else if (band.right > view.right) plot.scrollLeft += band.right - view.right + 8;
    }

    function pick(i) {
      keyIndex = i;
      reveal(i);
      current = -1;
      show(i);
      if (window.cedarsAnnounce) window.cedarsAnnounce(items[i].getAttribute("data-text"));
    }

    plot.addEventListener("pointerover", function (e) {
      var hit = e.target.closest && e.target.closest("rect.hit");
      if (!hit) return;
      pointed = true;
      pinned = e.pointerType === "touch";
      keyIndex = Number(hit.getAttribute("data-i"));
      show(keyIndex);
    });
    function leaveSoon() {
      if (pinned || focused) return;
      clearTimeout(hideTimer);
      hideTimer = setTimeout(function () { if (!pointed) hide(); }, 200);
    }
    plot.addEventListener("pointerleave", function () { pointed = false; leaveSoon(); });
    // The values stay while the pointer is over them.
    tip.addEventListener("pointerenter", function () { pointed = true; clearTimeout(hideTimer); });
    tip.addEventListener("pointerleave", function () { pointed = false; leaveSoon(); });
    // A tap anywhere else hides the values a tap showed.
    document.addEventListener("pointerdown", function (e) {
      if (pinned && !box.contains(e.target)) { pinned = false; if (!focused) hide(); }
    });

    plot.addEventListener("focus", function () {
      focused = true;
      show(Math.min(keyIndex, items.length - 1));
    });
    plot.addEventListener("blur", function () {
      focused = false;
      if (!pointed && !pinned) hide();
    });
    plot.addEventListener("keydown", function (e) {
      var to = null;
      var at = current >= 0 ? current : keyIndex;
      if (e.key === "ArrowRight") to = Math.min(at + 1, items.length - 1);
      else if (e.key === "ArrowLeft") to = Math.max(at - 1, 0);
      else if (e.key === "Home") to = 0;
      else if (e.key === "End") to = items.length - 1;
      else if (e.key === "Escape" && current >= 0) { hide(); e.preventDefault(); return; }
      if (to === null) return;
      e.preventDefault();
      pick(to);
    });
    // Escape also hides values shown by the pointer alone, wherever the focus is.
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && current >= 0 && !focused) { pinned = false; hide(); }
    });
    window.addEventListener("resize", function () { if (current >= 0) place(current); });
    plot.addEventListener("scroll", function () { if (current >= 0) place(current); });
  });
})();
