# Accessibility

Target: WCAG 2.2 Level AA on every page and published document (decision K; BRD v0.5 BR-10.3).

## Checked automatically (CI)

- axe-core 4.10.2 on every page in `tests/a11y/pages.py`, in the light and dark color schemes, with the WCAG 2.0/2.1/2.2 A and AA rules.
  Every disclosure on the page is opened first; the header's dropdown menus are opened one at a time and the header checked in that state.
- No sideways scroll at 320 px wide (1.4.10 reflow), with every disclosure in the content opened.
- Markup axe does not check (UI review 2, 2026-10-31), on the same page load: no id used twice (several Django forms on one page each get
  their own `auto_id`, e.g. `RenameForm(auto_id="rename_%s")`, or a label's `for` and a hint's `aria-describedby` reach only the first
  form's field); every checkbox and radio at least 24 x 24 px (they are `flex: none`, so a long label in a flex row cannot squeeze them);
  every `summary` at least 24 px tall (2.5.8); every table with a caption (visible or `.sr-only`); no label or legend ending in a
  colon (the field template writes none, DESIGN.md). A scroll box (`.hscroll`, `role="region"`) inside a section of the same name is
  named after its table ("Versions table"), so the two landmarks differ.
- The sticky header (it stays at the top of windows at least 861 px wide and 480 px tall, and scrolls away on smaller ones): backing up a
  long page with Shift+Tab, and scrolling each link and field in the content just into view from below, never leaves one hidden under
  it (2.4.11 focus not obscured); it scrolls away at 320 x 640 and 1280 x 400.
- How it runs: the `scene` fixture in `tests/a11y/test_axe.py` (the organizations, people and records the pages show) is built once
  per test process and every check reads it, with the site served from one thread (`uv run pytest -m a11y -n auto` spreads the checks
  over the cores; CI does). So a page must not change data when it is shown (GET), and a page that needs a record the scene lacks adds
  it to the scene. A signed-in check that lands on the sign-in page fails.

## Checked by hand before a page ships

- Keyboard: every action reachable by Tab, operable by Enter or Space, focus visible, no trap, a logical order; the skip link works.
- Screen reader: headings and landmarks make sense, every control is named, status changes are announced (through `window.cedarsAnnounce`).
  ReCET's NVDA walk-through (`tests/screenreader` in ReCET: Guidepup + Playwright on Windows) is the model for the platform's.
- 200% zoom, and text spacing overrides, without lost content.
- Color: any new color checked with a contrast calculator in both schemes before it is used.
