// The guided tour of the front page (Jake, 2026-10-31). The page works without it: every "Take the tour" link goes to /help/tour/, which
// lists the steps. With scripts, the link opens a modal dialog that shows one step at a time beside the part of the page it describes.
//   Keyboard: Next and Back move, Escape (the dialog's own cancel) or Close the tour leaves, Tab stays inside the dialog (showModal).
//   Screen readers: each step moves the focus to the step's heading ("Step 2 of 7: ..."), which is the dialog's name; the text is its
//   description. Back on the first step is aria-disabled, never disabled, so the focus is never lost (CLAUDE.md).
//   The part of the page a step describes gets a focus-colored outline and the rest is dimmed (and still announced in words).
(function () {
  "use strict";
  var dialog = document.getElementById("tour");
  var list = document.getElementById("tour-steps");
  if (!dialog || !list || typeof dialog.showModal !== "function") return;

  var steps = Array.prototype.slice.call(list.querySelectorAll("li")).map(function (li) {
    var link = li.querySelector("a");
    return {
      target: li.getAttribute("data-target") || "",
      title: (li.querySelector("[data-title]") || li).textContent.trim(),
      text: (li.querySelector("[data-text]") || li).textContent.trim(),
      href: link ? link.getAttribute("href") : "",
      linkText: link ? link.textContent.trim() : ""
    };
  });
  if (!steps.length) return;

  var title = dialog.querySelector("#tour-title");
  var text = dialog.querySelector("#tour-text");
  var linkBox = dialog.querySelector("#tour-link");
  var link = linkBox.querySelector("a");
  var back = dialog.querySelector("[data-tour-back]");
  var next = dialog.querySelector("[data-tour-next]");
  var close = dialog.querySelector("[data-tour-close]");
  var current = 0, lit = null, opener = null;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function remember(key) { try { window.localStorage.setItem(key, "1"); } catch (e) { /* private mode: nothing kept */ } }
  function remembered(key) { try { return window.localStorage.getItem(key) === "1"; } catch (e) { return false; } }

  function visible(el) { return !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden"); }

  // The step's part of the page; in the narrow layout a header link may be behind the Menu button, which is then shown instead.
  function targetOf(step) {
    if (!step.target) return null;
    var el = document.querySelector('[data-tour="' + step.target + '"]');
    if (visible(el)) return el;
    var menu = document.querySelector('[data-tour="menu"]');
    return visible(menu) ? menu : null;
  }

  // A header menu (a <details>) is opened while its step is shown, so its pages are in view, and closed again after.
  var opened = null;
  function light(el) {
    if (lit) lit.classList.remove("tour-target");
    if (opened && opened !== el) { opened.open = false; opened = null; }
    lit = el;
    if (el && el.tagName === "DETAILS" && !el.open) { el.open = true; opened = el; }
    if (!el) { dialog.classList.remove("tour-top"); return; }
    el.classList.add("tour-target");
    el.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    // Keep the dialog off the highlighted part: at the bottom of the window, or at the top when the part sits low.
    var box = el.getBoundingClientRect();
    dialog.classList.toggle("tour-top", box.top > window.innerHeight / 2);
  }

  function show(i) {
    current = Math.max(0, Math.min(i, steps.length - 1));
    var step = steps[current];
    // The heading reads "Step 2 of 7: Programs": the count on its own line, the colon for screen readers only.
    title.textContent = "";
    var n = document.createElement("span");
    n.className = "tour-count";
    n.textContent = "Step " + (current + 1) + " of " + steps.length;
    var colon = document.createElement("span");
    colon.className = "sr-only";
    colon.textContent = ": ";
    title.appendChild(n);
    title.appendChild(colon);
    title.appendChild(document.createTextNode(step.title));
    text.textContent = step.text;
    linkBox.hidden = !step.href;
    if (step.href) { link.setAttribute("href", step.href); link.textContent = step.linkText || step.href; }
    back.setAttribute("aria-disabled", current === 0 ? "true" : "false");
    next.textContent = current === steps.length - 1 ? "Finish" : "Next";
    light(targetOf(step));
    title.focus();
  }

  function start(from) {
    opener = from || document.activeElement;
    if (!dialog.open) dialog.showModal();
    document.documentElement.classList.add("touring");
    show(0);
  }

  dialog.addEventListener("close", function () {
    light(null);
    document.documentElement.classList.remove("touring");
    var to = opener && document.contains(opener) ? opener : document.getElementById("main");
    if (to) to.focus();
    window.cedarsAnnounce && window.cedarsAnnounce("Tour closed");
  });

  back.addEventListener("click", function () { if (current > 0) show(current - 1); });
  next.addEventListener("click", function () {
    if (current < steps.length - 1) { show(current + 1); return; }
    remember("cedars-tour-done");
    hideOffer();
    dialog.close();
  });
  close.addEventListener("click", function () { dialog.close(); });
  // app.js closes a header menu on any click outside it; a click in the tour must not close the menu its step has opened.
  dialog.addEventListener("click", function (e) { e.stopPropagation(); });
  // Left and Right arrows move too, unless the focus is on the step's link (where they mean nothing to the tour).
  dialog.addEventListener("keydown", function (e) {
    if (e.target === link) return;
    if (e.key === "ArrowRight" && current < steps.length - 1) { e.preventDefault(); show(current + 1); }
    if (e.key === "ArrowLeft" && current > 0) { e.preventDefault(); show(current - 1); }
  });

  document.querySelectorAll("[data-tour-start]").forEach(function (a) {
    a.addEventListener("click", function (e) { e.preventDefault(); start(a); });
  });

  // The offer at the top of the page: hidden once dismissed or once the tour has been finished (it stays under Help).
  var offer = document.querySelector("[data-tour-offer]");
  function hideOffer() { if (offer) offer.hidden = true; }
  if (offer) {
    if (remembered("cedars-tour-done") || remembered("cedars-tour-dismissed")) hideOffer();
    var dismiss = offer.querySelector("[data-tour-dismiss]");
    if (dismiss) {
      dismiss.hidden = false;
      dismiss.addEventListener("click", function () {
        remember("cedars-tour-dismissed");
        hideOffer();
        var help = document.getElementById("help-title");
        if (help) { help.setAttribute("tabindex", "-1"); help.focus(); }  // the focus goes on rather than to the top of the page
        window.cedarsAnnounce && window.cedarsAnnounce("Hidden. The tour is still under Help.");
      });
    }
  }

  // /?tour=1 (the written tour's "Start the tour" link) opens it at once: the visitor asked for it.
  if (/[?&]tour=1(&|$)/.test(window.location.search)) start(document.querySelector("[data-tour-start]"));
})();
