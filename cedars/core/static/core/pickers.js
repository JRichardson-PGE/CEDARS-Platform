// The multi-program picker (core/_program_picker.html; UI review 3, "pick, don't type"). The page works without this: the program typed
// or picked in the box is added when the form is sent. This adds it to the ticked programs as soon as it is picked from the list, empties
// the box for the next one and says so through the page's one live region (window.cedarsAnnounce).
(function () {
  "use strict";
  document.querySelectorAll("[data-program-picker]").forEach(function (picker) {
    var box = picker.querySelector("[data-picker-add]");
    var chosen = picker.querySelector("[data-picker-chosen]");
    var summary = picker.querySelector("[data-picker-summary]");
    var list = box && document.getElementById(box.getAttribute("list"));
    if (!box || !chosen || !list) return;
    var options = {};
    Array.prototype.forEach.call(list.options, function (o) { options[o.value.toLowerCase()] = o; });

    var ticked = function () { return chosen.querySelectorAll("input[type=checkbox]:checked").length; };
    var update = function () {
      var n = ticked();
      if (summary) summary.textContent = "(" + (n ? n + " program" + (n === 1 ? "" : "s") + " chosen" : "Every program") + ")";
    };

    var add = function () {
      var o = options[box.value.trim().toLowerCase()];
      if (!o) return;
      var value = o.value;
      var existing = null;
      chosen.querySelectorAll("input[type=checkbox]").forEach(function (c) { if (c.value === value) existing = c; });
      var name = o.textContent.split(" · ")[0];
      if (existing) {
        existing.checked = true;
      } else {
        var li = document.createElement("li");
        var label = document.createElement("label");
        label.className = "check";
        var input = document.createElement("input");
        input.type = "checkbox";
        input.name = box.name;
        input.value = value;
        input.checked = true;
        var id = document.createElement("span");
        id.className = "id";
        id.textContent = value;
        label.appendChild(input);
        label.appendChild(document.createTextNode(" "));
        label.appendChild(id);
        if (name) {
          var span = document.createElement("span");
          span.className = "small";
          span.textContent = name;
          label.appendChild(document.createTextNode(" "));
          label.appendChild(span);
        }
        li.appendChild(label);
        chosen.appendChild(li);
      }
      chosen.hidden = false;
      box.value = "";
      update();
      window.cedarsAnnounce(value + (name ? " " + name : "") + " added. " + ticked() + " chosen.");
    };

    // A choice picked from the list (no inputType, or a replacement) is added at once; typing a whole PrgID waits for the box to be left,
    // so a shorter ID that happens to exist is not taken while a longer one is still being typed.
    box.addEventListener("input", function (e) {
      if (!e.inputType || e.inputType === "insertReplacementText") add();
    });
    box.addEventListener("change", add);
    chosen.addEventListener("change", update);
  });
})();

// A choice that decides which of the form's next fields apply (select[data-reveal]: Link a record's "Carried by"): only the fields for
// the chosen value ([data-reveal-for]) show. Without this every one shows, each saying which choice it is for.
(function () {
  "use strict";
  document.querySelectorAll("select[data-reveal]").forEach(function (select) {
    var parts = select.form ? select.form.querySelectorAll("[data-reveal-for]") : [];
    var apply = function () {
      parts.forEach(function (p) { p.hidden = p.getAttribute("data-reveal-for") !== select.value; });
    };
    select.addEventListener("change", apply);
    apply();
  });
})();
