// The two-list column picker (core/_field_picker.html), as ReCET's own Templates page has it: on the output template page, in Choose
// columns on Results and Compare, on the record downloads page, in the cost-test builder's streams from input columns and on the
// publication scan settings. The left list shows the columns of the chosen kind or group that are not chosen yet, filtered by the search
// box; the right list is the chosen columns, in the order chosen (or, with data-order="catalog", in the catalog's order). The input the
// picker names (data-target: a Columns text box, or a hidden columns field) always holds the right list, one per line or comma-separated
// (data-sep), and typing or pasting in a text box rebuilds the list. Each picker ([data-field-picker]) has its own id prefix (data-prefix)
// and catalog (data-catalog), so a page can hold two. Without scripts the page keeps its own way to choose ([data-without-picker]: its
// ticked boxes are carried into the target, then cleared so they add nothing); [data-with-picker] parts are shown only with the picker. A
// disabled target (a hidden field that must not be sent without scripts) is enabled. Changes are announced through window.cedarsAnnounce.
(function () {
  "use strict";
  var pickers = document.querySelectorAll("[data-field-picker]");
  if (!pickers.length) return;
  Array.prototype.forEach.call(pickers, setUp);
  // the tick boxes are the pickers' no-script stand-in: hidden (and cleared, so they add nothing) while the pickers run
  Array.prototype.forEach.call(document.querySelectorAll("[data-without-picker]"), function (el) {
    Array.prototype.forEach.call(el.querySelectorAll("input[type=checkbox]"), function (b) { b.checked = false; });
    el.hidden = true;
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-with-picker]"), function (el) { el.hidden = false; });

function setUp(picker) {
  var pre = picker.getAttribute("data-prefix") || "pick";
  function el(name) { return document.getElementById(pre + "-" + name); }
  var catalogEl = document.getElementById(picker.getAttribute("data-catalog") || "field-catalog");
  if (!catalogEl) return;
  var text = document.getElementById(picker.getAttribute("data-target") || "id_columns");
  if (!text) return;
  text.disabled = false;
  var comma = picker.getAttribute("data-sep") === "comma";
  var noun = picker.getAttribute("data-noun") || "the template";
  var inCatalogOrder = picker.getAttribute("data-order") === "catalog";
  var unknownWords = picker.getAttribute("data-unknown") || "not a measure-level column";
  var item = picker.getAttribute("data-item") || "column";
  var fields = JSON.parse(catalogEl.textContent), byName = {}, byLower = {}, rank = {};
  fields.forEach(function (f, i) { byName[f.c] = f; byLower[f.c.toLowerCase()] = f.c; rank[f.c] = i; });
  function shown(name) { var f = byName[name]; return f && f.t ? f.t : name; } // Results' columns have a label besides their key
  var avail = el("available"), sel = el("selected");
  var kind = el("kind"), find = el("find"), findSel = el("find-sel");
  var def = el("def");
  var chosen = [];

  function say(t) { if (window.cedarsAnnounce) window.cedarsAnnounce(t); }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
  function parse(value) {
    var out = [];
    value.split(/\r?\n/).forEach(function (line) {
      line.split(",").forEach(function (c) {
        c = c.trim();
        c = byLower[c.toLowerCase()] || c;
        if (c && out.indexOf(c) < 0) out.push(c);
      });
    });
    return out;
  }
  function matches(f, q) {
    return !q || [f.c, f.t || "", f.d || ""].some(function (s) { return s.toLowerCase().indexOf(q) >= 0; });
  }
  function option(name) {
    var o = document.createElement("option");
    o.value = name;
    o.textContent = byName[name] ? shown(name) : name + " (" + unknownWords + ")"; // said in words, not by color
    return o;
  }
  function renderAvail() {
    var k = kind.value, q = find.value.trim().toLowerCase(), taken = {}, groups = {}, order = [], shown = 0;
    chosen.forEach(function (c) { taken[c] = 1; });
    avail.textContent = "";
    fields.forEach(function (f) {
      if (taken[f.c] || !matches(f, q)) return;
      if (k.slice(0, 2) === "k:" && f.k !== k.slice(2)) return;
      if (k.slice(0, 2) === "g:" && f.g !== k.slice(2)) return;
      if (!groups[f.g]) { groups[f.g] = []; order.push(f.g); }
      groups[f.g].push(f.c);
      shown += 1;
    });
    order.forEach(function (g) {
      var og = document.createElement("optgroup");
      og.label = g;
      groups[g].forEach(function (c) { og.appendChild(option(c)); });
      avail.appendChild(og);
    });
    el("count").textContent = plural(shown, item) + " shown";
    return shown;
  }
  function renderSel(keep, leaveText) {
    var q = findSel.value.trim().toLowerCase();
    sel.textContent = "";
    chosen.forEach(function (c) {
      if (q && c.toLowerCase().indexOf(q) < 0 && shown(c).toLowerCase().indexOf(q) < 0) return;
      var o = option(c);
      if (keep && keep.indexOf(c) >= 0) o.selected = true;
      sel.appendChild(o);
    });
    el("sel-count").textContent = plural(chosen.length, item) + " in " + noun;
    var count = pre === "pick" && document.getElementById("columns-count");
    if (count) count.textContent = plural(chosen.length, item) + " in " + noun + ".";
    if (!leaveText) text.value = comma ? chosen.join(",") : chosen.join("\n") + (chosen.length ? "\n" : "");
  }
  function picked(list) {
    return Array.prototype.filter.call(list.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
  }
  function showDef(name) {
    var f = byName[name];
    def.textContent = "";
    var head = document.createElement(f && f.l ? "a" : "strong");
    if (f && f.l) head.href = f.l;
    head.textContent = shown(name);
    def.appendChild(head);
    def.appendChild(document.createTextNode(f ? " (" + f.g + "): " + (f.d || "no definition recorded") : " is " + unknownWords + "."));
  }
  function sortChosen() {  // catalog order; a name the catalog lacks goes last
    chosen.sort(function (a, b) { return (a in rank ? rank[a] : 1e9) - (b in rank ? rank[b] : 1e9); });
  }
  function add(names) {
    names = names.filter(function (c) { return chosen.indexOf(c) < 0; });
    if (!names.length) return;
    chosen = chosen.concat(names);
    if (inCatalogOrder) sortChosen();
    renderSel(names);
    renderAvail();
    say((names.length === 1 ? shown(names[0]) : plural(names.length, item)) + " added; " + plural(chosen.length, item) + " in " + noun);
  }
  function remove(names) {
    if (!names.length) return;
    chosen = chosen.filter(function (c) { return names.indexOf(c) < 0; });
    renderSel();
    renderAvail();
    say((names.length === 1 ? shown(names[0]) : plural(names.length, item)) + " removed; " + plural(chosen.length, item) + " in " + noun);
  }
  function move(how) {
    var names = picked(sel);
    if (!names.length) return;
    var rest = chosen.filter(function (c) { return names.indexOf(c) < 0; });
    if (how === "top") chosen = names.concat(rest);
    else if (how === "bottom") chosen = rest.concat(names);
    else {
      var step = how === "up" ? -1 : 1, idx = names.map(function (c) { return chosen.indexOf(c); });
      if (step > 0) idx.reverse();
      idx.forEach(function (i) {
        var j = i + step;
        if (j < 0 || j >= chosen.length || names.indexOf(chosen[j]) >= 0) return;
        var tmp = chosen[j]; chosen[j] = chosen[i]; chosen[i] = tmp;
      });
    }
    renderSel(names);
    var first = chosen.indexOf(names[0]) + 1;
    say((names.length === 1 ? shown(names[0]) : plural(names.length, item)) + " now at position " + first + " of " + chosen.length);
  }

  el("add").addEventListener("click", function () { add(picked(avail)); });
  el("add-all").addEventListener("click", function () {
    add(Array.prototype.map.call(avail.options, function (o) { return o.value; }));
  });
  el("remove").addEventListener("click", function () { remove(picked(sel)); });
  el("clear").addEventListener("click", function () {
    if (chosen.length && window.confirm("Remove every " + item + " from " + noun + "?")) remove(chosen.slice());
  });
  ["top", "up", "down", "bottom"].forEach(function (h) {
    var button = el(h);
    if (button) button.addEventListener("click", function () { move(h); });
  });
  avail.addEventListener("dblclick", function (e) { if (e.target.tagName === "OPTION") add([e.target.value]); });
  sel.addEventListener("dblclick", function (e) { if (e.target.tagName === "OPTION") remove([e.target.value]); });
  avail.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); add(picked(avail)); } });
  sel.addEventListener("keydown", function (e) {
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); remove(picked(sel)); }
    else if (!inCatalogOrder && e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); move(e.key === "ArrowUp" ? "up" : "down"); }
  });
  [avail, sel].forEach(function (list) {
    list.addEventListener("change", function () { var n = picked(list); if (n.length) showDef(n[n.length - 1]); });
  });
  kind.addEventListener("change", function () { say(plural(renderAvail(), item) + " shown"); });
  find.addEventListener("input", renderAvail);
  findSel.addEventListener("input", function () { renderSel(picked(sel)); });
  text.addEventListener("input", function () { chosen = parse(text.value); if (inCatalogOrder) sortChosen(); renderSel(null, true); renderAvail(); }); // leave the text as typed
  text.addEventListener("change", function () { renderSel(); });

  // this picker's ticked no-script boxes are carried into its target before the boxes are cleared (above)
  var scope = picker.closest("form") || document;
  chosen = parse(text.value);
  Array.prototype.forEach.call(scope.querySelectorAll("[data-without-picker] input[type=checkbox]"), function (b) {
    var forThis = b.getAttribute("data-picker-for");
    if (forThis ? forThis !== pre : scope.querySelectorAll("[data-field-picker]").length > 1) return;
    if (b.checked && chosen.indexOf(b.value) < 0) chosen.push(b.value);
  });
  if (inCatalogOrder) sortChosen();
  picker.hidden = false;
  renderSel();
  renderAvail();
}
})();
