// "Find a field" on a record's page (submissions/record.html): jumps to the field whose name is typed (exactly, else the first whose name
// contains it), moves the focus into it and says so through the page's one live region (window.cedarsAnnounce). Without scripts the form
// asks the server, which sends the page back at the field (#f-<name>).
(function () {
  "use strict";
  var form = document.querySelector("form[data-find-field]");
  if (!form) return;
  var box = form.querySelector('input[name="find"]');
  var names = Array.prototype.map.call(document.querySelectorAll("#field-names option"), function (o) { return o.value; });

  form.addEventListener("submit", function (e) {
    var q = box.value.trim().toLowerCase();
    if (!q) { e.preventDefault(); return; }
    var hit = names.filter(function (n) { return n.toLowerCase() === q; })[0] ||
              names.filter(function (n) { return n.toLowerCase().indexOf(q) !== -1; })[0];
    if (!hit) {
      e.preventDefault();
      window.cedarsAnnounce("No field is named " + box.value.trim() + ".");
      return;
    }
    var control = document.getElementById("in-" + hit);
    if (!control) return;  // let the server find it
    e.preventDefault();
    control.focus();
    window.cedarsAnnounce("Moved to " + hit + ".");
  });
})();
