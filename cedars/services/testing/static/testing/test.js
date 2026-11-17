// A test's form: what the tester types is kept as they type (a draft on the server), so a closed tab loses nothing. The result itself is
// saved only by the Save buttons; without scripts the page works the same, less the drafts.
(function () {
  "use strict";
  var form = document.querySelector("form[data-draft-url]");
  if (!form || !window.fetch || !window.FormData) return;
  var status = document.getElementById("draft-status");
  var timer = null, dirty = false, told = false, failed = false, leaving = false;

  function say(text) {
    if (!status) return;
    status.hidden = false;
    status.textContent = text;
  }

  function send() {
    timer = null;
    if (!dirty || leaving) return;
    dirty = false;
    fetch(form.getAttribute("data-draft-url"), { method: "POST", body: new FormData(form), credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (!x.ok || !x.d.saved) throw new Error(x.d && x.d.why ? x.d.why : "");
        failed = false;
        say(x.d.draft
          ? "What you typed was kept at " + x.d.at + ". It is not a result until you save it."
          : "Nothing typed that is not already saved.");
        if (!told && x.d.draft && window.cedarsAnnounce) {
          told = true;  // once a page: a message on every pause would talk over the tester's typing
          window.cedarsAnnounce("What you type is kept as you go. It is not a result until you save it.");
        }
      })
      .catch(function (e) {
        dirty = true;
        say("What you typed could not be kept just now" + (e && e.message ? ": " + e.message : ".") + " Save your result to keep it.");
        if (!failed && window.cedarsAnnounce) window.cedarsAnnounce("What you typed could not be kept just now. Save your result to keep it.");
        failed = true;
      });
  }

  function changed() {
    dirty = true;
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(send, 1500);
  }

  form.addEventListener("input", changed);
  form.addEventListener("change", changed);
  form.addEventListener("submit", function () { leaving = true; });  // the Save buttons send everything themselves
  // leaving the page with something typed in the last moment: send it on the way out
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "hidden" || !dirty || leaving) return;
    if (timer) { window.clearTimeout(timer); timer = null; }
    if (navigator.sendBeacon) { dirty = false; navigator.sendBeacon(form.getAttribute("data-draft-url"), new FormData(form)); }
  });
}());
