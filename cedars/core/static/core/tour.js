// Page help and guided tours (Jake, 2026-10-31). Every page's heading has a (?) button (cedars/core/page_help.py). Without scripts it
// is a link to the page's help on a page of its own, with the tour written out. With scripts:
//   The (?) opens a modal dialog with the page's guide and a "Take a tour of this page" button; Escape or Close shuts it and the focus
//   goes back to the (?).
//   The tour is a modal dialog that shows one step at a time beside the part of the page it describes. Next and Back move (and the
//   arrow keys), Escape or Close the tour leaves, Tab stays inside the dialog (showModal). Each step moves the focus to the step's
//   heading ("Step 2 of 7: ..."), which is the dialog's name; the text is its description. Back on the first step is aria-disabled,
//   never disabled, so the focus is never lost (CLAUDE.md). The part of the page a step describes gets a focus-colored outline and the
//   rest is dimmed (and still announced in words). A step whose part is not on the page (a section only some people see) is left out.
// The front page's tour starts from its own "Take the tour" links too, and /?tour=1 (or any page's ?tour=1) opens it at once.
(function () {
  "use strict";
  var help = document.getElementById("page-help");
  var helpButton = document.querySelector("[data-page-help]");
  var dialog = document.getElementById("tour");
  var list = document.getElementById("tour-steps");
  var modal = !!(dialog && typeof dialog.showModal === "function");
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function remember(key) { try { window.localStorage.setItem(key, "1"); } catch (e) { /* private mode: nothing kept */ } }
  function remembered(key) { try { return window.localStorage.getItem(key) === "1"; } catch (e) { return false; } }
  function visible(el) { return !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden"); }
  function norm(t) { return (t || "").replace(/\s+/g, " ").trim().toLowerCase(); }

  // ---------- the page's help dialog ----------
  if (help && helpButton && typeof help.showModal === "function") {
    helpButton.setAttribute("role", "button");  // a link without scripts; with them it opens the dialog
    helpButton.setAttribute("aria-haspopup", "dialog");
    helpButton.addEventListener("click", function (e) {
      e.preventDefault();
      help.showModal();
      var title = help.querySelector("#page-help-title");
      if (title) title.focus();
    });
    help.querySelectorAll("[data-page-help-close]").forEach(function (b) {
      b.addEventListener("click", function () { help.close(); });
    });
    help.addEventListener("close", function () { if (!help.hasAttribute("data-handing-over")) helpButton.focus(); });
  }

  // ---------- the tour ----------
  if (!modal || !list) return;
  var steps = Array.prototype.slice.call(list.querySelectorAll("li")).map(function (li) {
    var link = li.querySelector("a");
    return {
      target: li.getAttribute("data-target") || "",
      heading: li.getAttribute("data-heading") || "",
      optional: li.hasAttribute("data-optional"),
      title: (li.querySelector("[data-title]") || li).textContent.trim(),
      text: (li.querySelector("[data-text]") || li).textContent.trim(),
      href: link ? link.getAttribute("href") : "",
      linkText: link ? link.textContent.trim() : ""
    };
  });
  if (!steps.length) return;

  // A step's part of the page: its selector's first match that is shown, or the first heading, legend, disclosure or caption whose text
  // starts with its heading. A heading stands for its section (or card) when it heads it, a legend for its fieldset, a disclosure's
  // summary for the disclosure and a caption for its table. In the narrow layout a header link may be behind the Menu button, which is
  // then shown instead.
  var HEADINGS = "main h2, main h3, main h4, main legend, main summary, main caption";
  function area(el) {
    if (!el) return null;
    var tag = el.tagName;
    if (tag === "LEGEND" || tag === "SUMMARY") return el.parentElement;
    if (tag === "CAPTION") { var table = el.closest("table"); return table.closest(".hscroll") || table; }
    if (/^H[2-4]$/.test(tag)) {
      var box = el.closest("section, .card, details");
      if (box && box.querySelector("h2, h3, h4, summary") === el) return box;
    }
    return el;
  }
  function find(step) {
    var el = null, all, i;
    if (step.target) {
      all = document.querySelectorAll(step.target);
      for (i = 0; i < all.length && !el; i++) if (visible(all[i])) el = all[i];
      if (!el && all.length && all[0].closest("body > header")) {
        var menu = document.querySelector('[data-tour="menu"]');
        if (visible(menu)) el = menu;
      }
    } else if (step.heading) {
      var want = norm(step.heading);
      all = document.querySelectorAll(HEADINGS);
      for (i = 0; i < all.length && !el; i++) {
        var shown = all[i].tagName === "CAPTION" ? visible(all[i].closest("table")) : visible(all[i]);
        if (shown && norm(all[i].textContent).indexOf(want) === 0) el = all[i];
      }
    }
    return area(el);
  }
  // For the tests: each step, whether its part is on the page and whether it may be missing.
  window.cedarsTourFound = function () {
    return steps.map(function (s) { return { title: s.title, optional: s.optional, found: !(s.target || s.heading) || !!find(s) }; });
  };

  var title = dialog.querySelector("#tour-title");
  var text = dialog.querySelector("#tour-text");
  var linkBox = dialog.querySelector("#tour-link");
  var link = linkBox.querySelector("a");
  var back = dialog.querySelector("[data-tour-back]");
  var next = dialog.querySelector("[data-tour-next]");
  var close = dialog.querySelector("[data-tour-close]");
  var shown = [], current = 0, lit = null, opener = null, opened = null;

  // A disclosure (a header menu, the filters) is opened while its step is shown, so what it holds is in view, and closed again after.
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
    current = Math.max(0, Math.min(i, shown.length - 1));
    var step = shown[current];
    // The heading reads "Step 2 of 7: Programs": the count on its own line, the colon for screen readers only.
    title.textContent = "";
    var n = document.createElement("span");
    n.className = "tour-count";
    n.textContent = "Step " + (current + 1) + " of " + shown.length;
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
    next.textContent = current === shown.length - 1 ? "Finish" : "Next";
    light(step.target || step.heading ? find(step) : null);
    title.focus();
  }

  function start(from) {
    opener = from || document.activeElement;
    // Only the steps whose part is on this page, for this visitor (worked out when the tour starts, so a page changed since counts).
    shown = steps.filter(function (s) { return !(s.target || s.heading) || !!find(s); });
    if (!shown.length) return;
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
    if (current < shown.length - 1) { show(current + 1); return; }
    if (document.querySelector("[data-tour-offer]")) { remember("cedars-tour-done"); hideOffer(); }
    dialog.close();
  });
  close.addEventListener("click", function () { dialog.close(); });
  // app.js closes a header menu on any click outside it; a click in the tour must not close the menu its step has opened.
  dialog.addEventListener("click", function (e) { e.stopPropagation(); });
  // Left and Right arrows move too, unless the focus is on the step's link (where they mean nothing to the tour).
  dialog.addEventListener("keydown", function (e) {
    if (e.target === link) return;
    if (e.key === "ArrowRight" && current < shown.length - 1) { e.preventDefault(); show(current + 1); }
    if (e.key === "ArrowLeft" && current > 0) { e.preventDefault(); show(current - 1); }
  });

  document.querySelectorAll("[data-tour-start]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      // From the page's help dialog: that dialog closes and the tour opens; when the tour closes the focus goes back to the (?).
      if (help && help.contains(a)) {
        help.setAttribute("data-handing-over", "");
        help.close();
        help.removeAttribute("data-handing-over");
        start(helpButton);
        return;
      }
      start(a);
    });
  });

  // The front page's offer: hidden once dismissed or once the tour has been finished (it stays under Help).
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
        var helpTitle = document.getElementById("help-title");
        if (helpTitle) { helpTitle.setAttribute("tabindex", "-1"); helpTitle.focus(); }  // the focus goes on rather than to the top
        window.cedarsAnnounce && window.cedarsAnnounce("Hidden. The tour is still under Help.");
      });
    }
  }

  // ?tour=1 (the written tour's "Take the tour on the page" link) opens it at once: the visitor asked for it.
  if (/[?&]tour=1(&|$)/.test(window.location.search)) start(document.querySelector("[data-tour-offer] [data-tour-start]") || helpButton);
})();
