# Design system

How the platform's pages look, and the rules a page follows so that every page looks like every other. Accessibility rules are in
`ACCESSIBILITY.md`; this file is about the visual system that keeps them without each page re-deciding.

## One stylesheet

`cedars/core/static/core/app.css`, in four parts: tokens, base, components, utilities. No CSS framework, no build step, no CDN (the CSP
allows the origin only). A page uses the classes that exist; a new component is added to the stylesheet once, with a comment saying
what it is for, and never styled inline.

## Tokens

Colour tokens are custom properties on `:root`, with a second set under `prefers-color-scheme: dark`. Each is chosen for its job and
checked for contrast in both schemes (the ratios are in the comments beside them):

| Token | Job |
|---|---|
| `--bg`, `--surface`, `--surface-2` | the page, a raised object on it (card, table, header), a tint inside one |
| `--fg`, `--fg-2`, `--fg-3` | text, secondary text (descriptions, muted), small labels (table headers, tile captions) |
| `--line`, `--line-2` | borders between objects, borders inside one |
| `--primary`, `--primary-fg`, `--primary-hover` | the one brand colour: primary buttons, the current item's bar, the mark |
| `--link` | links. Blue, never red: red means wrong |
| `--focus` | the focus ring, a colour used for nothing else |
| `--control` | borders of fields and buttons (3:1 on both surfaces) |
| `--ok`, `--warn`, `--bad`, `--info` and their `-bg` tints | state: badges, alerts, messages. Always with words; never the only cue |

Type: Public Sans (`--sans`) for everything, JetBrains Mono (`--mono`) for identifiers (PrgIDs, field names, keys). Both are vendored
under `core/fonts/` with their SIL Open Font License. Scale: 28 px page title, 20 px section, 16.8 px subsection, 16 px body, 15 px
tables, 14 px small, 12 px uppercase labels.

Space: multiples of 4 px (`.25rem` steps). Shape: 4 px radius on controls, 6 px on cards and tables, pills for badges. No shadows except
under an open menu.

## App shell

`core/base.html`: an identity bar (agency, product, environment), a header with the brand, five grouped navigation entries (Filings,
Programs, Calculations, Reference, Administration) as native `<details>` disclosures, and a utility cluster (acting-for organisation,
notifications with the unread count, account, sign out), then `main`, then a footer with the agency and the public reference pages.
Below 860 px the navigation stacks behind a Menu button that `app.js` adds; without scripts the list is simply shown. The open group is
marked with `.current` and the page inside it with `aria-current="page"`.

## Components (by class)

| Class | What it is | Notes |
|---|---|---|
| `.head-row` + `h1` + `.actions` | page header: title, description, action buttons | one `.primary` button per page at most |
| `.subnav` | a module's sections as a tab strip | links, not ARIA tabs; current marked by aria and a bar |
| `.filters` | a filter bar: label above each control, Apply at the end | `label` elements stack by default |
| `table.list` (in `.hscroll`) | a data table | `td.num` for numbers; `td[colspan]` alone in the body reads as the empty state |
| `.status` (+ `.ok` `.warn` `.bad` `.info`) | a status badge | the word is always present |
| `.card`, `dl.tiles`, `dl.facts` | an object, summary numbers, key/value facts | cards are for objects, not for a page's main table |
| `.messages`, `.alert`, `ul.notices` | page messages, an inline alert, a notice list | tinted by state |
| `button`, `.button`, `.primary`, `button.link`, `.danger` | the button set | 36 px high, 24 px minimum for inline ones |
| `.field`, `.helptext`, `.field-errors`, `fieldset` | form fields | label, hint, control, error, in that order |

Later additions planned (CEDARS-Research, UI review 2026-10-30): a due-date cell, sortable headers with `aria-sort`, pagination top and
bottom, an empty-state block with its action, active-filter chips, and a `/ui/` gallery page that the axe suite visits.
