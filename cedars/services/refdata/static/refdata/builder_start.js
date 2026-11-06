// Build a cost-test set, Start from: show only the field the choice needs, as soon as it is chosen. Without this script the page shows it
// after Next (refdata/builder_views.build_start). Nothing is disabled; the other fields are only hidden, and the server reads just the chosen one.
(function () {
  "use strict";
  var start = document.getElementById("id_start");
  if (!start) return;
  var form = start.form;
  var next = form.querySelector("[data-start-next]");
  function show() {
    var v = start.value;
    form.querySelectorAll("[data-start-for]").forEach(function (el) {
      var key = el.getAttribute("data-start-for");
      el.hidden = !(v && (key === v || key === "any"));
    });
    if (next) next.hidden = true;  // the choice shows its field at once
    var field = form.querySelector("[data-start-for='" + v + "']");
    var label = field && field.querySelector("label");
    if (label && window.cedarsAnnounce) window.cedarsAnnounce(label.textContent.trim() + " is next.");
  }
  start.addEventListener("change", show);
  if (next) next.hidden = true;
  if (start.value) show();
})();
