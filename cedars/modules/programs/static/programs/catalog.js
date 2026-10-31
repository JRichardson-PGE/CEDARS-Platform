// Programs catalog: the Other filters panel. Without this script the page works as it is: "Show its options" reloads the page with the
// chosen field's inputs shown. With it, choosing a field shows its inputs in place, and the form sends only the filters that are set,
// so the link stays short enough to share.
(function () {
  "use strict";
  var form = document.querySelector("form.catalog-filters");
  var select = document.getElementById("other-field");
  if (!form || !select) return;
  var groups = form.querySelectorAll(".other-field");
  form.querySelectorAll(".nojs").forEach(function (el) { el.hidden = true; });

  var show = function (code, announce) {
    var shown = null;
    groups.forEach(function (g) {
      var on = g.getAttribute("data-field") === code;
      g.hidden = !on;
      if (on) shown = g;
    });
    if (announce && shown) {
      var label = select.options[select.selectedIndex].text.replace(/ \(applied\)$/, "");
      window.cedarsAnnounce && window.cedarsAnnounce(label + ": its options follow the field list.");
    }
  };
  select.addEventListener("change", function () { show(select.value, true); });
  show(select.value, false);

  // Send only what is set: blank boxes, the field list and the paging are left out of the link (the server ignores them anyway).
  form.addEventListener("submit", function (e) {
    var params = new URLSearchParams();
    new FormData(form).forEach(function (value, name) {
      if (name === "field" || name === "pick" || String(value).trim() === "") return;
      params.append(name, value);
    });
    e.preventDefault();
    var qs = params.toString();
    window.location.assign((form.getAttribute("action") || window.location.pathname) + (qs ? "?" + qs : ""));
  });
})();
