// Page help and guided tours (Jake, 2026-10-31). Every page's heading has a (?) button (cedars/core/page_help.py). Without scripts it
// is a link to the page's help on a page of its own, with the tour written out. With scripts:
//   The (?) opens a modal dialog with the page's guide and a "Take a tour of this page" button; Escape or Close shuts it and the focus
//   goes back to the (?).
//   The tour is a modal dialog that shows one step at a time beside the part of the page it describes. Next and Back move (and the
//   arrow keys), Escape or Close the tour leaves, Tab stays inside the dialog (showModal). Each step moves the focus to the step's
//   heading ("Step 2 of 7: ..."), which is the dialog's name; the text is its description. Back on the first step is aria-disabled,
//   never disabled, so the focus is never lost (CLAUDE.md). The part of the page a step describes gets a focus-colored outline and the
//   rest is dimmed (and still announced in words). A step whose part is not on the page (a section only some people see) is left out.
//   The outline and the dimming are drawn by one spotlight box laid over the page, not on the part itself: drawn on the part they were
//   cut off by any scrolling box around it (a wide table's), hidden under the sticky header, and limited to a heading's own line.
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
  function rank(el) { var m = /^H([1-6])$/.exec(el.tagName); return m ? +m[1] : 0; }
  function area(el) {
    if (!el) return null;
    var tag = el.tagName;
    if (tag === "LEGEND" || tag === "SUMMARY") return el.parentElement;
    if (tag === "CAPTION") { var table = el.closest("table"); return table.closest(".hscroll") || table; }
    if (/^H[2-4]$/.test(tag)) {
      var box = el.closest("section, .card, details");
      if (box && box.querySelector("h2, h3, h4, summary") === el) return box;
    }
    if (tag === "TABLE" && el.parentElement && el.parentElement.classList.contains("hscroll")) return el.parentElement;
    return el;
  }
  // What a step outlines: its part, and for a heading that heads no box of its own, what follows it up to the next heading of its rank
  // or higher (the heading alone is one line, with the table or list it names left out).
  function parts(el) {
    var out = [el], r = rank(el);
    if (r < 2 || r > 4) return out;
    var from = el;
    while (from.parentElement && from.parentElement.tagName !== "MAIN" && from.parentElement.children.length === 1) from = from.parentElement;
    for (var sib = from.nextElementSibling; sib; sib = sib.nextElementSibling) {
      if (rank(sib) && rank(sib) <= r) break;
      var inner = sib.querySelector("h1, h2, h3, h4");
      if (inner && rank(inner) <= r) break;
      if (visible(sib)) out.push(sib);
    }
    return out;
  }
  // The closed disclosures around a part (not counting one whose summary holds it): the part is on the page but folded away, and its
  // step opens them while it is shown. A part already in view is chosen before one folded away.
  function closedAround(el) {
    var out = [];
    for (var a = el.parentElement; a; a = a.parentElement) {
      if (a.tagName !== "DETAILS" || a.open) continue;
      var summary = a.querySelector(":scope > summary");
      if (!(summary && summary.contains(el))) out.push(a);
    }
    return out;
  }
  function shown(el) { return visible(el) && !closedAround(el).length; }
  function folded(el) {
    var closed = closedAround(el);
    return closed.length > 0 && visible(closed[closed.length - 1]) && getComputedStyle(el).display !== "none";
  }
  function find(step) {
    var el = null, all, i, k, test;
    var tests = [shown, folded];
    if (step.target) {
      all = document.querySelectorAll(step.target);
      for (k = 0; k < tests.length && !el; k++) for (i = 0; i < all.length && !el; i++) if (tests[k](all[i])) el = all[i];
      if (!el && all.length && all[0].closest("body > header")) {
        var menu = document.querySelector('[data-tour="menu"]');
        if (visible(menu)) el = menu;
      }
    } else if (step.heading) {
      var want = norm(step.heading);
      all = Array.prototype.filter.call(document.querySelectorAll(HEADINGS), function (h) { return norm(h.textContent).indexOf(want) === 0; });
      for (k = 0; k < tests.length && !el; k++) {
        test = tests[k];
        for (i = 0; i < all.length && !el; i++) if (test(all[i].tagName === "CAPTION" ? all[i].closest("table") : all[i])) el = all[i];
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
  var shownSteps = [], current = 0, lit = null, opener = null, opened = [];

  // ---------- the spotlight ----------
  // One box over the page, the size of the step's part (with a gap for the outline), whose shadow dims everything else: the header too,
  // and nothing cut off by a scrolling box the part sits in (the box is clipped to what such a box shows). It follows the part while the
  // page scrolls or changes, and places the dialog at the bottom of the window or at the top, whichever hides less of the part.
  var spot = document.createElement("div");
  spot.className = "tour-spot";
  spot.setAttribute("aria-hidden", "true");
  spot.hidden = true;
  document.body.appendChild(spot);
  var litParts = [], clips = [], frame = 0, still = 0, lastKey = "";
  var GAP = 6;  // between the part and its outline

  function clipping(el) {
    var out = [];
    for (var a = el.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
      var s = getComputedStyle(a);
      if (s.overflowX !== "visible" || s.overflowY !== "visible") out.push(a);
    }
    return out;
  }
  function union() {
    var top = Infinity, left = Infinity, bottom = -Infinity, right = -Infinity, i, r;
    for (i = 0; i < litParts.length; i++) {
      r = litParts[i].getBoundingClientRect();
      if (!r.width && !r.height) continue;
      top = Math.min(top, r.top); left = Math.min(left, r.left); bottom = Math.max(bottom, r.bottom); right = Math.max(right, r.right);
    }
    if (top === Infinity) return null;
    for (i = 0; i < clips.length; i++) {
      r = clips[i].getBoundingClientRect();
      top = Math.max(top, r.top); left = Math.max(left, r.left); bottom = Math.min(bottom, r.bottom); right = Math.min(right, r.right);
    }
    return { top: top, left: left, bottom: Math.max(top, bottom), right: Math.max(left, right) };
  }
  function overlap(a, b) {
    var w = Math.min(a.right, b.right) - Math.max(a.left, b.left), h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0 ? w * h : 0;
  }
  function place(u) {
    // The dialog's two places: as it is now, and mirrored to the other edge of the window.
    var d = dialog.getBoundingClientRect(), top = dialog.classList.contains("tour-top"), vh = window.innerHeight;
    var gap = top ? d.top : vh - d.bottom;
    var atBottom = { left: d.left, right: d.right, top: vh - gap - d.height, bottom: vh - gap };
    var atTop = { left: d.left, right: d.right, top: gap, bottom: gap + d.height };
    var view = { top: Math.max(u.top, 0), left: u.left, bottom: Math.min(u.bottom, vh), right: u.right };
    // a part that runs on below the window keeps its start in view: the dialog stays at the bottom
    dialog.classList.toggle("tour-top", u.bottom <= vh && overlap(view, atTop) < overlap(view, atBottom));
  }
  function track() {
    frame = 0;
    if (!lit) return;
    var u = union();
    if (!u) { spot.hidden = true; return; }
    var key = [u.top, u.left, u.bottom, u.right].map(Math.round).join();
    if (key !== lastKey) {
      lastKey = key; still = 0;
      spot.hidden = false;
      spot.style.top = (u.top - GAP) + "px";
      spot.style.left = (u.left - GAP) + "px";
      spot.style.width = (u.right - u.left + 2 * GAP) + "px";
      spot.style.height = (u.bottom - u.top + 2 * GAP) + "px";
    } else if (++still === 3) {
      place(u);  // once the scroll into view has come to rest
    }
    if (still < 30) frame = requestAnimationFrame(track);
  }
  function follow() { still = 0; if (!frame && lit) frame = requestAnimationFrame(track); }
  window.addEventListener("scroll", follow, true);
  window.addEventListener("resize", follow);

  // Brings the part into view below the sticky header: in the middle of the room the dialog leaves when it fits there, else its top.
  function bring(el) {
    var u = union();
    if (!u) return;
    var vh = window.innerHeight, pad = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0;
    var room = vh - pad - dialog.offsetHeight - 32, height = u.bottom - u.top + 2 * GAP, y;
    if (height <= room) y = u.top - GAP - pad - (room - height) / 2;
    else if (height <= vh - pad - 16) y = u.top - GAP - pad - (vh - pad - height) / 2;
    else y = u.top - GAP - pad - 8;
    // a part inside a box that scrolls on its own (other than the window) is brought into that box's view first
    for (var i = 0; i < clips.length; i++) {
      var s = getComputedStyle(clips[i]);
      if (/auto|scroll/.test(s.overflowY) && clips[i].scrollHeight > clips[i].clientHeight + 1) { el.scrollIntoView({ block: "nearest" }); break; }
    }
    window.scrollBy({ top: y, behavior: reduce ? "auto" : "smooth" });
  }

  // A disclosure (a header menu, the filters) is opened while its step is shown, so what it holds is in view, and closed again after;
  // so are the disclosures a part is folded away in.
  function light(el) {
    if (lit) lit.classList.remove("tour-target");
    lit = el;
    // a header menu is its heading's link beside its disclosure: the disclosure is opened
    var d = el && (el.tagName === "DETAILS" ? el : el.querySelector(":scope > details"));
    var needed = el ? closedAround(el).concat(opened.filter(function (o) { return o === d || o.contains(el) || el.contains(o); })) : [];
    if (d) needed.push(d);
    opened = opened.filter(function (o) {
      if (needed.indexOf(o) >= 0) return true;
      o.open = false;
      return false;
    });
    needed.forEach(function (o) { if (!o.open) { o.open = true; opened.push(o); } });
    if (!el) { dialog.classList.remove("tour-top"); spot.hidden = true; litParts = []; clips = []; lastKey = ""; return; }
    el.classList.add("tour-target");  // which part is outlined, for the tests and for styles
    litParts = parts(el);
    // an open header menu's list hangs below the header: it is part of what is outlined
    if (d && d.open && el.closest("body > header")) {
      var list = d.querySelector(":scope > ul");
      if (list) litParts.push(list);
    }
    clips = clipping(el);
    lastKey = "";
    dialog.classList.remove("tour-top");
    bring(el);
    follow();
  }

  function show(i, backward) {
    current = Math.max(0, Math.min(i, shownSteps.length - 1));
    var step = shownSteps[current], el = step.target || step.heading ? find(step) : null;
    // a part gone since the tour started (the page changed under it) is left out, and the tour goes on to the next step
    if ((step.target || step.heading) && !el && shownSteps.length > 1) {
      shownSteps.splice(current, 1);
      show(backward ? current - 1 : current, backward);
      return;
    }
    // The heading reads "Step 2 of 7: Programs": the count on its own line, the colon for screen readers only.
    title.textContent = "";
    var n = document.createElement("span");
    n.className = "tour-count";
    n.textContent = "Step " + (current + 1) + " of " + shownSteps.length;
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
    next.textContent = current === shownSteps.length - 1 ? "Finish" : "Next";
    light(el);
    title.focus();
  }

  function start(from) {
    opener = from || document.activeElement;
    // Only the steps whose part is on this page, for this visitor (worked out when the tour starts, so a page changed since counts).
    shownSteps = steps.filter(function (s) { return !(s.target || s.heading) || !!find(s); });
    if (!shownSteps.length) return;
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

  back.addEventListener("click", function () { if (current > 0) show(current - 1, true); });
  next.addEventListener("click", function () {
    if (current < shownSteps.length - 1) { show(current + 1); return; }
    if (document.querySelector("[data-tour-offer]")) { remember("cedars-tour-done"); hideOffer(); }
    dialog.close();
  });
  close.addEventListener("click", function () { dialog.close(); });
  // app.js closes a header menu on any click outside it; a click in the tour must not close the menu its step has opened.
  dialog.addEventListener("click", function (e) { e.stopPropagation(); });
  // Left and Right arrows move too, unless the focus is on the step's link (where they mean nothing to the tour).
  dialog.addEventListener("keydown", function (e) {
    if (e.target === link) return;
    if (e.key === "ArrowRight" && current < shownSteps.length - 1) { e.preventDefault(); show(current + 1); }
    if (e.key === "ArrowLeft" && current > 0) { e.preventDefault(); show(current - 1, true); }
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
