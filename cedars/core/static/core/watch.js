// A part of a page that follows work still running elsewhere (a filing's pricing run; UI review 3, U1), as the run page follows its run
// (calculations/run.js): every 5 seconds it asks the element's data-watch-url for {"ended", "text"}, writes the text into the element,
// and says a change through the page's one live region (window.cedarsAnnounce). When the work ends the page reloads to show its
// outcome, but only when nothing has the focus, so a keyboard or screen-reader user is never moved; otherwise it says so and shows the
// element's reload link. Without scripts the element shows the state as it was when the page was loaded, beside a link to reload.
(function () {
  "use strict";
  var el = document.querySelector("[data-watch-url]");
  if (!el) return;
  var text = el.querySelector("[data-watch-text]") || el;
  var reload = el.querySelector("[data-watch-reload]");
  var last = text.textContent.trim();

  function idle() { var a = document.activeElement; return !a || a === document.body || a.id === "main"; }

  function update(s) {
    if (s.text && s.text !== last) {
      text.textContent = s.text;
      window.cedarsAnnounce(s.text);
      last = s.text;
    }
    if (!s.ended) return false;
    if (idle()) { window.location.reload(); }
    else {
      if (reload) reload.hidden = false;
      window.cedarsAnnounce(s.text + " Reload the page to see where the filing stands.");
    }
    return true;
  }

  function poll() {
    fetch(el.dataset.watchUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) { if (s && !update(s)) window.setTimeout(poll, 5000); })
      .catch(function () { window.setTimeout(poll, 15000); });
  }
  if (reload) reload.hidden = true;  // with scripts the page reloads itself, or shows the link when it cannot
  window.setTimeout(poll, 5000);
})();
