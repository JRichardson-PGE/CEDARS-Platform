// A fund shift's lines (budgets/_shift_lines_table.html; UI review 3, U16). The form works without this. This adds an "Add a line" button
// (a blank line without saving), keeps each line's operating budget and the net current as you type, and says beside a line what is wrong
// with it before you save: a PrgID that is not one of the PA's programs, an amount that is not a figure, a line that would take a
// program-year below zero, a program-year on two lines. The page's own check on saving stays the rule.
(function () {
  "use strict";
  var table = document.querySelector("[data-shift-lines]");
  var data = document.getElementById("shift-budget");
  if (!table || !data) return;
  var budget = JSON.parse(data.textContent);
  var pa = table.getAttribute("data-pa");
  var programs = {};
  var list = document.getElementById("programs");
  if (list) Array.prototype.forEach.call(list.options, function (o) { programs[o.value.toLowerCase()] = o.value; });
  var body = table.tBodies[0];
  var net = table.querySelector("[data-net]");
  var money = function (x) {
    var s = Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (x < 0 ? "-$" : "$") + s;
  };
  var amountOf = function (text) {
    var t = text.replace(/[$,\s]/g, "");
    if (!t) return 0;
    return /^-?\d+(\.\d+)?$/.test(t) ? parseFloat(t) : NaN;
  };

  var check = function () {
    var total = 0, figures = true, seen = {};
    Array.prototype.forEach.call(body.rows, function (row) {
      var prg = row.querySelector("[name=prg_id]"), year = row.querySelector("[name=year]"), amount = row.querySelector("[name=amount]");
      var problem = row.querySelector("[data-line-problem]"), heldCell = row.querySelector("[data-line-held]");
      var id = prg.value.trim(), canonical = programs[id.toLowerCase()] || "";
      var value = amountOf(amount.value), key = canonical + " " + year.value, b = budget[key];
      var says = [];
      if (id && !canonical) says.push(id + " is not one of the programs " + pa + " can name here.");
      if (isNaN(value)) { says.push("The amount must be a dollar figure."); figures = false; }
      if (canonical && year.value) {
        if (seen[key]) says.push(canonical + " " + year.value + " is also on line " + seen[key] + "; put its whole amount on one line.");
        seen[key] = seen[key] || row.querySelector("[data-line-number]").textContent;
      }
      var held = b ? parseFloat(b.held) : (canonical && year.value ? 0 : null);
      var portion = b && b.factor ? value * parseFloat(b.factor) : value;
      if (held !== null && !isNaN(value) && portion < 0 && held + portion < 0) {
        says.push("Takes " + canonical + " " + year.value + " below zero: it holds " + money(held) + ".");
      }
      heldCell.textContent = held === null ? "" : money(held);
      problem.textContent = says.join(" ");
      row.classList.toggle("has-problem", says.length > 0);
      if (!isNaN(value)) total += portion;
    });
    if (net) net.textContent = figures ? money(Math.round(total * 100) / 100) : "a line's amount is not a figure";
  };
  body.addEventListener("input", check);
  body.addEventListener("change", check);

  // Add a line: a copy of the last line, emptied, with its own ids and labels; focus goes to its PrgID.
  var add = document.querySelector("[data-add-line]");
  if (add) {
    add.hidden = false;
    add.addEventListener("click", function () {
      var last = body.rows[body.rows.length - 1];
      var row = last.cloneNode(true), n = body.rows.length + 1;
      row.querySelector("[data-line-number]").textContent = n;
      row.querySelectorAll("[id]").forEach(function (el) { el.id = el.id.replace(/-\d+$/, "-" + n); });
      row.querySelectorAll("label[for]").forEach(function (el) {
        el.htmlFor = el.htmlFor.replace(/-\d+$/, "-" + n);
        el.textContent = el.textContent.replace(/line \d+$/, "line " + n);
      });
      row.querySelectorAll("[aria-describedby]").forEach(function (el) { el.setAttribute("aria-describedby", "problem-" + n); });
      row.querySelectorAll("input").forEach(function (el) { el.value = ""; });
      row.querySelector("select").value = "";
      row.querySelector("[data-line-problem]").textContent = "";
      row.querySelector("[data-line-held]").textContent = "";
      row.classList.remove("has-problem");
      body.appendChild(row);
      row.querySelector("[name=prg_id]").focus();
      window.cedarsAnnounce("Line " + n + " added");
    });
  }
  check();
})();
