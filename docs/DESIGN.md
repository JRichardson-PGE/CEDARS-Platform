# Design system

How the platform's pages look, and the rules a page follows so that every page looks like every other. Accessibility rules are in
`ACCESSIBILITY.md`; this file is about the visual system that keeps them without each page re-deciding.

## One stylesheet

`cedars/core/static/core/app.css`, in four parts: tokens, base, components, utilities. No CSS framework, no build step, no CDN (the CSP
allows the origin only). A page uses the classes that exist; a new component is added to the stylesheet once, with a comment saying
what it is for, and never styled inline.

## Tokens

Color tokens are custom properties on `:root`, with a second set under `prefers-color-scheme: dark`. Each is chosen for its job and
checked for contrast in both schemes (the ratios are in the comments beside them):

| Token | Job |
|---|---|
| `--bg`, `--surface`, `--surface-2` | the page, a raised object on it (card, table, header), a tint inside one |
| `--fg`, `--fg-2`, `--fg-3` | text, secondary text (descriptions, muted), small labels (table headers, tile captions) |
| `--line`, `--line-2` | borders between objects, borders inside one |
| `--primary`, `--primary-fg`, `--primary-hover` | the one brand color: primary buttons, the current item's bar, the mark |
| `--link` | links. Blue, never red: red means wrong |
| `--focus` | the focus ring, a color used for nothing else |
| `--control` | borders of fields and buttons (3:1 on both surfaces) |
| `--ok`, `--warn`, `--bad`, `--info` and their `-bg` tints | state: badges, alerts, messages. Always with words; never the only cue |

Type: Public Sans (`--sans`) for everything, JetBrains Mono (`--mono`) for identifiers (PrgIDs, field names, keys). Both are vendored
under `core/fonts/` with their SIL Open Font License. Scale: 28 px page title, 20 px section, 16.8 px subsection, 16 px body, 15 px
tables, 14 px small, 12 px uppercase labels.

Space: multiples of 4 px (`.25rem` steps). Shape: 4 px radius on controls, 6 px on cards and tables, pills for badges. No shadows except
under an open menu.

## App shell

`core/base.html`: an identity bar (agency, product, environment), a header with the brand, seven navigation entries (Portfolio dashboard,
Programs, Filings, Calculations, Published data, Reference data, Administration; `core/nav.py`), the grouped ones as native `<details>` disclosures, and a utility cluster (acting-for organization,
notifications with the unread count, account, sign out), then `main`, then a footer with the agency and the public reference pages.
The utility cluster's plain controls (My queue, Help, Account, Sign out) are each a 32 px target with a rule between neighbors, so
they read as separate controls. Below 860 px the navigation stacks behind a Menu button that `app.js` adds, and the person's own controls
(`#header-person`) fold behind it too, stacked as the navigation is: the top row is the brand, the notification count (`.bell-compact`, a
bell and the count, named "Notifications") and Menu; without scripts both lists are simply shown. Under the header, each site notice and
the coming changes are a bar (`.coming-changes`) with a Put away button once its note has been read (`releases/banners.py`). The open group is
marked with `.current` and the page inside it with `aria-current="page"`. A menu's heading is a link to the menu's own page (its pages,
each with a sentence on what it is for; `core/nav.py` `Menu.landing`, `Item.about`) and the arrow beside it is the disclosure, a button of
its own with an accessible name ("Filings pages"), as WAI-ARIA's disclosure navigation with top-level links has it. A long menu keeps its
dropdown short: items marked `in_menu=False` are on its page only, and the dropdown ends with a link to it (Administration).

