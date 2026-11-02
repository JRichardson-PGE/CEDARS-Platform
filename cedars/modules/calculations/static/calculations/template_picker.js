// Output template page: a two-list field picker, as ReCET's own Templates page has it. The left list shows the fields of the chosen kind
// or group that are not in the template yet, filtered by the search box; the right list is the template in file order. The Columns text
// box (sent with the form) always holds the right list, one column per line, and typing or pasting there rebuilds the list. Without
// scripts the page keeps its text box and its tick boxes. Changes are announced through window.cedarsAnnounce.
(function () {
  "use strict";
  var text = document.getElementById("id_columns");
  var picker = document.getElementById("field-picker");
  var catalogEl = document.getElementById("field-catalog");
  if (!text || !picker || !catalogEl) return;
  var fields = JSON.parse(catalogEl.textContent), byName = {}, byLower = {};
  fields.forEach(function (f) { byName[f.c] = f; byLower[f.c.toLowerCase()] = f.c; });
  var avail = document.getElementById("pick-available"), sel = document.getElementById("pick-selected");
  var kind = document.getElementById("pick-kind"), find = document.getElementById("pick-find"), findSel = document.getElementById("pick-find-sel");
  var def = document.getElementById("pick-def");
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
  function matches(f, q) { return !q || f.c.toLowerCase().indexOf(q) >= 0 || (f.d || "").toLowerCase().indexOf(q) >= 0; }
  function option(name) {
    var o = document.createElement("option");
    o.value = name;
    o.textContent = byName[name] ? name : name + " (not a measure-level column)"; // said in words, not by color
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
    document.getElementById("pick-count").textContent = plural(shown, "field") + " shown";
    return shown;
  }
  function renderSel(keep, leaveText) {
    var q = findSel.value.trim().toLowerCase();
    sel.textContent = "";
    chosen.forEach(function (c) {
      if (q && c.toLowerCase().indexOf(q) < 0) return;
      var o = option(c);
      if (keep && keep.indexOf(c) >= 0) o.selected = true;
      sel.appendChild(o);
    });
    document.getElementById("pick-sel-count").textContent = plural(chosen.length, "column") + " in the template";
    var count = document.getElementById("columns-count");
    if (count) count.textContent = plural(chosen.length, "column") + " in the template.";
    if (!leaveText) text.value = chosen.join("\n") + (chosen.length ? "\n" : "");
  }
  function picked(list) {
    return Array.prototype.filter.call(list.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
  }
  function showDef(name) {
    var f = byName[name];
    def.textContent = "";
    var head = document.createElement(f ? "a" : "strong");
    if (f) head.href = f.l;
    head.textContent = name;
    def.appendChild(head);
    def.appendChild(document.createTextNode(f ? " (" + f.g + "): " + (f.d || "no definition recorded") : " is not a measure-level column."));
  }
  function add(names) {
    names = names.filter(function (c) { return chosen.indexOf(c) < 0; });
    if (!names.length) return;
    chosen = chosen.concat(names);
    renderSel(names);
    renderAvail();
    say((names.length === 1 ? names[0] : plural(names.length, "field")) + " added; " + plural(chosen.length, "column") + " in the template");
  }
  function remove(names) {
    if (!names.length) return;
    chosen = chosen.filter(function (c) { return names.indexOf(c) < 0; });
    renderSel();
    renderAvail();
    say((names.length === 1 ? names[0] : plural(names.length, "column")) + " removed; " + plural(chosen.length, "column") + " in the template");
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
    say((names.length === 1 ? names[0] : plural(names.length, "column")) + " now at position " + first + " of " + chosen.length);
  }

  document.getElementById("pick-add").addEventListener("click", function () { add(picked(avail)); });
  document.getElementById("pick-add-all").addEventListener("click", function () {
    add(Array.prototype.map.call(avail.options, function (o) { return o.value; }));
  });
  document.getElementById("pick-remove").addEventListener("click", function () { remove(picked(sel)); });
  document.getElementById("pick-clear").addEventListener("click", function () {
    if (chosen.length && window.confirm("Remove every column from the template?")) remove(chosen.slice());
  });
  ["top", "up", "down", "bottom"].forEach(function (h) {
    document.getElementById("pick-" + h).addEventListener("click", function () { move(h); });
  });
  avail.addEventListener("dblclick", function (e) { if (e.target.tagName === "OPTION") add([e.target.value]); });
  sel.addEventListener("dblclick", function (e) { if (e.target.tagName === "OPTION") remove([e.target.value]); });
  avail.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); add(picked(avail)); } });
  sel.addEventListener("keydown", function (e) {
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); remove(picked(sel)); }
    else if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); move(e.key === "ArrowUp" ? "up" : "down"); }
  });
  [avail, sel].forEach(function (list) {
    list.addEventListener("change", function () { var n = picked(list); if (n.length) showDef(n[n.length - 1]); });
  });
  kind.addEventListener("change", function () { say(plural(renderAvail(), "field") + " shown"); });
  find.addEventListener("input", renderAvail);
  findSel.addEventListener("input", function () { renderSel(picked(sel)); });
  text.addEventListener("input", function () { chosen = parse(text.value); renderSel(null, true); renderAvail(); }); // leave the text as typed
  text.addEventListener("change", function () { renderSel(); });

  // the tick boxes are the picker's no-script stand-in: hidden (and cleared, so they add nothing) while the picker runs
  Array.prototype.forEach.call(document.querySelectorAll("[data-without-picker]"), function (el) {
    Array.prototype.forEach.call(el.querySelectorAll("input[type=checkbox]"), function (b) {
      if (b.checked) { var v = b.value; if (chosen.indexOf(v) < 0) text.value += (text.value && !/\n$/.test(text.value) ? "\n" : "") + v + "\n"; }
      b.checked = false;
    });
    el.hidden = true;
  });
  chosen = parse(text.value);
  picker.hidden = false;
  renderSel();
  renderAvail();
})();
