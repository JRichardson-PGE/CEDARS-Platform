// Compact tick lists (core/_tick_list.html; UI review 3). The lists work without this: open one and tick. This keeps each list's summary
// ("2 chosen: Commercial, Residential") current as boxes are ticked, and shows a long list's narrowing box: typing keeps the values that
// contain the words (the ticked ones always stay), says how many are shown, and tells screen readers once typing pauses.
(function () {
  "use strict";
  var NAMED = 3;  // core/tick_lists.py NAMED
  document.querySelectorAll("[data-tick-list]").forEach(function (list) {
    var summary = list.querySelector("[data-tick-summary]");
    var boxes = Array.prototype.slice.call(list.querySelectorAll("input[type=checkbox]"));
    var labelOf = function (box) {
      var l = box.parentNode.querySelector("[data-tick-label]");
      return l ? l.textContent : box.value;
    };
    var update = function () {
      var labels = boxes.filter(function (b) { return b.checked; }).map(labelOf);
      if (!summary) return;
      if (!labels.length) { summary.textContent = list.getAttribute("data-none") || "None chosen"; return; }
      var more = labels.length > NAMED ? " and " + (labels.length - NAMED) + " more" : "";
      summary.textContent = labels.length + " chosen: " + labels.slice(0, NAMED).join(", ") + more;
    };
    list.addEventListener("change", function (e) { if (e.target.type === "checkbox") update(); });

    var narrow = list.querySelector("[data-tick-narrow]");
    if (!narrow) return;
    narrow.hidden = false;
    var input = narrow.querySelector("input");
    var shown = narrow.querySelector("[data-tick-shown]");
    var timer = null;
    input.addEventListener("input", function () {
      var words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      var n = 0;
      boxes.forEach(function (b) {
        var text = labelOf(b).toLowerCase();
        var keep = b.checked || words.every(function (w) { return text.indexOf(w) >= 0; });
        b.closest("li").hidden = !keep;
        if (keep) n += 1;
      });
      var said = n + " of " + boxes.length + " shown";
      shown.textContent = words.length ? said : boxes.length + " shown";
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { if (words.length) window.cedarsAnnounce(said); }, 600);
    });
    // Enter in the box would send the whole form; here it only narrows.
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });
  });
})();