`main.page` takes the full window width, less the gutter, as the header and footer do: no fixed maximum, so a wide table scrolls in its
`.hscroll` box only when it is wider than the window (1920 px gives it 1,872 px, not 1,152). Prose keeps a readable measure on its own:
paragraphs, hints and plain lists in `main.page` stop at 80 characters (`.guide`, `.measure` and page descriptions are narrower still),
while tables, charts, filter bars, tiles and card grids use the width. A form that should stay narrow says so (`.form-narrow`).
A long form uses the width too (New run, `.wide-form`): each fieldset's fields sit in a `.field-grid`, as many columns of about 20rem
as fit (one at 320 px), filled left to right in the order they are read, so the visual order is the DOM order. Fields that go together
share a `.field-cell` (a box and the option it turns on, a check and its year), and small sections share a row in a `.fieldset-row`
(the larger one marked `.grow`). Explanatory paragraphs inside keep the 80-character measure; tables in a section's disclosures use its width.
A long form keeps what most people need in its first section and folds the rest into one native disclosure (`.advanced-options`; New
run: The job's seven fields, then Advanced options). The disclosure opens itself when one of its fields has an error, and its summary
says how many of its fields are not at their defaults, so a setting is never folded away without a word.

## Components (by class)

| Class | What it is | Notes |
|---|---|---|
| `.head-row` + `h1` + `.actions` | page header: title, description, action buttons | one `.primary` button per page at most |
| `.subnav` | a module's sections as a tab strip | links, not ARIA tabs; current marked by aria and a bar; one row at 1280 px (a dashboard page with a long title has a `short` label for it) |
| `.filters` | a filter bar: label above each control, Apply at the end | `label` elements stack by default. One submit button per bar, a plain `button` named "Apply" (a search box on its own: "Search"), after any "Other filters" / "More filters" disclosure so the one button sends both |
| `nav[aria-label="Breadcrumb"] > ol.crumbs` | the page's parents, first thing in `content`, above the heading | on every page below a module index or a list; each parent is a link, the page itself is not listed (its `h1` follows); the › separators are drawn by CSS, so screen readers hear a list |
| `table.list` (in `.hscroll`) | a data table | `td.num` for numbers; `td[colspan]` alone in the body reads as the empty state |
| `.status` (+ `.ok` `.warn` `.bad` `.info`) | a status badge | the word is always present |
| `.card`, `dl.tiles`, `dl.facts` | an object, summary numbers, key/value facts | cards are for objects, not for a page's main table |
| `.messages`, `.alert`, `ul.notices` | page messages, an inline alert, a notice list | tinted by state |
| `button`, `.button`, `.primary`, `button.link`, `.danger` | the button set | 36 px high, 24 px minimum for inline ones |
| `a` in running text | a link inside a sentence | stays an inline link: WCAG 2.5.8 exempts targets in a sentence from the 24 px size, so a short link in prose is never turned into a button to pass a size probe |
| `.field`, `.helptext`, `.field-errors`, `fieldset` | form fields | `as_field_group` renders every field as one `.field` block (`core/templates/django/forms/field.html`) in the order label, control, errors, hint, so a field stays together when a filter bar wraps. No colon after a label or legend; a checkbox is one line with its box inside the label (`label.check`). A hand-written field follows the same shape: `<div class="field"><label for>…</label><input aria-describedby="…-help"><span class="helptext" id="…-help">…</span></div>`. Inside a `form.stack`, only a `<p>` holding a label or control stacks as a field; a prose `<p>` stays prose (links inline) |
| `.label-row`, `button.info-btn`, `.info-pop` | a field's hint behind an "i" button beside its label | a form with `help_as_info = True` (New run) renders each hint this way (`core/_info_button.html`, `core/_info_pop.html`). The button is named "About <label>"; the hint is a native popover that opens with a click, a tap, Enter or Space and closes with Escape or a click elsewhere, with no script. `app.js` places it under its button and opens it when a mouse rests on the button; a hint opened by hovering stays open while the pointer is on the button or the hint (1.4.13). The hint keeps its `<id>_helptext` id, so the control's `aria-describedby` still reads it |
| `td.due` with `<time>` and `<small>` | a due date with its urgency in words | `{{ due_at\|due_words }}`: "Due in 2 weeks", "Passed 7 months ago"; every filing index and the calendar use it |
| `{{ when\|date_words }}`, `{{ when\|datetime_words }}` | the one way a date is written | ISO, "2026-10-31", on every record (a version, a confirmation, a publication); the time with its zone, "2026-10-31 14:56 PDT", only where the minute matters: runs, uploads, jobs, logs and notifications. Never `\|date:"..."` in a template |
| `{{ user\|person }}`, `{{ user\|person_name }}` | a person | the name, then the email address in small muted text (`person`); the name alone, or the email when none is recorded, inside labels, headings and sr-only text (`person_name`). Never a bare `.email` |
| `{{ cycle\|cycle_label }}` | a filing cycle's years | "2024-2027", or "2021" for a one-year cycle (`str(cycle)` stays "2021-2021": it is a key) |
| `ui/dash.html`, `.muted` | a missing value | a missing number is a dash with "none" for screen readers; a missing status is muted text ("Not given"), never a badge |
| `ui/version_status.html` | an organization's status on a filing | the badge, then "version 2" in small text, a link when the version has a page (Monthly index and year) |
| `.status` + `{{ status\|status_tone }}` | a badge toned from a status word | every status column, row and fact is a badge (never plain words); a date beside it stays text. The `ui` filters are builtins: no `{% load %}` needed |
| `details.section` with `h2` in `summary` and `.section-body` | an earlier cycle or year, collapsed with a count | the newest stays an open `section.cycle` |
| `ui/empty.html` (`.empty`) | an empty state with its one action | `{% include "ui/empty.html" with title=... text=... action_url=... action=... %}` |
| `.id` | an identifier (PA code, PrgID, field name) in the mono face | |
| `core/tick_lists.py` `tick_list()`, `core/_tick_list.html` (`details.tick-list`) | a long list of checkboxes, folded | a closed disclosure whose summary names the list and what is ticked ("Sector 2 chosen: Commercial, Residential", "Any" or "None followed" when nothing is); open, the ticked values first, then the rest in the list's order, in columns of about 230 px; a list of more than ten gets a "Narrow the list" box (`core/tick_lists.js`: it keeps the values holding the words typed, never drops a ticked one, says how many are shown and announces it once typing pauses; without scripts it stays hidden). Plain checkboxes in the page's form, so it works without scripts. In a Django form, give a multiple-choice field `TickListInput(legend=...)` as its widget (the summary carries the name, so the field has no label of its own). Use it for any list of more than a handful of values instead of a wall of boxes |
| `core/pickers.py`: `ProgramField`, `FieldField`, `MemberField`; `core/_program_picker.html` (`fieldset.picker`) | pick, don't type: a program, a dictionary field or a member chosen from a list | a text box with the choices as a native `<datalist>` (value and name), so the list narrows as you type with no script; the server reads the value in any case or a name only one choice has, and text naming nothing gets one sentence with the near matches ("No program is “PGE2100”. Did you mean PGE21001 (Home Energy Reports)?"). Several programs for a filter (`program_picker`): the chosen ones as ticked boxes above the box that adds another, which `core/pickers.js` adds as soon as it is picked from the list (announced); in a Django form, `ProgramsField` draws the same and cleans to the PrgIDs. A list that depends on an earlier choice: `select[data-reveal]`, or a group of radio buttons marked `data-reveal` (Add a rule's kind), shows only the `[data-reveal-for]` fields of the chosen value (Link a record); without scripts all show, each saying which choice it is for (`.no-script-note`, hidden once scripts run). Never ask for a number from an address or a comma-separated list of IDs |
| `form.form-steps` with `.form-step` blocks, `h2` + `.step-n` | a long form in short parts | each part a block under its heading with a line between parts; for steps in order (File a document) the number sits in a circle and is part of the heading's text, so it is read as well as seen. All on one page, so nothing is lost between steps and the form works without scripts. A choice that changes the rest of the form (the collection) is a link at the top of step 1, not a step of its own |
| `.page-title` + `h1` + `{% page_help "<template>" %}` | every page's heading with its help button (?) | the guide and tour are in the app's `page_help.yaml` (`cedars/core/page_help.py`); the button opens a modal dialog with the guide and "Take a tour of this page", and is a link to `/help/pages/<key>/` without scripts |
| `#filing-steps` > `ol.steps` + `p.step-now` | a filing's step line: Upload, Check, Price, Confirm, Published | `submissions/_filing_steps.html`; done steps carry a check and "(done)", the current one `aria-current="step"` and "(now)" for screen readers; the sentence under it says the current step in words |
| `section.card.outcome` | a filing's outcome and its one next action | `submissions/_filing_outcome.html`, right under the step line; it holds the page's only `.primary` button, and everything that is not the next step (the upload once validated, the file's details, the statewide card, earlier confirmations) folds below in `details.section` |
| `table.findings-by-rule`, `ul.finding-records` | a filing's findings grouped by rule | `submissions/_filing_findings.html`: the rule ID links to the rule, a severity badge with its count, the message and how to fix it, then the records to correct, each a link to its correction page |
| `[data-watch-url]` + `core/watch.js` | a part of a page that follows work still running elsewhere | polls the URL for `{"ended", "text"}` every 5 seconds, announces a change through `window.cedarsAnnounce`, reloads when the work ends unless something has the focus (then shows its `[data-watch-reload]` link); without scripts the reload link shows |
| `.save-bar` | a long form's buttons, kept at the bottom of the window (a record's page) | sticky where the window is at least 480 px tall, and `html:has(.save-bar)` gets a bottom scroll-padding so a focused field is never under it (2.4.11); below that height it is the form's last row |
| `table.list.choice-grid` | one choice per row from the same few options (Notification settings: Off, Page, Page and email) | a radio button per cell, named by its row heading and column heading (`aria-labelledby` on both ids); a `select[data-event-group]` above it sets every row at once (`app.js` ticks the rows and announces it; without a script the server applies it on save); the columns narrow and their headings wrap on a phone |
| `form.find-field` + `core/find_field.js` | "Find a field" on a long form | a search box with the fields' names as a datalist; the script moves the focus to the field and announces it; without scripts the server sends the page back at `#f-<field>` |
| `input[list]` + `datalist` | a field whose value comes from a value list | the codes are suggestions and typing anything else is allowed (the checks say if it is not on the list); a list too long to offer links to its page |

