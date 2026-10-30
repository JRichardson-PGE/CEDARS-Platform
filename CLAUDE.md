# CEDARS Platform

The CEDARS replacement: one Django project (a modular monolith) that the CPUC owns and hosts on its AWS account. Design: the CEDARS
Replacement Architecture (CEDARS-Research `deliverables/cedars_platform/architecture/CEDARS_Replacement_Architecture.html`, settled decisions
A-M, 2026-10-29). Requirements: BRD v0.5 (CEDARS-Research `deliverables/`). The calculation engine is ReCET (`JRichardson-PGE/ReCET`), imported
unchanged as a package; never copy engine code here. `docs/ARCHITECTURE.md` maps the design onto this code and says what is built.

## Rules

- **Services and modules.** `cedars/services/<name>` holds the shared services (registry, submissions, rules, dictionary, audit,
  notifications, reference data, snapshots); `cedars/modules/<name>` the modules (Programs, Calculations, Impact profiles, Budgets, ...). A module
  changes data only through a service's functions (it may read a service's models to show them) and never imports another module;
  a service never imports a module.
- **Versions, not edits.** A confirmed submission is never updated in place; a correction is a new version with a reason. The audit trail is
  append-only (no UPDATE or DELETE on its table).
- **Every change audited.** A service that changes a record writes an audit event (who, active organisation, what, why, before, after).
- **Configure rather than recode.** Rule sets, periods, value lists, templates, retention periods and policy choices (BRD D1-D14) are data with
  effective dates and an admin page, not code paths.
- **PII stays in the vault.** Only `PII_Contact` and `PII_Site`, in their own database; everything else holds SiteID / ContactID keys only.
- **Names.** ReCET's renamed field dictionary is canonical (decision L); CET names are accepted on upload through the crosswalk. Name ACCs by year
  ("2024 ACC"). The BRD never names the development vendor; neither do pages, docs or commits here.
- **WCAG 2.2 AA on every page** (decision K; a California state agency requirement). Pages render on the server and work without scripts.
  Every page change keeps: a visible label or accessible name on every control; keyboard reach and a visible focus for everything a mouse can do;
  status in text, not colour alone; 4.5:1 text and 3:1 control-border/focus contrast in both colour schemes; announcements through
  `window.cedarsAnnounce` (one page-wide live region), never a new live region; never disable the focused control (use aria-disabled); targets
  at least 24 x 24 px; reflow at 320 px and 200% zoom. Add every new page to `tests/a11y/pages.py`; CI runs axe on it in both schemes.
  ReCET's web templates, CSS and NVDA fixes are the reference (`recet/web`, `tests/screenreader`).
- **Same-origin scripts only.** The CSP allows `'self'`; vendor a library with its integrity checked, never load it from a CDN.
- **PostgreSQL only** (tests too): the design relies on its triggers and ranges.
- **Secrets from the environment**, never in files. Settings per environment in `cedars/settings/`.

## Working here

- `uv sync`, then `uv run pytest` (needs PostgreSQL; `DATABASE_URL`, default `postgres://cedars:cedars@localhost:5432/cedars`).
- `uv run pytest -m a11y` runs axe in Chromium (`uv run playwright install chromium`, or `PLAYWRIGHT_CHROMIUM_EXECUTABLE`).
- `uv run ruff check . && uv run ruff format --check .`
- Each PR is small and off `main`; never stack PRs. Migrations are part of the PR that changes the models (CI checks).
