// The run page keeps its status up to date while the run is queued or running. Changes are spoken through the page's one live region
// (window.cedarsAnnounce); when the run ends the page reloads to show its files, but only when nothing has the focus, so a keyboard or
// screen-reader user is never moved (ReCET 0.1.67 reloadWhenIdle). Without scripts the page shows the status as it was when loaded.
(function () {
  "use strict";
  var el = document.getElementById("run-status");
  if (!el || el.dataset.ended) return;
  var label = document.getElementById("run-status-label");
  var lastStatus = label ? label.textContent : "", lastStage = "";

  function idle() { var a = document.activeElement; return !a || a === document.body || a.id === "main"; }

  function update(s) {
    el.textContent = s.text;
    var bar = document.getElementById("run-progress");
    if (bar && s.percent !== null) { bar.value = s.percent; bar.textContent = s.percent + "%"; }
    if (label) label.textContent = s.status_label;
    if (s.status_label !== lastStatus || (s.stage && s.stage !== lastStage)) window.cedarsAnnounce(s.text);
    lastStatus = s.status_label; lastStage = s.stage;
    if (s.ended) {
      if (idle()) { window.location.reload(); }
      else { window.cedarsAnnounce(s.text + " Reload the page to see its files."); }
      return true;
    }
    return false;
  }

  // The log updates in place while the run runs (never announced: the status line says what changed). It stays at its end when the
  // reader was there, else where they scrolled to.
  var logEl = document.getElementById("run-log");
  function pollLog() {
    if (!logEl || !logEl.dataset.live) return;
    fetch(logEl.dataset.url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (t) {
        if (!t || t === logEl.textContent) return;
        var atEnd = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 4;
        logEl.textContent = t.length > 200000 ? t.slice(t.indexOf("\n", t.length - 200000) + 1) : t;
        if (atEnd) logEl.scrollTop = logEl.scrollHeight;
      })
      .catch(function () {});
  }

  function poll() {
    pollLog();
    fetch(el.dataset.url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) { if (!s || !update(s)) window.setTimeout(poll, 5000); })
      .catch(function () { window.setTimeout(poll, 15000); });
  }
  window.setTimeout(poll, 5000);
})();