### Tables (long lists)

Shared parts in `cedars/core/tables.py`, everything in the link (a sorted, filtered, paged list can be shared and works without scripts):

| Part | What it is | Notes |
|---|---|---|
| `tables.sorting(request.GET, columns, default)` + `ui/sort_th.html` | sortable column headings | `?sort=key` ascending, `?sort=-key` descending; a heading is a link in the link color with an arrow (filled on the sorted column, which carries `aria-sort`; a neutral one on the others); a heading's link keeps the filters and goes back to page 1. A queryset sorts with `sort.ordering(columns)` (blanks last, then a tie-breaker), a list with `sort.rows(rows, columns)`. The caption says the order (`sort.words`). Programs, Runs, Program change requests and the Impact profile library sort; Results keeps its own |
| `{% pager page query=... label=... where="above" %}` and `where="below"` | the pager, drawn above the table and again below it | First and Last once they are more than one page away; "Page 2 of 7 (51 to 100 of 304)"; each copy is a `nav` named "… pages, above the list" / "below the list". `previous="Newer" next="Older"` where that reads better; `anchor=` keeps the reader at a section |
| `tables.chips(query, items)` (`tables.form_chips(form)` for a filter form) + `ui/filter_chips.html` | the filters applied, one chip each | each chip has its own Remove link (keeps the other filters and the sort); "Clear all filters" once two or more apply |
| year links (`ul.toggle-chips` in a `nav.chip-group`) | one click to another year | the allocation page's Program year: the current year first, the others and Every year one click away; the chosen one has a check mark and `aria-current` |

A table wider than the window shows fewer columns first rather than scrolling: budget Compare opens on four figures, ticked to add
the rest; the allocation page shows one year at a time (no Year column then) and shades every other funding PA's three columns.

From the UI review of 2026-10-30, still planned: a `/ui/` gallery page that the axe suite visits.
