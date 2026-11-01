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
})();
