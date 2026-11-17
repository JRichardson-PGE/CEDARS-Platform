/* Edit inputs as a spreadsheet: the draft's Measure and ProgramCost tables in two Tabulator grids, as ReCET's own Edit inputs page has
   them (recet/web/static/edit.js, whose screen-reader fixes this keeps: NVDA-tested roles, one tab stop with the focus on the active
   cell, Tab and Shift+Tab out of the grid, a help line that follows the active cell, the ARIA tabs pattern).

   A progressive enhancement: without this script (or with ?view=rows) the page is the server's paged table and row pages. With it,
   every change is saved (PUT grid.json, debounced) with both tables whole, and the server answers with the engine's cell checks and
   the dropdown values for the claim years now in the table. Each row carries a private _rid (never sent) so a check result, which
   names rows by position at the time of the save, still lands on the right row after later inserts. No sorting or filtering, so the
   grid order is the table order. Cell text is always set as text, never as HTML. Status is announced through cedarsAnnounce. */
(function () {
  "use strict";
  var root = document.getElementById("editor");
  if (!root || typeof Tabulator === "undefined") return;

  var API = root.dataset.api, END_USES = root.dataset.endUses, RUN = root.dataset.run;
  var CSRF = (root.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";
  var TABLES = ["Measure", "ProgramCost"];
  var WIDTHS = { CEInputID: 120, MeasCode: 100, MeasDescription: 230, PrgID: 180, AC_Elec_Impact_Profile: 240, AC_Elec_Impact_Profile_2nd_Baseline: 240,
                 AC_Elec_Impact_Profile_Water: 240, AC_Elec_Impact_Profile_Parent: 240,
                 AC_Elec_Impact_Profile_Parent_2nd_Baseline: 240, AC_Gas_Impact_Profile: 200 };
  var END_USE_COLUMNS = ["AC_Elec_Impact_Profile", "AC_Elec_Impact_Profile_2nd_Baseline", "AC_Elec_Impact_Profile_Water",
                         "AC_Elec_Impact_Profile_Parent", "AC_Elec_Impact_Profile_Parent_2nd_Baseline"];   // per-row lists (IOU and target sector)
  var st = {
    rev: null, layout: null, columns: {}, choices: {}, issues: { Measure: {}, ProgramCost: {} }, grids: {}, current: "Measure",
    timer: null, saving: false, pending: false, dirty: false, conflict: false, nextRid: 1, endUses: {}, maxRows: 500, newMeasure: null,
    pcLike: [], canChange: false, loaded: false
  };

  function say(text) { if (window.cedarsAnnounce) window.cedarsAnnounce(text); }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function setStatus(text, kind) {
    var s = document.getElementById("save-status");
    s.textContent = text;
    s.className = "save-status small " + (kind || "muted");
  }

  // the engine's editor.display_number: at most 6 decimals, trailing zeros dropped; display only
  var NUM = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
  function displayNumber(v) {
    var s = v == null ? "" : String(v).trim();
    if (!s || !NUM.test(s)) return s;
    var f = Number(s);
    if (!isFinite(f)) return s;
    var out = f.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
    if ((out === "0" || out === "-0") && f !== 0) out = Number(f.toPrecision(3)).toExponential();
    return out === "-0" ? "0" : out;
  }

  function uniqueId(base, taken) {
    var stem = String(base || "NEW").replace(/_(copy|\d+)(_\d+)?$/, "") || "NEW";
    for (var i = 1; i < 100000; i++) {
      var cand = stem + "_" + i;
      if (!taken.has(cand)) return cand;
    }
    return stem + "_" + Date.now();
  }

  function rowsOf(t) { return st.grids[t].getRows("active"); }
  function withRid(obj) { obj._rid = st.nextRid++; return obj; }

  function toObjects(table) {
    return table.rows.map(function (r) {
      var o = {};
      table.columns.forEach(function (c, i) { o[c] = r[i] == null ? "" : String(r[i]); });
      return withRid(o);
    });
  }

  function blankRow(t) {
    var o = {};
    st.columns[t].forEach(function (c) { o[c] = ""; });
    return o;
  }

  // ---------------------------------------------------------------------------------------------------------------- columns
  // a cell's marks: its class (outline and corner symbol) and the text the help line reads; touches only the cell's class and
  // data, so an open editor in it survives. The problem is in the help line and the issue list, never only in a title.
  var RANK = { error: 3, warning: 2, info: 1 };
  function markIssue(t, cell) {
    var e = cell.getElement(), rid = cell.getRow().getData()._rid, f = cell.getField();
    var found = (st.issues[t][rid] || {})[f];
    e.classList.remove("issue-error", "issue-warning", "issue-info");
    if (found) {
      e.classList.add("issue-" + found.severity);
      e.dataset.issue = found.messages.join("; ");
      e.setAttribute("aria-description", (found.severity === "error" ? "Stops the run: " : found.severity === "warning" ? "Warning: " : "Note: ")
        + found.messages.join("; "));
    } else {
      delete e.dataset.issue;
      e.removeAttribute("aria-description");
    }
  }

  // redraws the marks of these rows ({table: Set of rid strings}); rows deleted since have nothing to redraw
  function remark(touched) {
    TABLES.forEach(function (t) {
      if (!touched[t] || !touched[t].size) return;
      var live = new Map(rowsOf(t).map(function (r) { return [String(r.getData()._rid), r]; }));
      touched[t].forEach(function (rid) {
        var row = live.get(String(rid));
        if (row) row.getCells().forEach(function (c) { markIssue(t, c); });   // not reformat(): that would close an open editor
      });
    });
  }

  function formatter(t, numeric, list) {
    return function (cell) {
      markIssue(t, cell);
      var v = cell.getValue(), text = numeric ? displayNumber(v) : (v == null ? "" : String(v));
      if (!list) return el("span", null, text);
      var w = el("span", "list-cell");                  // a value-list column shows an arrow, so it reads as "pick from a list"
      w.appendChild(el("span", "list-val", text));
      var a = el("span", "list-arrow", "▾");
      a.setAttribute("aria-hidden", "true");
      w.appendChild(a);
      return w;
    };
  }

  function endUseLookup(cell) {
    var d = cell.getRow().getData();
    var iou = d.IOU_AC_Territory || "", sector = d.AC_Elec_Target_Sector || "", key = iou + "|" + sector;
    if (st.endUses[key]) return st.endUses[key];
    return fetch(END_USES + "?iou=" + encodeURIComponent(iou) + "&sector=" + encodeURIComponent(sector), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) { st.endUses[key] = j.values || [""]; return st.endUses[key]; })
      .catch(function () { return [""]; });
  }

  function columnDefs(t) {
    var L = st.layout[t], noEffect = new Set(L.no_effect), numeric = new Set(L.numeric);
    return L.pinned.concat(L.scroll).map(function (col) {
      var endUse = t === "Measure" && END_USE_COLUMNS.indexOf(col) >= 0, list = endUse || !!(st.choices[t] || {})[col];
      var d = {
        title: col, field: col, frozen: L.pinned.indexOf(col) >= 0, cssClass: noEffect.has(col) ? "noeff" : "calc",
        formatter: formatter(t, numeric.has(col), list), hozAlign: numeric.has(col) ? "right" : "left",
        width: WIDTHS[col] || Math.min(240, Math.max(84, col.length * 7 + 26))
      };
      if (!st.canChange) return d;
      d.editor = "input";
      d.editorParams = { selectContents: true };
      if (endUse) {
        d.editor = "list";
        d.editorParams = { valuesLookup: endUseLookup, autocomplete: true, freetext: true, allowEmpty: true, listOnEmpty: true, filterDelay: 150 };
      } else if ((st.choices[t] || {})[col]) {
        d.editor = "list";
        d.editorParams = function () {
          return { values: (st.choices[t] || {})[col] || [""], autocomplete: true, freetext: true, allowEmpty: true, listOnEmpty: true };
        };
      }
      return d;
    });
  }

  function buildGrid(t, data) {
    var opts = {
      data: toObjects(data), index: "_rid", height: "62vh", layout: "fitData", placeholder: "No rows",
      columns: columnDefs(t),
      rowHeader: { formatter: "rownum", headerSort: false, hozAlign: "center", resizable: false, frozen: true, width: 46 },
      columnDefaults: { headerSort: false, resizable: "header" },
      selectableRange: 1, selectableRangeColumns: true, selectableRangeRows: true,
      clipboard: st.canChange ? true : "copy", clipboardCopyStyled: false,
      clipboardCopyConfig: { rowHeaders: false, columnHeaders: false, formatCells: false }, clipboardCopyRowRange: "range"
    };
    if (st.canChange) {
      Object.assign(opts, {
        selectableRangeClearCells: true, selectableRangeClearCellsValue: "", editTriggerEvent: "dblclick",
        clipboardPasteParser: "range", clipboardPasteAction: "range", history: true
      });
    }
    var g = new Tabulator("#grid-" + t, opts);
    g.on("tableBuilt", function () { fixRoles(t, g); cellFocus(t, g); });
    if (st.canChange) {
      ["cellEdited", "clipboardPasted", "rowAdded", "rowDeleted", "historyUndo", "historyRedo", "dataChanged"].forEach(function (ev) {
        g.on(ev, function () { changed(); });
      });
      // A block pasted (an Excel copy) while a cell's editor is open goes to the grid's paste, not into the one input.
      g.element.addEventListener("paste", function (e) {
        var text = e.clipboardData && e.clipboardData.getData("text/plain");
        if (!text || e.target.tagName !== "INPUT" || !/[\t\n]/.test(text.replace(/\r?\n$/, ""))) return;
        e.preventDefault();
        e.stopPropagation();
        var editing = g.element.querySelector(".tabulator-cell.tabulator-editing");
        var cell = editing && g.getRows("active").reduce(function (hit, r) {
          return hit || r.getCells().find(function (c) { return c.getElement() === editing; });
        }, null);
        if (cell) cell.cancelEdit();
        var dt = new DataTransfer();
        dt.setData("text/plain", text);
        g.element.querySelector(".tabulator-tableholder").dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
      }, true);
    }
    // One click opens the editor (Shift / Ctrl / Cmd clicks are left to the range selection; a click in a cell being edited places
    // the caret). The edit waits a tick: the range selection moves the focus to the grid on the same click.
    g.on("cellClick", function (e, cell) {
      var col = cell.getField(), elm = cell.getElement();
      showHelp(t, col, elm);
      if (!st.canChange || !col || e.shiftKey || e.ctrlKey || e.metaKey || elm.classList.contains("tabulator-editing")) return;
      setTimeout(function () {
        if (elm.classList.contains("tabulator-editing") || !elm.isConnected) return;
        elm.focus({ preventScroll: true });
        cell.edit();
      }, 0);
    });
    st.grids[t] = g;
  }

  // The help line: the column's description and what it changes, led by the cell's problems when it is flagged. Not a live region (NVDA
  // read the whole description before every cell); a screen reader reads the focused cell with its problem, and this is one Shift+Tab away.
  var lastHelp = "";
  function showHelp(t, col, cellEl) {
    var help = document.getElementById("help"), L = st.layout[t];
    if (!col || !L) return;
    var issue = cellEl && cellEl.dataset.issue;
    var sev = !cellEl ? "" : cellEl.classList.contains("issue-error") ? "Stops the run: " : cellEl.classList.contains("issue-warning") ? "Warning: " : "Note: ";
    var text = (issue ? sev + issue + "  |  " : "") + col + ": " + (L.help[col] || "no description") + (L.tooltips[col] ? "  |  " + L.tooltips[col] : "");
    if (text === lastHelp) return;
    lastHelp = text;
    help.textContent = text;
  }

  // Tabulator 6.3.1's grid roles nest a rowgroup in the header's rowgroup and put an unroled, focusable scroll box between the grid and
  // its rows, so screen readers cannot read it as a table (1.3.1, 4.1.2): the wrappers become presentation, the scroll box the rowgroup.
  function fixRoles(t, g) {
    var e = g.element;
    e.setAttribute("aria-label", t + " table");
    var hc = e.querySelector(".tabulator-header-contents"), holder = e.querySelector(".tabulator-tableholder"), body = e.querySelector(".tabulator-table");
    if (hc) hc.setAttribute("role", "presentation");
    if (holder) { holder.setAttribute("role", "rowgroup"); holder.setAttribute("aria-label", t + " rows"); }
    if (body) body.setAttribute("role", "presentation");
  }

  // The grid is one tab stop (the ARIA grid pattern). The focus sits on the active cell, so a screen reader names the cell and switches to
  // its focus mode by itself, the arrows move it, and the help line follows it (nothing hover-only, 2.1.1). Tab and Shift+Tab leave the
  // grid (Tabulator's Tab moved to the next cell: a keyboard trap, 2.1.2); Tab still moves an open editor to the next cell.
  function cellFocus(t, g) {
    var holder = g.element.querySelector(".tabulator-tableholder"), header = g.element.querySelector(".tabulator-header");
    var range = g.modules && g.modules.selectRange;
    if (!holder || !range) return;
    if (header) header.setAttribute("tabindex", "-1");
    var editing = function () { return g.element.querySelector(".tabulator-cell.tabulator-editing"); };
    var active = function () {
      var c = range.activeRange && range.getActiveCell(), e = c && c.getElement && c.getElement();
      return e && e.isConnected && holder.contains(e) ? e : null;
    };
    var stop = function () {   // the tab stop: the active cell while it is drawn (scrolling drops rows), else the scroll box
      var e = active();
      g.element.querySelectorAll(".tabulator-cell[tabindex]").forEach(function (c) { if (c !== e) c.removeAttribute("tabindex"); });
      if (e) e.setAttribute("tabindex", "0");
      holder.setAttribute("tabindex", e ? "-1" : "0");
      return e;
    };
    var toCell = function () {
      var f = document.activeElement, inGrid = g.element.contains(f), e = stop();   // stop() blurs the cell it takes the tabindex from
      if (inGrid && !editing() && f !== e) (e || holder).focus({ preventScroll: true });
      if (e) showHelp(t, e.getAttribute("tabulator-field"), e);
    };
    holder.addEventListener("focus", function () { setTimeout(toCell, 0); });
    holder.addEventListener("focusin", function (ev) {
      if (ev.target.classList.contains("tabulator-cell")) showHelp(t, ev.target.getAttribute("tabulator-field"), ev.target);
    });
    g.on("rangeChanged", function () { setTimeout(toCell, 0); });
    g.element.addEventListener("keyup", function (e) {   // the first move sends no rangeChanged
      if (/^(Arrow|Home|End|Page)/.test(e.key) && e.target.tagName !== "INPUT") toCell();
    });
    g.on("renderComplete", stop);
    // An open editor: its input is named by the column and row (it has no label of its own, 4.1.2), and the cell gives up its tab stop
    // while the input covers it, so the input is the one target there (2.5.8). Ending the edit puts the focus back on the cell.
    g.on("cellEditing", function (cell) {
      var e = cell.getElement(), name = cell.getField() + ", row " + (rowsOf(t).indexOf(cell.getRow()) + 1);
      setTimeout(function () {
        var input = e.querySelector("input, select, textarea");
        if (!input) return;
        input.setAttribute("aria-label", name);
        e.removeAttribute("tabindex");
      }, 0);
    });
    ["cellEdited", "cellEditCancelled"].forEach(function (ev) { g.on(ev, function () { setTimeout(toCell, 0); }); });
    g.element.addEventListener("keydown", function (e) {
      if (e.key === "Tab" && !editing()) e.stopPropagation();   // before Tabulator's keybinding takes it
    }, true);
    stop();
  }

  function updateCounts() {
    TABLES.forEach(function (t) {
      var c = document.querySelector('[data-count="' + t + '"]');
      if (c && st.grids[t]) c.textContent = "(" + rowsOf(t).length + ")";
    });
  }

  // ---------------------------------------------------------------------------------------------------------------- saving
  function changed() {
    if (st.conflict || !st.loaded) return;
    st.dirty = true;
    updateCounts();
    setStatus("Unsaved changes…");
    clearTimeout(st.timer);
    st.timer = setTimeout(save, 700);
  }

  function collect(t) {
    var cols = st.columns[t], rows = rowsOf(t).map(function (r) { return r.getData(); });
    return {
      rids: rows.map(function (d) { return d._rid; }),
      table: { columns: cols, rows: rows.map(function (d) { return cols.map(function (c) { return d[c] == null ? "" : String(d[c]); }); }) }
    };
  }

  function save() {
    clearTimeout(st.timer);
    if (st.conflict) return Promise.resolve(false);
    if (st.saving) { st.pending = true; return st.saving; }
    var snap = {}, body = { rev: st.rev, tables: {} };
    TABLES.forEach(function (t) { var c = collect(t); snap[t] = c.rids; body.tables[t] = c.table; });
    st.dirty = false;
    setStatus("Saving…");
    st.saving = fetch(API, {
      method: "PUT", credentials: "same-origin", body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF, Accept: "application/json" }
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; });
    }).then(function (res) {
      if (!res.ok) {
        st.dirty = true;
        if (res.status === 409) st.conflict = true;
        var msg = res.status === 403 && !res.j.error ? "Signed out or not allowed: reload the page. Your last changes were not saved."
          : (res.j.error || ("Not saved (" + res.status + ")"));
        setStatus(msg, "bad");
        say(msg);
        return false;
      }
      st.rev = res.j.rev;
      st.choices = res.j.choices || st.choices;
      applyIssues(res.j.issues || [], snap);
      setStatus(st.dirty ? "Unsaved changes…" : "Saved " + new Date().toLocaleTimeString(), st.dirty ? "muted" : "ok");
      return true;
    }).catch(function (e) {
      st.dirty = true;
      var msg = "Not saved: " + e.message + ". Your changes are still here; they are saved with the next change.";
      setStatus(msg, "bad");
      say(msg);
      return false;
    }).then(function (ok) {
      var again = st.pending;
      st.saving = false;
      st.pending = false;
      if (again || (ok && st.dirty)) return save();
      return ok;
    });
    return st.saving;
  }

  function applyIssues(list, snap) {
    var touched = { Measure: new Set(Object.keys(st.issues.Measure)), ProgramCost: new Set(Object.keys(st.issues.ProgramCost)) };
    st.issues = { Measure: {}, ProgramCost: {} };
    st.issueList = [];
    list.forEach(function (i) {
      var rid = snap[i.table] && snap[i.table][i.row];
      if (rid == null) return;
      var byCol = st.issues[i.table][rid] = st.issues[i.table][rid] || {};
      var cur = byCol[i.column] = byCol[i.column] || { severity: i.severity, messages: [] };
      if (RANK[i.severity] > RANK[cur.severity]) cur.severity = i.severity;
      cur.messages.push(i.message);
      touched[i.table].add(String(rid));
      st.issueList.push({ table: i.table, rid: rid, column: i.column, severity: i.severity, message: i.message });
    });
    remark(touched);
    renderIssues();
  }

  var WORDS = { error: "Stops the run", warning: "Warning", info: "Note" };
  function renderIssues() {
    var ul = document.getElementById("grid-issues");
    ul.textContent = "";
    var pos = {};
    TABLES.forEach(function (t) {
      pos[t] = {};
      rowsOf(t).forEach(function (r, i) { pos[t][r.getData()._rid] = i + 1; });
    });
    var list = (st.issueList || []).filter(function (i) { return pos[i.table][i.rid]; });
    var order = { error: 0, warning: 1, info: 2 };
    list.sort(function (a, b) {
      return order[a.severity] - order[b.severity] || (a.table < b.table ? -1 : a.table > b.table ? 1 : 0) || pos[a.table][a.rid] - pos[b.table][b.rid];
    });
    var n = { error: 0, warning: 0, info: 0 };
    list.forEach(function (i) { n[i.severity]++; });
    st.cellCounts = n;
    st.firstError = list.find(function (i) { return i.severity === "error"; }) || null;
    list.slice(0, 400).forEach(function (i) {
      var li = el("li", "issue-" + i.severity);
      li.appendChild(el("span", null, WORDS[i.severity] + ": "));
      var b = el("button", "link", i.table + " row " + pos[i.table][i.rid] + ", " + i.column);
      b.type = "button";
      b.addEventListener("click", function () { goTo(i.table, i.rid, i.column); });
      li.appendChild(b);
      li.appendChild(document.createTextNode(": " + i.message));
      ul.appendChild(li);
    });
    if (list.length > 400) ul.appendChild(el("li", null, "and " + (list.length - 400) + " more"));
    var box = document.getElementById("grid-issues-box");
    box.hidden = !list.length;
    document.getElementById("grid-issues-count").textContent = list.length.toLocaleString();
    renderSummary();
  }

  var lastSummary = "";
  function renderSummary() {
    var sum = document.getElementById("grid-summary"), n = st.cellCounts || { error: 0, warning: 0, info: 0 };
    var plural = function (k, one, many) { return k.toLocaleString() + " " + (k === 1 ? one : many); };
    var text, kind;
    if (n.error) {
      text = plural(n.error, "cell would stop the run", "cells would stop the run") + " (marked ✕)" + (n.warning ? "; " + plural(n.warning, "warning", "warnings") : "") + ".";
      kind = "bad";
    } else if (n.warning) {
      text = "Nothing here stops the run; " + plural(n.warning, "warning", "warnings") + " (marked !).";
      kind = "warn";
    } else {
      text = "Nothing here stops the run.";
      kind = "ok";
    }
    sum.textContent = text;
    sum.className = "issue-line " + kind;
    if (st.loaded && text !== lastSummary) say(text);   // said once when the verdict changes, never on every save
    lastSummary = text;
  }

  function goTo(t, rid, col) {
    show(t);
    var g = st.grids[t], done = false;
    var finish = function () {
      if (done) return;
      done = true;
      var row = g.getRow(rid), cell = row && row.getCell(col);
      if (cell) {
        var e = cell.getElement();
        e.classList.add("flash");
        setTimeout(function () { e.classList.remove("flash"); }, 1200);
        e.focus({ preventScroll: true });   // "show this cell" takes the keyboard there too
      }
      showHelp(t, col, cell && cell.getElement());
    };
    g.scrollToRow(rid, "center", false).then(function () { return g.scrollToColumn(col, "middle", false); }).catch(function () {}).then(finish);
    setTimeout(finish, 400);   // the scroll promises do not always settle (a row already in view)
  }

  // ---------------------------------------------------------------------------------------------------------------- tabs and rows
  function show(t) {
    st.current = t;
    TABLES.forEach(function (x) {
      document.getElementById("grid-" + x).hidden = x !== t;
      var tab = document.getElementById("tab-" + x);
      tab.setAttribute("aria-selected", x === t ? "true" : "false");
      tab.tabIndex = x === t ? 0 : -1;
    });
    st.grids[t].redraw(true);
    fillColumnNames();
  }

  function fillColumnNames() {
    var dl = document.getElementById("col-names"), L = st.layout && st.layout[st.current];
    if (!dl || !L) return;
    dl.textContent = "";
    L.pinned.concat(L.scroll).forEach(function (c) { var o = el("option"); o.value = c; dl.appendChild(o); });
  }

  var colFind = document.getElementById("col-find");
  colFind.addEventListener("change", function () {
    var t = st.current, g = st.grids[t], q = colFind.value.trim().toLowerCase();
    if (!g || !q) return;
    var L = st.layout[t], cols = L.pinned.concat(L.scroll);
    var hit = cols.find(function (c) { return c.toLowerCase() === q; }) || cols.find(function (c) { return c.toLowerCase().indexOf(q) >= 0; });
    if (!hit) { colFind.setCustomValidity("No column named like that"); colFind.reportValidity(); return; }
    colFind.setCustomValidity("");
    var col = g.getColumn(hit);
    if (!col.isVisible()) col.show();
    g.scrollToColumn(hit, "middle", false).catch(function () {}).then(function () {
      var h = col.getElement();
      h.classList.add("flash");
      setTimeout(function () { h.classList.remove("flash"); }, 1200);
      showHelp(t, hit);
      say("Column " + hit);
    });
  });

  document.getElementById("hide-noeff").addEventListener("change", function (e) {
    TABLES.forEach(function (t) {
      var g = st.grids[t];
      if (!g) return;
      (st.layout[t].no_effect || []).forEach(function (c) {
        var col = g.getColumn(c);
        if (col && st.layout[t].pinned.indexOf(c) < 0) { if (e.target.checked) col.hide(); else col.show(); }
      });
    });
  });

  function selectedRows(t) {
    var seen = new Set(), out = [];
    st.grids[t].getRanges().forEach(function (range) {
      range.getRows().forEach(function (r) {
        var rid = r.getData()._rid;
        if (!seen.has(rid)) { seen.add(rid); out.push(r); }
      });
    });
    var pos = new Map(rowsOf(t).map(function (r, i) { return [r.getData()._rid, i]; }));
    out.sort(function (a, b) { return pos.get(a.getData()._rid) - pos.get(b.getData()._rid); });
    return out;
  }

  function tell(text) { setStatus(text, "bad"); say(text); }

  function addRow() {
    var t = st.current, g = st.grids[t], sel = selectedRows(t), all = rowsOf(t);
    var after = sel.length ? sel[sel.length - 1] : all[all.length - 1];
    var like = after ? after.getData() : null, row = blankRow(t);
    if (t === "Measure") {
      if (all.length >= st.maxRows) { tell("Edit inputs takes jobs of up to " + st.maxRows + " measures."); return; }
      Object.assign(row, st.newMeasure.defaults);
      st.newMeasure.like.forEach(function (c) { if (like && like[c]) row[c] = like[c]; });
      row.CEInputID = uniqueId("NEW", new Set(all.map(function (r) { return r.getData().CEInputID; })));
    } else {
      st.pcLike.forEach(function (c) { if (like && like[c]) row[c] = like[c]; });
    }
    g.addRow(withRid(row), false, after || undefined).then(function (r) {
      g.scrollToRow(r, "nearest", false);
      say("Added row " + (rowsOf(t).indexOf(r) + 1));
    });
  }

  function duplicateRows() {
    var t = st.current, g = st.grids[t], sel = selectedRows(t), all = rowsOf(t);
    if (!sel.length) { tell("Select one or more rows (or cells in them) first."); return; }
    if (t === "Measure" && all.length + sel.length > st.maxRows) { tell("Edit inputs takes jobs of up to " + st.maxRows + " measures."); return; }
    var taken = new Set(all.map(function (r) { return r.getData().CEInputID; }));
    var copies = sel.map(function (r) {
      var d = Object.assign({}, r.getData());
      delete d._rid;
      if (t === "Measure") { d.CEInputID = uniqueId(d.CEInputID || "NEW", taken); taken.add(d.CEInputID); }
      return withRid(d);
    });
    copies.reduce(function (p, d) { return p.then(function (prev) { return g.addRow(d, false, prev); }); }, Promise.resolve(sel[sel.length - 1]))
      .then(function () { say("Copied " + copies.length + (copies.length === 1 ? " row" : " rows")); });
  }

  // Delete asks first on the page itself (a confirm bar with its own buttons), never a browser dialog.
  var confirmBar = document.getElementById("grid-confirm");
  function deleteRows() {
    var t = st.current, sel = selectedRows(t);
    if (!sel.length) { tell("Select one or more rows (or cells in them) first."); return; }
    confirmBar.hidden = false;
    document.getElementById("grid-confirm-text").textContent = "Delete " + sel.length + (sel.length === 1 ? " row" : " rows") + " from " + t + "? Undo brings them back.";
    var yes = document.getElementById("grid-confirm-yes"), no = document.getElementById("grid-confirm-no");
    var close = function () { confirmBar.hidden = true; yes.onclick = no.onclick = null; };
    yes.onclick = function () { sel.forEach(function (r) { r.delete(); }); close(); say("Deleted " + sel.length + (sel.length === 1 ? " row" : " rows")); };
    no.onclick = function () { close(); };
    yes.focus();
  }

  TABLES.forEach(function (t) {
    var b = document.getElementById("tab-" + t);
    b.addEventListener("click", function () { show(t); });
    b.addEventListener("keydown", function (e) {   // the ARIA tabs pattern: Left / Right, Home / End
      var i = TABLES.indexOf(t), j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABLES.length - 1 }[e.key];
      if (j == null) return;
      e.preventDefault();
      var next = TABLES[(j + TABLES.length) % TABLES.length], to = document.getElementById("tab-" + next), grid = document.getElementById("grid-" + next);
      show(next);
      var back = function () { to.focus(); };   // the grid's redraw takes the focus: for a moment, give it back to the tab
      grid.addEventListener("focusin", back);
      setTimeout(function () { grid.removeEventListener("focusin", back); }, 500);
      to.focus();
    });
  });
  root.querySelectorAll("[data-action]").forEach(function (b) {
    b.addEventListener("click", function () {
      if (!st.layout) return;
      var a = b.dataset.action, g = st.grids[st.current];
      if (a === "add") addRow();
      else if (a === "duplicate") duplicateRows();
      else if (a === "delete") deleteRows();
      else if (a === "undo") g.undo();
      else if (a === "redo") g.redo();
    });
  });

  // Run these inputs, and any form on the page (rename, add, check): save first, so the server acts on the tables as they are shown.
  var runLink = document.getElementById("run-link");
  if (runLink) runLink.addEventListener("click", function (e) {
    if (st.cellCounts && st.cellCounts.error && st.firstError) {
      e.preventDefault();
      tell("Fix the " + (st.cellCounts.error === 1 ? "problem" : st.cellCounts.error + " problems") + " marked ✕ first: the run would be refused.");
      goTo(st.firstError.table, st.firstError.rid, st.firstError.column);
      return;
    }
    if (!st.dirty && !st.saving) return;
    e.preventDefault();
    Promise.resolve(save()).then(function (ok) { if (ok) window.location.href = RUN; });
  });
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(st.dirty || st.saving) || form.dataset.saved) return;
    e.preventDefault();
    var submitter = e.submitter;
    Promise.resolve(save()).then(function (ok) {
      if (!ok) return;
      form.dataset.saved = "1";
      if (form.requestSubmit) form.requestSubmit(submitter || undefined); else form.submit();
    });
  }, true);
  window.addEventListener("beforeunload", function (e) {
    if (st.dirty || st.saving) { e.preventDefault(); e.returnValue = ""; }
  });

  // ---------------------------------------------------------------------------------------------------------------- load
  fetch(API, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) { setStatus(res.j.error || "Could not load the draft.", "bad"); root.dataset.ready = "failed"; return; }
      var d = res.j;
      // the spreadsheet takes over from the paged table and its static check list
      document.querySelectorAll("[data-without-grid]").forEach(function (e) { e.hidden = true; });
      root.hidden = false;
      st.rev = d.rev;
      st.layout = d.layout;
      st.choices = d.choices || {};
      st.maxRows = d.max_rows;
      st.newMeasure = d.new_measure;
      st.pcLike = d.new_programcost_like || [];
      st.canChange = !!d.can_change;
      var ready = 0;
      TABLES.forEach(function (t) {
        st.columns[t] = d.tables[t].columns;
        buildGrid(t, d.tables[t]);
        st.grids[t].on("tableBuilt", function () {
          if (++ready < TABLES.length) return;
          var snap = {};
          TABLES.forEach(function (x) { snap[x] = rowsOf(x).map(function (r) { return r.getData()._rid; }); });
          applyIssues(d.issues || [], snap);
          updateCounts();
          fillColumnNames();
          st.dirty = false;
          st.loaded = true;
          setStatus(st.canChange ? "Saved" : "Read only: you cannot change this draft", st.canChange ? "ok" : "muted");
          root.dataset.ready = "1";   // for the page's checks (tests/a11y): the grids are drawn
        });
      });
    })
    .catch(function (e) { setStatus("Could not load the draft: " + e.message, "bad"); root.dataset.ready = "failed"; });
})();
