// The test page's formula bar. Without this script the formula is a plain text box that the server reads when it changes, and the
// lists below it are ticked by hand. With it: the names you can write are suggested as you type (Up and Down choose, Enter or Tab
// takes one, Escape closes the list), the formula is checked as you type, the lists follow the formula, and ticking a list rewrites
// the formula. The server reads the formula again on save; this check only says early what it will say.
(function () {
  "use strict";
  var ta = document.getElementById("id_formula");
  var list = document.getElementById("formula-suggest");
  var check = document.getElementById("formula-check");
  var data = document.getElementById("formula-catalog");
  if (!ta || !list || !check || !data) return;
  var cat = JSON.parse(data.textContent);
  var streams = {};
  cat.streams.forEach(function (s) { streams[s.name] = s; });
  var names = cat.streams.map(function (s) { return s.name; });
  var form = ta.form;
  var announce = window.cedarsAnnounce || function () {};

  ta.setAttribute("aria-autocomplete", "list");
  ta.setAttribute("aria-controls", "formula-suggest");

  function boxes(field) { return Array.prototype.slice.call(form.querySelectorAll('input[name="' + field + '"]')); }
  function testId() { var el = form.querySelector('[name="id"]'); return el ? el.value.trim() : ""; }
  function pricing() { var el = form.querySelector('input[name="pricing"]:checked'); return el ? el.value : ""; }

  // --- the formula from the lists, and the lists from the formula (the same reading as cedars/services/refdata/formula.py)
  function fromLists() {
    var ticked = function (f) { return boxes(f).filter(function (b) { return b.checked; }).map(function (b) { return b.value; }); };
    var ben = ticked("benefits").concat(ticked("numerator_adders").map(function (a) { return "adder(" + a + ")"; }));
    var cost = ticked("costs");
    var share = (form.querySelector('input[name="share"]:checked') || {}).value || "";
    if (share === "positive_benefits") cost.push("share x ProgramCosts");
    else if (share) cost.push("share(" + share + ") x ProgramCosts");
    var id = testId();
    return (id ? id + " = " : "") + "(" + ben.join(" + ") + ") / (" + cost.join(" + ") + ")";
  }

  function splitTop(text, sep) {
    var out = [], depth = 0, cur = "";
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (ch === "(") depth++;
      else if (ch === ")" && --depth < 0) return null;
      if (ch === sep && depth === 0) { out.push(cur); cur = ""; } else cur += ch;
    }
    if (depth) return null;
    out.push(cur);
    return out;
  }
  function unwrap(s) {
    s = s.trim();
    while (s[0] === "(" && s[s.length - 1] === ")" && splitTop(s.slice(1, -1), "\0") !== null) s = s.slice(1, -1).trim();
    return s;
  }
  function near(word) {
    var low = word.toLowerCase();
    for (var i = 0; i < names.length; i++) if (names[i].toLowerCase() === low) return names[i];
    var best = "", score = 0;
    names.forEach(function (n) {
      var nl = n.toLowerCase(), k = 0;
      while (k < nl.length && k < low.length && nl[k] === low[k]) k++;
      if (k >= 4 && k > score) { best = n; score = k; }
    });
    return best;
  }

  function parse(text) {
    var p = { test: "", benefits: [], numerator_adders: [], costs: [], share: "", problems: [], warnings: [] };
    var body = text.replace(/\s+/g, " ").trim();
    if (!body) return p;
    var m = body.match(/^([A-Za-z][A-Za-z0-9]*)\s*=(?!=)\s*(.*)$/);
    if (m) { p.test = m[1]; body = m[2]; }
    var halves = splitTop(body, "/");
    if (halves === null) { p.problems.push("The brackets do not pair up: each ( needs a )."); return p; }
    if (halves.length !== 2 || !halves[0].trim() || !halves[1].trim()) { p.problems.push("Write one / between the benefits and the costs: (benefits) / (costs)."); return p; }
    var written = { benefits: false, costs: false };
    [["benefits", halves[0]], ["costs", halves[1]]].forEach(function (side) {
      var where = side[0], seen = {}, inner = unwrap(side[1]);
      if (!inner) return;
      var terms = splitTop(inner, "+");
      if (terms === null) { p.problems.push("The brackets do not pair up: each ( needs a )."); return; }
      terms.forEach(function (raw) {
        var term = unwrap(raw), sm, am, name, target;
        if (term) written[where] = true;
        if (!term) { p.problems.push("The " + where + " have an empty term: two + together, or a + at an end."); return; }
        if ((sm = term.match(/^share(?:\(\s*([A-Za-z][A-Za-z0-9]*)\s*\))?\s*(?:x|\*|×)\s*ProgramCosts$/i))) {
          if (where === "benefits") p.problems.push("“" + term + "” is a cost: put the program costs below the line, in the costs.");
          else if (p.share) p.problems.push("The program costs are named twice; a test takes one share of them.");
          else if (sm[1]) {
            if (cat.shares.indexOf(sm[1]) >= 0) p.share = sm[1];
            else p.problems.push("“" + term + "”: " + sm[1] + " does not compute its own share of the program costs (tests that do: " + (cat.shares.join(", ") || "none in this set yet") + ").");
          } else p.share = "positive_benefits";
          return;
        }
        if ((am = term.match(/^adder\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)$/i))) {
          if (where === "costs") { p.problems.push("“" + term + "”: an adder is added to the benefits; write it above the line."); return; }
          name = am[1]; target = p.numerator_adders;
        } else if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(term)) {
          if (term.toLowerCase() === "programcosts") { p.problems.push("“ProgramCosts” is taken through a share: write share x ProgramCosts."); return; }
          name = term; target = where === "benefits" ? p.benefits : p.costs;
        } else if (/[-*×]|\d|\bx\b/.test(term)) {
          p.problems.push("“" + term + "”: a formula only adds streams, each once. A weight, a number or a minus sign would need an engine change.");
          return;
        } else { p.problems.push("“" + term + "” is not a stream name, adder(…) or share x ProgramCosts."); return; }
        if (!streams[name]) { var n = near(name); p.problems.push("“" + name + "” is not a stream of this set." + (n ? " Did you mean " + n + "?" : "")); return; }
        if (seen[name]) { p.problems.push(name + " is named twice in the " + where + "."); return; }
        seen[name] = true;
        target.push(name);
        var pr = pricing();
        if (pr && streams[name].pricings.length && streams[name].pricings.indexOf(pr) < 0) p.warnings.push(name + " is not read in " + pr + ".");
      });
    });
    if (!written.benefits) p.problems.push("A test needs at least one benefit stream above the line.");
    if (!p.costs.length && !written.costs) p.problems.push("A test needs at least one cost stream below the line.");
    var id = testId();
    if (p.test && id && p.test !== id) p.problems.push("The formula is named " + p.test + " but the test's id is " + id + ".");
    return p;
  }

  function setLists(p) {
    ["benefits", "numerator_adders", "costs"].forEach(function (f) {
      boxes(f).forEach(function (b) { b.checked = p[f].indexOf(b.value) >= 0; });
    });
    boxes("share").forEach(function (b) { b.checked = b.value === p.share; });
  }

  function plural(n, one) { return n + " " + one + (n === 1 ? "" : "s"); }
  var lastSaid = "";
  function showCheck(p, speak) {
    var text;
    if (!ta.value.trim()) { check.hidden = true; return; }
    if (p.problems.length) {
      text = plural(p.problems.length, "problem") + ": " + p.problems.join(" ");
      check.className = "formula-check small bad";
    } else {
      var share = p.share === "positive_benefits" ? "its own share of the program costs" : p.share ? p.share + "'s share of the program costs" : "no program costs";
      text = "Reads " + plural(p.benefits.length, "benefit") + (p.numerator_adders.length ? ", " + plural(p.numerator_adders.length, "adder") : "") +
        " and " + plural(p.costs.length, "cost") + ", with " + share + ". The lists below follow it." + (p.warnings.length ? " " + p.warnings.join(" ") : "");
      check.className = "formula-check small " + (p.warnings.length ? "warn" : "ok");
    }
    check.textContent = text;
    check.hidden = false;
    if (speak && text !== lastSaid) { lastSaid = text; announce(text); }
  }

  var timer = 0;
  function onFormula() {
    var p = parse(ta.value);
    if (!p.problems.length && ta.value.trim()) setLists(p);
    showCheck(p, false);
    window.clearTimeout(timer);
    timer = window.setTimeout(function () { showCheck(parse(ta.value), true); }, 900);
  }

  form.addEventListener("change", function (e) {
    var n = e.target.name;
    if (n === "benefits" || n === "numerator_adders" || n === "costs" || n === "share") {
      ta.value = fromLists();
      showCheck(parse(ta.value), true);
    } else if (n === "pricing" || n === "id") showCheck(parse(ta.value), false);
  });

  // --- suggestions for the word being typed
  var active = -1, items = [];
  function wordAt() {
    var end = ta.selectionStart, start = end;
    while (start > 0 && /[A-Za-z0-9_]/.test(ta.value[start - 1])) start--;
    return { start: start, end: end, text: ta.value.slice(start, end) };
  }
  function candidates(word) {
    var w = word.toLowerCase(), out = [];
    var extra = ["adder(", "share x ProgramCosts"].concat(cat.shares.map(function (t) { return "share(" + t + ") x ProgramCosts"; }));
    names.concat(extra).forEach(function (n) { if (n.toLowerCase().indexOf(w) === 0) out.push(n); });
    names.forEach(function (n) { if (out.indexOf(n) < 0 && n.toLowerCase().indexOf(w) > 0) out.push(n); });
    return out.slice(0, 8);
  }
  function close() {
    list.hidden = true; list.innerHTML = ""; items = []; active = -1;
    ta.removeAttribute("aria-activedescendant");
  }
  function highlight(i) {
    active = i;
    items.forEach(function (li, k) { li.setAttribute("aria-selected", k === i ? "true" : "false"); });
    if (i >= 0) ta.setAttribute("aria-activedescendant", items[i].id); else ta.removeAttribute("aria-activedescendant");
    if (i >= 0) announce(items[i].textContent);
  }
  function take(i) {
    var w = wordAt(), text = items[i].dataset.value;
    var after = ta.value.slice(w.end);
    var join = text.slice(-1) === "(" ? "" : (/^\s*[)+/]/.test(after) || !after ? "" : " ");
    ta.value = ta.value.slice(0, w.start) + text + join + after;
    var caret = w.start + text.length + join.length;
    ta.setSelectionRange(caret, caret);
    close();
    onFormula();
  }
  function suggest() {
    var w = wordAt();
    if (w.text.length < 1) { close(); return; }
    var c = candidates(w.text);
    if (!c.length || (c.length === 1 && c[0] === w.text)) { close(); return; }
    list.innerHTML = "";
    items = c.map(function (n, k) {
      var li = document.createElement("li");
      li.id = "formula-opt-" + k;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      li.dataset.value = n;
      var s = streams[n];
      li.textContent = n + (s ? " (" + (s.pricings.join(", ") || "no pricing") + ")" : "");
      li.addEventListener("mousedown", function (e) { e.preventDefault(); take(k); });
      list.appendChild(li);
      return li;
    });
    list.hidden = false;
    active = -1;
    announce(plural(items.length, "suggestion") + ": Down to choose, Enter to take.");
  }

  ta.addEventListener("input", function () { suggest(); onFormula(); });
  ta.addEventListener("keydown", function (e) {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); highlight((active + 1) % items.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlight(active <= 0 ? items.length - 1 : active - 1); }
    else if ((e.key === "Enter" || e.key === "Tab") && active >= 0) { e.preventDefault(); take(active); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  ta.addEventListener("blur", close);

  if (ta.value.trim()) showCheck(parse(ta.value), false);
})();
