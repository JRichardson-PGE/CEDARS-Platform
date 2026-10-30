// Page-wide helpers. Pages render on the server and work without scripts; scripts only add to them (WCAG 2.2 AA, docs/ACCESSIBILITY.md).
(function () {
  "use strict";
  // Speak a short message through the page's one live region. Clearing it first and setting the text a moment later makes screen readers
  // announce a message even when it repeats (ReCET 0.1.70, checked with NVDA 2026.2).
  window.cedarsAnnounce = function (text) {
    var el = document.getElementById("announcer");
    if (!el) return;
    el.textContent = "";
    window.setTimeout(function () { el.textContent = text; }, 150);
  };
})();
