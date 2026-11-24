// A fund shift's lines (budgets/_shift_lines_table.html). The form works without this. This adds an "Add a line" button (a blank
// line without saving), keeps each line's budget now and after, the totals and what is left to cover current as you type, and says
// beside a line what is wrong with it before you save: a PrgID that cannot be named here, an amount that is not a figure, a line that
// would take a program-year below zero, a program-year on two lines. On the statewide program changes page (data-mode="statewide") a
// line is the change to the whole program: the page also shows what each funding PA would cover, split by the allocation factors the
// way the server splits it (to the cent, half to even, the cents left over to the shift's PA). The page's own check on saving stays
// the rule.
(function () {
  "use strict";
  var table = document.querySelector("[data-shift-lines]");
  var data = document.getElementById("shift-budget");
  if (!table || !data) return;
  var budget = JSON.parse(data.textContent);
  var partsData = document.getElementById("shift-parts");
  var parts = partsData ? JSON.parse(partsData.textContent) : {};  // the PA's own IDs of statewide programs: {id: the statewide PrgID}
  var pa = table.getAttribute("data-pa");
  var statewide = table.getAttribute("data-mode") === "statewide";
  var programs = {};
  var list = document.getElementById("programs");
  if (list) Array.prototype.forEach.call(list.options, function (o) { programs[o.value.toLowerCase()] = o.value; });
  var body = table.tBodies[0];
  var totalCell = table.querySelector("[data-total]");
  var leftCell = table.querySelector("[data-left]");
  var split = document.querySelector("[data-split]");
  var money = function (cents) {
    var s = (Math.abs(cents) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (cents < 0 ? "-$" : "$") + s;
  };
  // Dollars as typed to whole cents; NaN when it is not a figure, 0 when blank.
  var centsOf = function (text) {
    var t = text.replace(/[$,\s]/g, "");
    if (!t) return 0;
    return /^-?\d+(\.\d+)?$/.test(t) ? Math.round(parseFloat(t) * 100) : NaN;
  };
  var portionCents = Math.round(parseFloat(table.getAttribute("data-portion") || "0") * 100) || 0;
  // A funder's portion of a line: the amount by its factor (four decimal places), rounded half to even like the server.
  var portionOf = function (cents, factor) {
    var product = cents * Math.round(parseFloat(factor) * 10000);
    if (!Number.isSafeInteger(product)) return Math.round(product / 10000);
    var negative = product < 0, p = Math.abs(product), q = Math.floor(p / 10000), r = p % 10000;
    if (r > 5000 || (r === 5000 && q % 2 === 1)) q += 1;
    return negative ? -q : q;
  };
  // {funding PA: its portion in cents} of one statewide line; the cents rounding leaves over go to entry.rounding.
  var splitLine = function (entry, cents) {
    var out = {}, placed = 0;
    entry.funders.forEach(function (f) { out[f[0]] = portionOf(cents, f[1]); placed += out[f[0]]; });
    out[entry.rounding] += cents - placed;
    return out;
  };
  var coverWords = function (code, cents) {
    if (cents > 0) return code + " covers " + money(cents) + " from its own programs";
    if (cents < 0) return code + " gets " + money(-cents) + " back, to put into its own programs";
    return code + " has nothing to cover";
  };
  var announced = "";

  var check = function () {
    var total = 0, figures = true, seen = {}, covers = {}, order = [];
    Array.prototype.forEach.call(body.rows, function (row) {
      var prg = row.querySelector("[name=prg_id]"), year = row.querySelector("[name=year]"), amount = row.querySelector("[name=amount]");
      var problem = row.querySelector("[data-line-problem]"), heldCell = row.querySelector("[data-line-held]");
      var afterCell = row.querySelector("[data-line-after]");
      var id = prg.value.trim(), canonical = programs[id.toLowerCase()] || "";
      var value = centsOf(amount.value), key = canonical + " " + year.value, b = budget[key];
      var says = [];
      if (id && !canonical) {
        var whole = parts[id.toLowerCase()];
        if (statewide) says.push(id + " is not a statewide program " + pa + " leads.");
        else if (whole) says.push(id + " is " + pa + "'s part of the statewide program " + whole + ", whose budget changes only as a whole, on " + whole + ".");
        else says.push(id + " is not one of the programs " + pa + " can name here.");
      }
      if (isNaN(value)) { says.push("The amount must be a dollar figure."); figures = false; }
      if (canonical && year.value) {
        if (seen[key]) says.push(canonical + " " + year.value + " is also on line " + seen[key] + "; put its whole amount on one line.");
        seen[key] = seen[key] || row.querySelector("[data-line-number]").textContent;
      }
      var held = null;
      if (canonical && year.value) {
        if (statewide) {
          if (!b) says.push(canonical + " " + year.value + " has no allocation factors, so its funding PAs' portions are unknown.");
          else held = Math.round(parseFloat(b.whole) * 100);
        } else {
          held = b ? Math.round(parseFloat(b.held) * 100) : 0;
        }
      }
      if (statewide && b && !isNaN(value) && value) {
        var portions = splitLine(b, value), below = held + value < 0;
        if (below) says.push("Takes " + key + " below zero: the whole program holds " + money(held) + ".");
        b.funders.forEach(function (f) {
          var code = f[0], holds = Math.round(parseFloat(f[2]) * 100);
          if (!(code in covers)) { covers[code] = 0; order.push(code); }
          covers[code] += portions[code];
          if (!below && portions[code] < 0 && holds + portions[code] < 0) {
            says.push("Takes " + code + "'s portion of " + key + " below zero: it holds " + money(holds) + ".");
          }
        });
      } else if (!statewide && held !== null && !isNaN(value) && value < 0 && held + value < 0) {
        says.push("Takes " + key + " below zero: it holds " + money(held) + ".");
      }
      heldCell.textContent = held === null ? "" : money(held);
      if (afterCell) afterCell.textContent = held === null || isNaN(value) || !value ? "" : money(held + value);
      problem.textContent = says.join(" ");
      row.classList.toggle("has-problem", says.length > 0);
      if (!isNaN(value)) total += value;
    });
    if (totalCell) totalCell.textContent = figures ? money(total) : "a line's amount is not a figure";
    var say = "";
    if (leftCell) {
      var left = portionCents >= 0 ? portionCents + total : -(portionCents + total);
      leftCell.textContent = figures ? money(left) + (left === 0 ? " (covered)" : left < 0 ? " (more than the portion)" : "") : "";
      say = figures ? (left === 0 ? pa + "'s portion is covered" : (left < 0 ? money(-left) + " more than the portion" : money(left) + " left")) : "";
    }
    if (split) {
      var rows = split.tBodies[0];
      while (rows.firstChild) rows.removeChild(rows.firstChild);
      order.sort(function (a, b2) { return a === pa ? -1 : b2 === pa ? 1 : a < b2 ? -1 : 1; });
      order.forEach(function (code) {
        var tr = document.createElement("tr"), th = document.createElement("th"), amount = document.createElement("td");
        var does = document.createElement("td");
        th.scope = "row"; th.textContent = code;
        amount.className = "num"; amount.textContent = money(covers[code]);
        does.textContent = coverWords(code, covers[code]) + (code === pa ? ", in the next step" : "");
        tr.appendChild(th); tr.appendChild(amount); tr.appendChild(does); rows.appendChild(tr);
      });
      if (!order.length) {
        var none = document.createElement("tr"), cell = document.createElement("td");
        cell.colSpan = 3; cell.className = "muted"; cell.textContent = "No statewide change yet.";
        none.appendChild(cell); rows.appendChild(none);
      }
      say = order.map(function (code) { return coverWords(code, covers[code]); }).join("; ");
    }
    return say;
  };
  body.addEventListener("input", check);
  // Leaving a field with a new figure: say the new total once (not on every keystroke).
  body.addEventListener("change", function () {
    var say = check();
    if (say && say !== announced && window.cedarsAnnounce) window.cedarsAnnounce(say);
    announced = say;
  });

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
      var after = row.querySelector("[data-line-after]");
      if (after) after.textContent = "";
      row.classList.remove("has-problem");
      body.appendChild(row);
      row.querySelector("[name=prg_id]").focus();
      window.cedarsAnnounce("Line " + n + " added");
    });
  }
  announced = check();
})();
