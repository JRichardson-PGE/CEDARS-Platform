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

  // A link to a folded answer or section (#id on a closed <details>, such as an FAQ answer) opens it, on arrival and when the address
  // changes; without scripts the link still lands on its question, one activation from the answer.
  function openTarget() {
    var id;
    try { id = decodeURIComponent(window.location.hash.slice(1)); } catch (e) { return; }
    var el = id && document.getElementById(id);
    while (el) {
      if (el.tagName === "DETAILS") el.open = true;
      el = el.parentElement;
    }
  }
  openTarget();
  window.addEventListener("hashchange", openTarget);

  // Header navigation. The menus are native <details>, so they open and close with the keyboard without any script; this adds the
  // conventions people expect of a menu bar: one open at a time, Escape closes and returns focus to the menu's button, a click outside closes.
  var nav = document.querySelector(".top nav");
  if (nav) {
    var menus = nav.querySelectorAll("details");
    nav.addEventListener("toggle", function (e) {
      if (!e.target.open) return;
      menus.forEach(function (d) { if (d !== e.target) d.open = false; });
    }, true);
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      menus.forEach(function (d) {
        if (d.open) { d.open = false; d.querySelector("summary").focus(); }
      });
    });
    document.addEventListener("click", function (e) {
      menus.forEach(function (d) { if (d.open && !d.contains(e.target)) d.open = false; });
    });
  }

  // The header sticks to the top of a wide, tall enough window (app.css). Its height goes to --header-h, which keeps focused and linked
  // content clear of it (2.4.11); without this script a fixed allowance is used. When its navigation wraps to a second row it is marked
  // header-wrapped and scrolls away with the page instead of covering a fifth of the window.
  var header = document.querySelector("body > header");
  var bar = header && header.querySelector(".top");
  if (header && window.ResizeObserver) {
    new ResizeObserver(function () {
      document.documentElement.style.setProperty("--header-h", header.offsetHeight + "px");
      if (bar) {
        var brand = bar.querySelector(".brand"), who = bar.querySelector(".who");
        var wrapped = !!(brand && who) && who.getBoundingClientRect().top - brand.getBoundingClientRect().top > 20;
        document.documentElement.classList.toggle("header-wrapped", wrapped);
      }
    }).observe(header);
  }

  // "Copy" buttons ([data-copy]: the id of the field whose value to copy) show only where the clipboard is available; the field itself
  // stays selectable, so the value can always be copied by hand. The result is announced, not shown by colour.
  if (navigator.clipboard && window.isSecureContext) {
    document.querySelectorAll("button[data-copy]").forEach(function (b) {
      var src = document.getElementById(b.getAttribute("data-copy"));
      if (!src) return;
      b.hidden = false;
      b.addEventListener("click", function () {
        var text = "value" in src ? src.value : src.textContent;
        navigator.clipboard.writeText(text).then(function () {
          window.cedarsAnnounce("Copied");
          b.textContent = "Copied";
          window.setTimeout(function () { b.textContent = "Copy the key"; }, 2000);
        }, function () { window.cedarsAnnounce("Could not copy; select the key and copy it by hand"); });
      });
    });
  }

  // Narrow screens: a Menu button shows and hides the navigation. Without this script the button stays hidden and the list is simply shown.
  var btn = document.querySelector(".menu-btn");
  if (btn && nav) {
    document.documentElement.classList.add("js");
    var mq = window.matchMedia("(max-width: 860px)");
    var apply = function () {
      var current = nav.querySelector("[aria-current='page']");
      // Collapsed by default on a narrow screen; the current page is still announced by the page title and heading.
      var collapsed = mq.matches && btn.getAttribute("aria-expanded") !== "true";
      nav.classList.toggle("collapsed", collapsed);
      btn.hidden = !mq.matches;
      void current;
    };
    btn.addEventListener("click", function () {
      btn.setAttribute("aria-expanded", btn.getAttribute("aria-expanded") === "true" ? "false" : "true");
      apply();
    });
    mq.addEventListener("change", apply);
    apply();
  }

  // Info buttons (core/_info_button.html): each opens its hint, a native popover, with a click, a tap, Enter or Space, and Escape or a click
  // elsewhere closes it, all without this script. This places the hint just under its button (kept inside the window, and kept there while
  // the page scrolls), and opens it when a mouse rests on the button. A hint opened by hovering closes when the pointer has left both the
  // button and the hint (so the pointer can move onto it, 1.4.13); a click on the button keeps it open until dismissed.
  var infoButtons = document.querySelectorAll("button.info-btn[popovertarget]");
  if (infoButtons.length && HTMLElement.prototype.hasOwnProperty("popover")) {
    var place = function (btn, pop) {
      var r = btn.getBoundingClientRect();
      pop.setAttribute("data-placed", "");
      pop.style.left = "0px";
      pop.style.top = "0px";
      var w = pop.offsetWidth, h = pop.offsetHeight, gap = 6, edge = 8;
      var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
      var left = Math.max(edge, Math.min(r.left, vw - w - edge));
      var top = r.bottom + gap;
      if (top + h > vh - edge && r.top - gap - h >= edge) top = r.top - gap - h;  // no room below: above the button
      pop.style.left = left + "px";
      pop.style.top = top + "px";
    };
    infoButtons.forEach(function (btn) {
      var pop = document.getElementById(btn.getAttribute("popovertarget"));
      if (!pop) return;
      var byHover = false, closeTimer = null, openTimer = null;
      var isOpen = function () { return pop.matches(":popover-open"); };
      var cancelClose = function () { window.clearTimeout(closeTimer); };
      var closeSoon = function () {
        window.clearTimeout(openTimer);
        if (!byHover) return;
        cancelClose();
        closeTimer = window.setTimeout(function () { if (byHover && isOpen()) pop.hidePopover(); }, 300);
      };
      var onScroll = function () { place(btn, pop); };
      pop.addEventListener("toggle", function (e) {
        if (e.newState === "open") {
          place(btn, pop);
          window.addEventListener("scroll", onScroll, true);
          window.addEventListener("resize", onScroll);
        } else {
          byHover = false;
          window.removeEventListener("scroll", onScroll, true);
          window.removeEventListener("resize", onScroll);
        }
      });
      btn.addEventListener("pointerenter", function (e) {
        if (e.pointerType !== "mouse") return;  // a touch opens it with the tap
        cancelClose();
        if (isOpen()) return;
        openTimer = window.setTimeout(function () {
          if (isOpen()) return;
          byHover = true;
          pop.showPopover();
        }, 250);
      });
      btn.addEventListener("pointerleave", closeSoon);
      pop.addEventListener("pointerenter", cancelClose);
      pop.addEventListener("pointerleave", closeSoon);
      btn.addEventListener("click", function (e) {
        window.clearTimeout(openTimer);
        if (byHover && isOpen()) { e.preventDefault(); byHover = false; cancelClose(); }  // opened by hovering: the click keeps it open
      });
    });
  }

  // The PA picker (core/_pa_picker.html). Without this script it is a list of plain checkboxes in a <details>. With it: the All PAs and
  // group boxes are tri-state (half ticked when some of their PAs are), ticking one ticks everything under it, each level shows and hides
  // with a button, the closed picker names what it keeps, Escape closes it, and on submit a whole PA type goes as its pa_type (it keeps
  // PAs typed later too), everything ticked as no PA filter at all and nothing ticked likewise (every PA).
  document.querySelectorAll("[data-pa-picker]").forEach(function (picker) {
    document.documentElement.classList.add("js-pa");
    var menu = picker.querySelector(".pa-menu");
    var all = picker.querySelector("[data-pa-all]");
    var sw = picker.querySelector("[data-pa-statewide]");
    var summary = picker.querySelector("[data-pa-summary]");
    var groups = Array.prototype.map.call(picker.querySelectorAll("[data-pa-group]"), function (li) {
      return {
        li: li, label: li.getAttribute("data-label"), type: li.getAttribute("data-type"),
        box: li.querySelector("[data-pa-group-box]"),
        members: Array.prototype.slice.call(li.querySelectorAll("input[name='pa']")),
      };
    });
    picker.querySelectorAll("[data-pa-all], [data-pa-group-box], .pa-twisty").forEach(function (el) { el.hidden = false; });

    var full = function (g) { return g.members.length > 0 && g.members.every(function (m) { return m.checked; }); };
    var some = function (g) { return g.members.some(function (m) { return m.checked; }); };
    var words = function () {
      var picked = groups.some(some) || (sw && sw.checked);
      if (!picked || (groups.every(full) && (!sw || sw.checked))) return sw ? "All PAs and statewide programs" : "All PAs";
      var parts = [];
      if (groups.every(full)) parts.push("All PAs");
      else groups.forEach(function (g) {
        if (full(g)) parts.push(g.type ? "All " + g.label : g.label);
        else g.members.forEach(function (m) { if (m.checked) parts.push(m.value); });
      });
      if (sw && sw.checked) parts.push("Statewide");
      var shown = parts.slice(0, 3).join(", ");
      return parts.length <= 3 ? shown : shown + " and " + (parts.length - 3) + " more";
    };
    var sync = function () {
      groups.forEach(function (g) {
        g.box.checked = full(g);
        g.box.indeterminate = !g.box.checked && some(g);
      });
      all.checked = groups.length > 0 && groups.every(full);
      all.indeterminate = !all.checked && groups.some(some);
      summary.textContent = words();
    };

    // A ticked group ticks its PAs; nothing ticked at all means every PA, so it opens with everything ticked.
    groups.forEach(function (g) { if (g.box.checked) g.members.forEach(function (m) { m.checked = true; }); });
    if (!picker.querySelector("input[type='checkbox']:checked")) {
      groups.forEach(function (g) { g.members.forEach(function (m) { m.checked = true; }); });
      if (sw) sw.checked = true;
    }
    sync();

    picker.addEventListener("change", function (e) {
      var t = e.target;
      if (t === all) groups.forEach(function (g) { g.members.forEach(function (m) { m.checked = all.checked; }); });
      groups.forEach(function (g) {
        if (t === g.box) g.members.forEach(function (m) { m.checked = g.box.checked; });
      });
      sync();
    });

    // Show and hide: All PAs starts open; a group starts closed unless only some of its PAs are ticked.
    picker.querySelectorAll(".pa-twisty").forEach(function (b) {
      var list = document.getElementById(b.getAttribute("aria-controls"));
      var set = function (open) { b.setAttribute("aria-expanded", open ? "true" : "false"); list.hidden = !open; };
      var g = groups.filter(function (x) { return x.li.contains(b) && x.li.querySelector(".pa-twisty") === b; })[0];
      set(!g || (some(g) && !full(g)));
      b.addEventListener("click", function () { set(b.getAttribute("aria-expanded") !== "true"); });
    });

    picker.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && menu.open) { e.stopPropagation(); menu.open = false; menu.querySelector("summary").focus(); }
    });
    document.addEventListener("click", function (e) { if (menu.open && !picker.contains(e.target)) menu.open = false; });

    var form = picker.closest("form");
    if (form) form.addEventListener("formdata", function (e) {
      var fd = e.formData;
      fd.delete("pa");
      fd.delete("pa_type");
      if (groups.every(full) && (!sw || sw.checked)) return;  // everything: no PA filter
      groups.forEach(function (g) {
        if (full(g) && g.type) fd.append("pa_type", g.type);
        else g.members.forEach(function (m) { if (m.checked) fd.append("pa", m.value); });
      });
      if (sw && sw.checked) fd.append("pa", sw.value);
    });
  });

  // Filing calendar, year view: a date's tooltip shows on hover and keyboard focus (CSS); Escape hides it without moving the focus or the
  // pointer, and it comes back the next time the date is reached (WCAG 1.4.13, dismissible).
  var yearGrid = document.getElementById("year-grid");
  if (yearGrid) {
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      yearGrid.querySelectorAll(".ycell").forEach(function (cell) {
        if (cell.matches(":hover") || cell.contains(document.activeElement)) cell.classList.add("tip-off");
      });
    });
    var reset = function (e) {
      var cell = e.target.closest && e.target.closest(".ycell");
      if (cell && !cell.contains(e.relatedTarget)) cell.classList.remove("tip-off");
    };
    yearGrid.addEventListener("mouseout", reset);
    yearGrid.addEventListener("focusout", reset);
  }
})();
