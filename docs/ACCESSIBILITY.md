# Accessibility

Target: WCAG 2.2 Level AA on every page and published document (decision K; BRD v0.5 BR-10.3).

## Checked automatically (CI)

- axe-core 4.10.2 on every page in `tests/a11y/pages.py`, in the light and dark colour schemes, with the WCAG 2.0/2.1/2.2 A and AA rules.
  Every disclosure on the page is opened first; the header's dropdown menus are opened one at a time and the header checked in that state.
- No sideways scroll at 320 px wide (1.4.10 reflow).
- How it runs: the `scene` fixture in `tests/a11y/test_axe.py` (the organisations, people and records the pages show) is built once
  per test process and every check reads it, with the site served from one thread (`uv run pytest -m a11y -n auto` spreads the checks
  over the cores; CI does). So a page must not change data when it is shown (GET), and a page that needs a record the scene lacks adds
  it to the scene. A signed-in check that lands on the sign-in page fails.

## Checked by hand before a page ships

- Keyboard: every action reachable by Tab, operable by Enter or Space, focus visible, no trap, a logical order; the skip link works.
- Screen reader: headings and landmarks make sense, every control is named, status changes are announced (through `window.cedarsAnnounce`).
  ReCET's NVDA walk-through (`tests/screenreader` in ReCET: Guidepup + Playwright on Windows) is the model for the platform's.
- 200% zoom, and text spacing overrides, without lost content.
- Colour: any new colour checked with a contrast calculator in both schemes before it is used.
