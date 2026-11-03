// New run: when files are chosen, the form is sent with XMLHttpRequest so the page can show how much of the upload has gone (a large
// job zip takes a while). Without scripts, or without XMLHttpRequest upload events, the form posts as it always does. The pressed button
// is sent too ("Check the inputs only" is a submit button with a name). Progress is shown in text and a <progress> element, and spoken
// through window.cedarsAnnounce at each quarter. The buttons are never disabled (a disabled focused control loses the focus): a second
// press while sending is ignored and the buttons say so with aria-disabled.
(function () {
  "use strict";
  var form = document.querySelector("form[data-upload-progress]");
  var box = document.getElementById("upload-progress");
  if (!form || !box || !window.FormData || !window.XMLHttpRequest) return;
  var bar = box.querySelector("progress");
  var text = box.querySelector("[data-upload-text]");
  var sending = false;

  function hasFile() {
    return Array.prototype.some.call(form.querySelectorAll("input[type=file]"), function (i) { return i.files && i.files.length; });
  }
  function buttons(on) {
    Array.prototype.forEach.call(form.querySelectorAll("button[type=submit]"), function (b) {
      if (on) b.setAttribute("aria-disabled", "true"); else b.removeAttribute("aria-disabled");
    });
  }
  function say(t) {
    text.textContent = t;
    if (window.cedarsAnnounce) window.cedarsAnnounce(t);
  }

  form.addEventListener("submit", function (e) {
    if (sending) { e.preventDefault(); return; }
    if (!hasFile()) return; // nothing to upload: the plain post is quick
    e.preventDefault();
    var data = new FormData(form);
    var by = e.submitter;
    if (by && by.name && !data.has(by.name)) data.append(by.name, by.value);
    var xhr = new XMLHttpRequest();
    xhr.open("POST", form.getAttribute("action") || window.location.href);
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    var said = -1;
    xhr.upload.addEventListener("progress", function (ev) {
      if (!ev.lengthComputable) return;
      var pct = Math.floor((100 * ev.loaded) / ev.total);
      bar.value = pct;
      bar.textContent = pct + "%";
      var quarter = Math.floor(pct / 25);
      text.textContent = "Uploading: " + pct + "% sent.";
      if (quarter !== said) { said = quarter; if (window.cedarsAnnounce) window.cedarsAnnounce("Uploading: " + pct + "% sent."); }
      if (pct >= 100) say("Uploaded. Checking the job and queuing the run…");
    });
    xhr.addEventListener("load", function () {
      // a queued run redirects to its page; anything else (the form with its errors) is shown as the server sent it
      var to = xhr.responseURL;
      if (to && to.split("#")[0] !== window.location.href.split("#")[0]) { window.location.assign(to); return; }
      document.open();
      document.write(xhr.responseText);
      document.close();
    });
    xhr.addEventListener("error", function () {
      sending = false;
      buttons(false);
      box.hidden = false;
      say("The upload did not go through (the connection dropped). Nothing was queued; press Start the run to try again.");
    });
    sending = true;
    buttons(true);
    box.hidden = false;
    bar.value = 0;
    say("Uploading: 0% sent.");
    xhr.send(data);
  });
})();
