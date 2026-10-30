# Accessibility

Target: WCAG 2.2 Level AA on every page and published document (decision K; BRD v0.5 BR-10.3).

## Checked automatically (CI)

- axe-core 4.10.2 on every page in `tests/a11y/pages.py`, in the light and dark colour schemes, with the WCAG 2.0/2.1/2.2 A and AA rules.
- No sideways scroll at 320 px wide (1.4.10 reflow).

## Checked by hand before a page ships

- Keyboard: every action reachable by Tab, operable by Enter or Space, focus visible, no trap, a logical order; the skip link works.
- Screen reader: headings and landmarks make sense, every control is named, status changes are announced (through `window.cedarsAnnounce`).
  ReCET's NVDA walk-through (`tests/screenreader` in ReCET: Guidepup + Playwright on Windows) is the model for the platform's.
- 200% zoom, and text spacing overrides, without lost content.
- Colour: any new colour checked with a contrast calculator in both schemes before it is used.
