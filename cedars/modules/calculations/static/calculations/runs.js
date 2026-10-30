// The runs list updates the status of runs still queued or running every 10 s, and says when one ends (through window.cedarsAnnounce).
(function () {
  "use strict";
  var cells = Array.prototype.slice.call(document.querySelectorAll("[data-run-status]:not([data-ended])"));
  if (!cells.length) return;
  function poll() {
    var pending = cells.filter(function (c) { return !c.dataset.ended; });
    if (!pending.length) return;
    Promise.all(pending.map(function (c) {
      return fetch(c.dataset.runStatus, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) {
          if (!s) return;
          c.textContent = s.text;
          if (s.ended) {
            c.dataset.ended = "1";
            var name = c.closest("tr").querySelector("th a");
            window.cedarsAnnounce((name ? name.textContent + ": " : "") + s.text);
          }
        })
        .catch(function () {});
    })).then(function () { window.setTimeout(poll, 10000); });
  }
  window.setTimeout(poll, 10000);
})();
