# Accessibility conformance report

The CEDARS platform against WCAG 2.2 Level AA, criterion by criterion, with how each is met, the evidence for it, and the known
exceptions. The architecture requires this report before program administrators' users are onboarded (CEDARS Replacement Architecture,
security and delivery: "an independent penetration test and an accessibility conformance report before PA users are onboarded").
Target: decision K, WCAG 2.2 AA, a California state agency requirement; BRD v0.5 BR-10.3 ("all public and authenticated pages and
published documents"). How the checks run day to day is in `ACCESSIBILITY.md`; the visual rules that keep them are in `DESIGN.md`.

It follows the shape of an ACR in the VPAT 2.5 WCAG edition. It is written from the code and the tests in this
repository; an independent audit (below, under "Before PA onboarding") is still to be done, and its findings replace any claim here
they contradict.

| | |
| --- | --- |
| Product | CEDARS platform (this repository), every page in `tests/a11y/pages.py` and every file it generates |
| Version | `cedars.__version__` 0.2.0 and later, as of 2026-11-01 |
| Standard | WCAG 2.2, Levels A and AA (55 success criteria; 4.1.1 Parsing is obsolete in 2.2 and not listed) |
| Methods | axe-core 4.10.2 in Chromium on every listed page in both color schemes (CI, every pull request); Playwright tests for reflow, focus, the sticky header, the tour, help dialogs and markup axe does not check; the document accessibility check on every generated workbook (unit tests); design rules enforced by tests (`test_page_help.py`, `test_us_spelling.py`, `test_page_patterns.py`, `test_form_fields.py`); the ReCET NVDA walk-through for the templates the platform reuses |
| Not yet done | an NVDA (or JAWS) walk-through of the platform's own pages, a manual audit by someone other than the builders, VoiceOver on iOS and macOS |

## Terms

- **Supports**: the criterion is met, by the means and evidence given.
- **Partially supports**: met for the pages, or for part of the content, and not for the rest (the remark says which).
- **Not applicable**: the platform has no content the criterion is about.
- **Evidence** names the check that holds it: *axe* (CI on every listed page, light and dark), *test* (a named Playwright or unit test
  in CI), *rule* (a design rule in `DESIGN.md` / `CLAUDE.md` that review enforces, without an automated check), *ReCET NVDA* (the
  walk-through in ReCET `tests/screenreader`, NVDA 2026.2 with Chrome, 2026-10-29, on the templates and scripts this platform took over).

## Evidence in numbers (2026-11-01)

- 305 page states in `tests/a11y/pages.py` (signed out, a second-factor set-up, and eight people: a CPUC system administrator, a CPUC
  approver, a PG&E administrator, analyst and PII filer, a measure developer, an SCE member and a community member), each checked by axe with the WCAG 2.0, 2.1 and 2.2 A and AA rules in the light
  and the dark scheme: 610 axe runs, none with a violation, with every disclosure on the page opened and each header menu opened in turn.
- Each of those pages at 320 px wide with no sideways scroll, no id used twice, every checkbox and radio at least 24 x 24 px, every
  `summary` at least 24 px tall, every table with a caption, no label ending in a colon.
- The sticky header never hides the focused element (Shift+Tab back up long pages, each link scrolled into view from below) and scrolls
  away where there is no room (320 x 640, 1280 x 400).
- The guided tour and every page's help dialog by keyboard alone, in both schemes, and without scripts.
- Every generated workbook (budget filing appendix, record downloads, dashboard downloads, the claims and dictionary upload templates)
  has no finding from the document accessibility check (`tests/test_document_accessibility.py`, `test_budgets.py`, `test_dictionary.py`).

## Pages: WCAG 2.2 A and AA

### Perceivable

| Criterion | Level | Conformance | How it is met | Evidence |
| --- | --- | --- | --- | --- |
| 1.1.1 Non-text Content | A | Supports | Icons and the cedar mark are decorative (`aria-hidden`, `focusable="false"`) or named; every chart (dashboards, restatement figures) has a table with the same numbers beside it; generated workbooks have no pictures | axe; rule |
| 1.2.1 Audio-only and Video-only | A | Not applicable | No audio or video on any page | |
| 1.2.2 Captions (Prerecorded) | A | Not applicable | No video | |
| 1.2.3 Audio Description or Media Alternative | A | Not applicable | No video. Training videos (BR-12.3) are not built; when they are, they need captions and a transcript | |
| 1.2.4 Captions (Live) | AA | Not applicable | No live media | |
| 1.2.5 Audio Description (Prerecorded) | AA | Not applicable | No video | |
| 1.3.1 Info and Relationships | A | Supports | Server-rendered semantic HTML: one `h1` per page and sections with headings in order, landmarks (`header`, `nav`, `main`, `footer`), tables with captions and `th scope`, fields with labels and `aria-describedby` hints, fieldsets with legends, lists as lists, breadcrumbs as an ordered list | axe; test (captions, labels); ReCET NVDA |
| 1.3.2 Meaningful Sequence | A | Supports | Source order is reading order; no CSS reordering of content; a field's label, control, errors and hint stay together | rule; test (reflow) |
| 1.3.3 Sensory Characteristics | A | Supports | Instructions name controls by their label ("choose Apply"), never by shape, place or color | rule |
| 1.3.4 Orientation | AA | Supports | No orientation lock; the layout reflows to one column | test (320 px) |
| 1.3.5 Identify Input Purpose | AA | Supports | Sign-in and account fields carry `autocomplete` (email, current-password, new-password, one-time-code from django-allauth); fields that are not about the user turn it off | rule |
| 1.4.1 Use of Color | A | Supports | Every status is a word in a badge (`.status`), never color alone; links in text keep the browser's underline; flagged cells carry a symbol | rule; axe (link-in-text-block) |
| 1.4.2 Audio Control | A | Not applicable | No audio | |
| 1.4.3 Contrast (Minimum) | AA | Supports | Color tokens chosen for 4.5:1 text in both schemes (ratios beside them in `app.css`) | axe (color-contrast, both schemes) |
| 1.4.4 Resize Text | AA | Supports | Sizes in rem; nothing is cut off at 200% zoom | test (320 px is 400% of 1280); manual at 200% before a page ships |
| 1.4.5 Images of Text | AA | Supports | No text in images; the header's name is text | rule |
| 1.4.10 Reflow | AA | Supports | One column at 320 px; wide tables scroll inside their own named region (`.hscroll`), which the criterion allows for data tables | test (every page, every disclosure open) |
| 1.4.11 Non-text Contrast | AA | Supports | `--control` (field and button borders) and `--focus` (the focus ring) at 3:1 in both schemes | rule (ratios in `app.css`); axe does not measure borders |
| 1.4.12 Text Spacing | AA | Supports | No fixed heights on text containers; checked by hand with the WCAG text-spacing bookmarklet before a page ships | manual |
| 1.4.13 Content on Hover or Focus | AA | Supports | No tooltips or hover-only content (`title` is never the only way to learn something); the header menus, help dialog and tour open on activation and close with Escape | rule; test (tour, help by keyboard) |

### Operable

| Criterion | Level | Conformance | How it is met | Evidence |
| --- | --- | --- | --- | --- |
| 2.1.1 Keyboard | A | Supports | Native links, buttons, fields and `details`; nothing is mouse-only (no hover or double-click actions, no drag) | test (tour, help, Other filters by keyboard); ReCET NVDA |
| 2.1.2 No Keyboard Trap | A | Supports | The help dialog and tour release the focus with Escape and their buttons; there is no editable grid on the platform (Edit inputs is a table of rows with a form per row) | test |
| 2.1.4 Character Key Shortcuts | A | Supports | No single-character shortcuts; the tour uses the arrow keys and Escape only while it is open | rule |
| 2.2.1 Timing Adjustable | A | Supports | No time limits on pages or forms; a session lasts Django's default two weeks; due dates are business deadlines, not interface timeouts | rule |
| 2.2.2 Pause, Stop, Hide | A | Supports | Nothing moves, blinks or scrolls by itself. A running calculation's page and the runs list update their status text every 5 to 10 s, which is the page's purpose; without scripts they do not update, and they reload only when nothing has the focus | rule; ReCET NVDA (reloadWhenIdle) |
| 2.3.1 Three Flashes | A | Supports | Nothing flashes | rule |
| 2.4.1 Bypass Blocks | A | Supports | "Skip to content" is the first stop on every page (`core/base.html`, to `main#main`); landmarks | axe (bypass, region) |
| 2.4.2 Page Titled | A | Supports | Every page's `title` is its own name, then "CEDARS" | axe (document-title); rule |
| 2.4.3 Focus Order | A | Supports | Source order; the help dialog and tour move the focus into themselves and back to what opened them | test |
| 2.4.4 Link Purpose (In Context) | A | Supports | Repeated links ("Download", "Open") carry the object in visually hidden text ("Download<span class=sr-only> report.pdf</span>") | axe; rule |
| 2.4.5 Multiple Ways | AA | Supports | Header navigation, the front page, Help with an index of every page's help (`/help/pages/`), search on the program catalog, documents and studies, breadcrumbs | rule |
| 2.4.6 Headings and Labels | AA | Supports | Headings and labels name their content; every page heading has a help (?) that describes the page | test (`test_page_help.py`) |
| 2.4.7 Focus Visible | AA | Supports | `:focus-visible` ring in `--focus` on every control | rule; axe does not test it; test (the header never hides it) |
| 2.4.11 Focus Not Obscured (Minimum) | AA | Supports | The sticky header is held to it: no focused link or field is hidden under it, and it scrolls away on short or narrow windows | test (`test_header_stays_in_view_and_never_hides_the_focus`, `test_header_scrolls_away_where_there_is_no_room`) |
| 2.5.1 Pointer Gestures | A | Supports | No multipoint or path gestures | rule |
| 2.5.2 Pointer Cancellation | A | Supports | Native controls act on release | rule |
| 2.5.3 Label in Name | A | Supports | A control's accessible name starts with its visible label (hidden text is only appended) | rule |
| 2.5.4 Motion Actuation | A | Not applicable | No motion input | |
| 2.5.7 Dragging Movements | AA | Supports | No dragging anywhere on the platform (ReCET's Tabulator grid, whose column widths drag, is not used here) | rule |
| 2.5.8 Target Size (Minimum) | AA | Supports | Buttons 36 px, inline buttons, checkboxes, radios and `summary` at least 24 x 24 px; links inside sentences are exempt | axe (target-size); test (checkbox, radio and summary sizes) |

### Understandable

| Criterion | Level | Conformance | How it is met | Evidence |
| --- | --- | --- | --- | --- |
| 3.1.1 Language of Page | A | Supports | `<html lang="en">` | axe (html-has-lang, html-lang-valid) |
| 3.1.2 Language of Parts | AA | Supports | All content is English (US spelling, `test_us_spelling.py`); data values filed by PAs are shown as filed | rule |
| 3.2.1 On Focus | A | Supports | Focus never changes the page | rule |
| 3.2.2 On Input | A | Supports | Filters and view controls change nothing until Apply; no select submits on change | rule (`DESIGN.md` filters); ReCET 0.1.67 |
| 3.2.3 Consistent Navigation | AA | Supports | One base template: the same header, navigation and footer in the same order on every page | rule |
| 3.2.4 Consistent Identification | AA | Supports | One design system: the same function has the same name and look everywhere (`DESIGN.md`) | rule |
| 3.2.6 Consistent Help | A | Supports | Help in the header and the support and feedback links in the footer, in the same place on every page; a (?) beside every heading | test (`test_page_help.py`) |
| 3.3.1 Error Identification | A | Supports | Errors in text, beside the field (`.field-errors`, `aria-describedby`) and as a page alert (`role="alert"`) | rule; axe |
| 3.3.2 Labels or Instructions | A | Supports | Every field has a visible label and, where needed, a hint | axe (label); test (`test_form_fields.py`) |
| 3.3.3 Error Suggestion | AA | Supports | Messages say what to do ("Say why the settings change", "Enter a plain number in NumUnits"); validation findings carry a fix | rule |
| 3.3.4 Error Prevention (Legal, Financial, Data) | AA | Supports | Filings go through check, review and an explicit confirm; a confirmed version is never changed, a correction is a new version with a reason, and a confirmation can be withdrawn before the due date | rule (versions, not edits) |
| 3.3.7 Redundant Entry | A | Supports | A form returned with an error keeps what was entered; a rerun, a correction or a new version starts from the previous one's values | rule |
| 3.3.8 Accessible Authentication (Minimum) | AA | Supports | Email and password (paste and password managers allowed); a second factor by authenticator code (`autocomplete="one-time-code"`, paste allowed), a security key (WebAuthn) or a recovery code; no CAPTCHA or puzzle; single sign-on through the CPUC's identity provider (OIDC) when configured | rule |

### Robust

| Criterion | Level | Conformance | How it is met | Evidence |
| --- | --- | --- | --- | --- |
| 4.1.2 Name, Role, Value | A | Supports | Native elements first; ARIA only where they fall short (the help dialog, the tour, `aria-current` in navigation, `aria-disabled` instead of disabling the focused control) | axe; ReCET NVDA |
| 4.1.3 Status Messages | AA | Supports | Page messages are a `role="status"` list (errors `role="alert"`); text that updates on its own is announced through the one page-wide live region (`window.cedarsAnnounce`), never a new live region | rule; ReCET NVDA (announcer) |

## Published documents

BR-10.3 covers published documents as well as pages. The platform publishes two kinds: files it writes itself, and files people upload
to the document library or attach to comments.

### Files the platform writes

Every workbook is written by `cedars/core/workbooks.py`, so each is accessible the same way:

| What | Where |
| --- | --- |
| Budget filing appendix (BR-04.5) | `budgets.service.appendix_xlsx` |
| Record downloads, XLSX (BR-07.2) | `data.records._xlsx` |
| Dashboard downloads, XLSX | `data.downloads._xlsx` |
| Claims upload template | `claims.spec.template_xlsx` |
| Data dictionary upload templates | `dictionary.service.template_xlsx` |

| Criterion | How a generated workbook meets it |
| --- | --- |
| 1.1.1 Non-text Content | No pictures, charts or shapes; what a page shows as a chart is rows in the workbook |
| 1.3.1 Info and Relationships | Each sheet's rows are an Excel table with a marked header row (what screen readers announce column by column, and what Excel's accessibility checker asks for); the header repeats on printed pages (print titles) and is frozen on screen; header names are unique and never blank |
| 1.3.2 Meaningful Sequence | One header row, rows under it; no merged cells, no layout tables, no blank spacer rows or columns |
| 2.4.2 Page Titled | The workbook's title, subject and creator are set in its properties |
| 2.4.6 Headings and Labels | Every sheet is named for what it holds ("Budget", "Cost-effectiveness", "Data", "About"); no "Sheet1" |
| 3.1.1 Language | The workbook's language is en-US |
| 1.4.3 Contrast | Table style "Light 1": black text on white and light gray stripes, no colored header fill |

CSV and Parquet downloads are data formats with a header row and no presentation; the CSV's stamp lines start with `#`. Generated
letters and pages (change letters, release notes, the limits page) are HTML pages and covered above.

### Files people upload

PDFs and workbooks stored as a document's file, or attached to a public comment on a document or to a comment on a filing, are checked
as they are stored (`cedars/services/accessibility`, BR-10.3):

| Finding | Kind | WCAG 2.2 |
| --- | --- | --- |
| PDF is not tagged (no structure tree, or not marked as tagged) | PDF | 1.3.1, 1.3.2, 4.1.2 |
| PDF has no text on its first 20 pages (scanned pages) | PDF | 1.1.1, 1.4.5 |
| PDF has no title (neither its document information nor its XMP metadata) | PDF | 2.4.2 |
| PDF has no language (`/Lang`) | PDF | 3.1.1 |
| Sheet with data has no marked header row (no table with a header row, no print titles, no "Title" defined name) | Workbook | 1.3.1 |
| Sheet has merged cells | Workbook | 1.3.1, 1.3.2 |
| Picture, chart or shape has no alternative text and is not marked decorative | Workbook | 1.1.1 |
| Sheet keeps a default name ("Sheet1") | Workbook | 2.4.6 |

The uploader is warned on the page they land on, with how to fix each finding; the document's page tells everyone that a flagged file
may be hard to read and links to the support page to ask for an accessible copy; CPUC system administrators see every flagged file on
Administration, Document accessibility, and mark each reviewed with a note. Nothing is refused by default. The CPUC can turn blocking on
(Document accessibility settings, with a reason, audited) for the findings it chooses; files brought over from the legacy sites (the
PDA, the PSR, CEDARS' own documents) are checked and listed but never refused.

## Known exceptions and limits

1. **Documents filed by others are as accessible as their authors made them.** The platform checks and flags, and blocks only when the
   CPUC turns blocking on; it does not remediate. The legacy PDA's documents (thousands of PDFs since 2013, many scanned) arrive as they
   are. Remedy: the alternative-format request on every document page, the review list, and blocking for new uploads once PAs are told.
   Conformance for uploaded documents: **Partially supports** 1.1.1, 1.3.1, 1.3.2, 2.4.2, 3.1.1.
2. **The PDF check is structural.** It finds a missing structure tree, text, title or language; it does not judge the quality of the
   tags (reading order, figure alternative text, table headers inside the PDF, PDF/UA). A PDF that passes may still need a manual check
   with a PDF/UA checker (for example PAC) for anything published as a CPUC deliverable.
3. **Word, PowerPoint, older Excel formats (.xls, .xlsb), images and zip files are not checked.** They are stored and listed under
   their document as they are. Word and PowerPoint checks (headings, alternative text, language) are the natural next step.
4. **No screen-reader pass of the platform's own pages yet.** The screen-reader evidence is ReCET's NVDA walk-through of the templates,
   styles and scripts the platform took over; the platform's pages are built from them and checked by axe, but have not been walked
   with NVDA or JAWS. See below.
5. **axe finds about a third to a half of WCAG failures.** The criteria marked *rule* or *manual* rest on review and on checks by hand
   before a page ships (`ACCESSIBILITY.md`); they are not re-checked on every change.
6. **Training videos (BR-12.3) are not built.** When they are, they need captions, a transcript and audio description where the
   picture carries information (1.2.2, 1.2.3, 1.2.5).

## Before PA onboarding

What the architecture's "accessibility conformance report before PA users are onboarded" still needs, in order:

1. An NVDA walk-through of the platform on Windows with Chrome, on the model of ReCET `tests/screenreader` (Guidepup + Playwright):
   sign in with a second factor, the front page and dashboards (charts and their tables), a budget filing from upload to confirmation,
   a claims upload with findings, Edit inputs, a document upload with an accessibility warning, the help dialog and the tour. Record the
   transcripts and fix what they show.
2. A manual audit by someone who did not build the pages (the CPUC's accessibility coordinator or a contracted auditor) against this
   report, including keyboard-only use, 200% zoom, text spacing, Windows high contrast, and VoiceOver on macOS and iOS.
3. A decision by the CPUC on turning document blocking on, and for which findings, with notice to the PAs as a release note before it
   takes effect.
4. This report updated with the results, dated, and published with the release that onboards PA users.
