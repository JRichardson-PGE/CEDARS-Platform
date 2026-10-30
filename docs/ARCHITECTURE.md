# Architecture in code

The design is the CEDARS Replacement Architecture (version 33, 2026-10-30) in CEDARS-Research. This file says where each part lives here and
how far it is built. Update the Status column in the PR that changes it.

## Layout

| Path | What |
| --- | --- |
| `cedars/settings/` | `base` (shared), `dev`, `test`, `sandbox`, `prod`; everything machine-specific from environment variables |
| `cedars/core/` | Base templates, the page-wide announcer, security headers, health check, home page |
| `cedars/services/` | One app per shared service |
| `cedars/modules/` | One app per module |
| `tests/` | pytest-django; `tests/a11y/` drives Chromium with axe-core 4.10.2 |
| `engine/recet-commit` | The pinned ReCET engine commit (see `engine/README.md`) |
| `data/dictionary/` | Field catalogs exported from the pinned engine |
| `infra/terraform/` | The AWS stack per environment (sandbox, staging, production): written and validated in CI, applied only by the CPUC's cloud team (`infra/terraform/README.md`) |
| `scripts/` | One-off tools (the catalog export) |

## Shared services

| Service | App | Phase | Status | Requirements |
| --- | --- | --- | --- | --- |
| Identity and access (organisations, memberships, grants, recertification) | `cedars.services.accounts` | 1 | Built: rules (`service.py`), sign-in with MFA for everyone and OIDC federation (django-allauth), session assurance, recertification job, API tokens (`tokens.py`, `api_auth.TokenAuthentication`) | BR-01, D6, F, G |
| Audit and change log | `cedars.services.audit` | 1 | Built: append-only in the database (migration 0002); log page in Administration | BR-09.1, 09.3, BR-12.7 |
| Program registry (programs, effective-dated versions, allocation versions, filing cycles, periods) | `cedars.services.registry` | 1 | Built: programs with effective-dated versions (no overlaps, enforced in PostgreSQL), filing cycles, reporting periods, statewide allocation versions (import, check, confirm, ReCET CSV export, most recent for a year); `import_programs`, `import_allocation` commands; `catalog()` reads every program in two queries | BR-13, BR-06.7, BR-13.8 |
| Dictionary and templates | `cedars.services.dictionary` | 1 | Built: catalog versions loaded from ReCET's export (`scripts/export_recet_dictionary.py`, `data/dictionary/`), aliases (CET names) and header mapping, CSV / Excel templates and JSON schemas per module table, public dictionary pages and downloads. Claims, budget and monthly tables join the catalog with their modules | BR-02.1, BR-06.9, BR-06.10, BR-08 |
| Validation rule library | `cedars.services.rules` | 1 | Built: rule sets as data (CEDARS' claims and budget-filing rules imported from the pinned engine, 215 / 221 rules), published versions never change, the version in force by date, suggested fixes, set comparison, the engine's runner (`recet.filing_rules.check_frame`) over the stored rules, findings in one format, the pre-upload checker page and `check_file` command | BR-03.1-03.4, 03.6, BR-12.6 |
| Submissions and workflow | `cedars.services.submissions` | 1 | Built: one state machine for every module's filings (draft, validated, confirmed, published; a version is never edited once confirmed), the filing calendar (deadlines per period or cycle, moving one is audited), confirm only on a finished run over the current rows (`record_calculation` + the rows' hash), publication through hooks modules register (`register_publisher`), un-confirm before the due date on the same version, after it a reopen as version n+1 under the module's policy (D1: approval, open, closed) decided by an approver or their delegate; calendar and reopen-request pages | BR-02.3-02.7, BR-05.1-05.2, BR-09.4-09.6 |
| Notifications | `cedars.services.notifications` | 1 | Built: subscriptions per event type (optionally per module or organisation) fed from the audit trail after commit, so a rolled-back change notifies nobody; filing events only to the organisation's members and CPUC staff; every notification on the user's page, email per subscription through Django's mail backend (SES SMTP in production) with failures kept on the notification; `notify(user, ...)` for modules (run finished or failed, honouring the user's preference); deadline reminders 7 days and 1 day before (`send_deadline_reminders`, once each); notifications page, settings page and an unread count in the navigation | BR-06.2, BR-09.2, BR-12.1 |
| Reference data (pack registry) | `cedars.services.refdata` | 1 | Built: pack versions by kind (ACC electric / gas, impact profiles, eTRM, DEER, value lists, run parameters, engine releases) with a SHA-256 manifest, a fingerprint (the manifest's SHA-256) and a storage URI; registered as drafts (analysis runs only, BR-11.8), published once the tie-outs their kind requires pass (`TieOutRequirement`, data; a difference over the stated precision fails, BR-06.21); published packs never change and are withdrawn, never deleted (database trigger); `in_force`, `usable(pack, purpose)`, `pin()` for runs and submissions; `register_pack` / `record_tie_out` commands; pages to view, publish, withdraw. A ReCET run is pinned to its ACC year's electric pack and the impact-profile library in force (`calculations.service.packs_for`: published, or for analysis runs a draft when none is; a filing refuses a draft): the files under the engine's reference-folder paths (`acc/<year>/`, `impact_profiles/`) become the job spec's `packs.files`, so the worker refuses a folder whose files differ, and `pin()` goes into the spec's context, the receipt and the input hash | BR-06.21, BR-11.7, BR-11.8 |
| Retention (decision M) | `cedars.services.retention` | 1 | Built: a period per record class (the decision M defaults seeded, each changed on the Retention page with a reason), legal holds on one record or a whole class, the daily `purge_expired` job (dry run, one audit event per class, a purge log); notifications and idle unconfirmed filing drafts register their deletion step, Calculations adds scratch runs | BR-09.1, BR-10 (records) |
| Publication snapshots | `cedars.services.snapshots` | 3 | Planned | BR-07, BR-08.5 |

## Modules

| Module | App | Phase | Status |
| --- | --- | --- | --- |
| Programs | `cedars.modules.programs` | 2 | Built: catalog (search by name or PrgID, PA, open or closed, segment, program year; CSV download of the filtered list), program page (definition in force, versions with field-level changes, statewide allocation history, slots for budgets, claims and reports), effective-dated change form for the program's administrators and the CPUC. Change requests through the workflow come with the submissions service |
| Calculations (ReCET) | `cedars.modules.calculations` | 2 | Built: engine releases as data (CPUC page: release, worker image, Batch job definition, calculations and their ACCs, default, retire), run records (organisation or own, settings, inputs by key and SHA-256, input hash, lane, job spec, progress, receipt), the job spec for ReCET's `recet worker run` (`jobspec.py`), local and AWS Batch dispatch (`dispatch.py`, `calculations_worker`, `calculations_sync`), statewide allocation versions pinned per run, queue position and estimates, New run / runs / run pages with downloads, owner notices, scratch-run retention, a finished filing run priced into its submission version. Job API, Results, Compare, templates, Edit inputs and parallel runs follow |
| Impact profiles | `cedars.modules.impact_profiles` | 2 | Built: nightly eTRM sync into a versioned snapshot with sync history and exclusions (against a fixture schema until we have database access); load-shape upload with the four validation stages as rules LS-F/T/S/E; generation with CEDARS' formula and ID formula, 2nd-baseline profiles, NR/NC mapping rows; custom profiles (upload, replace, retire); DEER/TOU/legacy import; the library written in the engine's format, numbered and registered as a reference-data pack; per-PA job queues |
| Budgets (filings, fund shifts) | `cedars.modules.budgets` | 3 | Planned |
| Claims | `cedars.modules.claims` | 4 | Planned |
| Monthly reports | `cedars.modules.monthly` | 5 | Planned |
| Data, dashboards, downloads | `cedars.modules.data` | 6 | Planned |
| Documents and DEER resources | `cedars.modules.documents` | 6 | Planned |
| Admin and configuration | `cedars.modules.administration` | 1-2 | Access pages built: account, organisations, members and grants, access requests, recertification, stewardship, access settings, audit log, statewide allocation versions |

## Choices made in code

- Rules are stored as rows but run by ReCET's own rule runner (`recet.filing_rules.check_frame`, given a RuleSet built from the rows), so
  the platform, ReCET runs and the checker can never disagree. A rule's test (`definition`) is never edited; new CEDARS rule files are a
  new rule set version. The engine is needed to import and run rules; the pages work without it.

- Sign-in is django-allauth: email and password with a second factor (TOTP, WebAuthn security keys, recovery codes) required of every
  account (`RequireMFAMiddleware`), and each federating organisation's directory as an OpenID Connect app
  (`SOCIALACCOUNT_PROVIDERS["openid_connect"]["APPS"]`). Cognito or the CPUC's provider plugs in the same way. A session's assurance
  (password, mfa, federated) comes from allauth's sign-in records and is stamped on every audit event.
- Run `manage.py bootstrap_cpuc EMAIL --steward` once per environment, and `manage.py close_recertifications` daily.
- Impact profiles: `manage.py sync_etrm` nightly and `manage.py run_impact_profile_jobs --loop` as a worker (upload checks, generation, syncs
  and library writes, one at a time per PA queue). `import_profile_library FOLDER` loads legacy CEDARS' library (the engine's
  catalog.parquet and hourly.parquet). Every 8,760 is one float64 array in default storage under its SHA-256, stored once. The profile
  catalog lives in the module; each library write registers as a draft `impact_profiles` pack in the reference-data service.
  The eTRM database layout is `cedars/modules/impact_profiles/etrm_fixture_schema.sql`: only `etrm.PACKAGES_SQL` and `PERMUTATIONS_SQL`
  change when the real schema is known.

- The API is Django REST Framework under `/api/v1` (JSON only), described by drf-spectacular at `/api/v1/schema`. A call authenticates
  with an API token (`Authorization: Bearer cedars_...`; only its SHA-256 is stored) or the session. A token acts for one organisation and
  reaches only its scopes (modules), never more than its user's grants there, and never PII or admin permissions; API views check
  `cedars.core.api.allowed(request, "<module>.<action>")` or use `ModulePermission.of(...)`. `CEDARS_API_TOKEN_MAX_DAYS` (365) caps a
  token's life.
- Pages and the API call one service layer; the API (`/api/v1`, OpenAPI) has its own tests. This keeps the design's aim (the API is never a
  less-tested path) without pages making HTTP calls to their own server.
- Edit inputs (`calculations/drafts.py`, pages under `/calculations/drafts/`) keeps a small job's tables (up to the engine's 500
  measures) as text in Postgres and checks them cell by cell with the pinned engine's `recet.editor` (Jake, 2026-10-30: the web app
  installs ReCET); impact profiles are not matched there (no profile library on the web servers), the run checks them. Pages are
  server-rendered forms, one row at a time; a draft downloads as a job zip, or starts from a run's inputs (Edit inputs and rerun).
  Drafts are scratch data, purged with scratch runs from their last change.
- The calculation job API, run records and S3 orchestration live in the Calculations module; ReCET supplies the worker and the job contract.
  The platform never imports the engine: it writes a job spec (spec version 1) to the results bucket with every input pinned by SHA-256,
  and the worker (`recet worker run <spec>`, locally through `CALCULATIONS_WORKER_COMMAND` or on Batch through the job definition's
  `Ref::job_spec`) writes outputs, `progress.json` and `receipt.json` beside it. Runs of up to `CALCULATIONS_SMALL_RUN_RECORDS` (10,000)
  measures take the small-runs queue; `CALCULATIONS_MAX_RECORDS` (2,000,000) is refused before queueing. A run that allocates statewide
  programs carries its allocation version's whole factor table as an input, and the spec names its SHA-256. The job API
  (`calculations/api.py`, `/api/v1/calculations`: engines, start, list, follow, stop, download) takes New run's fields as multipart form data and answers with the same run record the run page shows; a call
  sees its organisation's runs (calculations.read or .submit) and the caller's own. A call names an output template by id (`templates`
  lists those it sees), `default` for the caller's default, or gives `output_columns`; one that names none writes every column.
- Parallel runs (the engine beside CET, phase 2): New run and the job API take a CET run's outputs CSV for the same job
  (`cet_outputs`, ReCET's own calculation only); it is an input of the run, so the worker compares each measure on CET_ID and writes
  `cet_comparison_measures_*` and `cet_comparison_summary_*`, and the run page shows the summary's totals and ratios.
- Object storage is `cedars.core.storage`: four areas (uploads, results, refdata, snapshots), one bucket each in AWS, folders under
  `CEDARS_STORAGE_ROOT` locally. Downloads from S3 are pre-signed URLs; nothing large passes through a page.
- A run's Results and Compare pages (`calculations/results.py`) call the pinned engine's `recet.results_view` unchanged (Jake, 2026-10-30:
  the web app installs ReCET) over the group sums the worker keeps in `out/_results/`, copied once into `CALCULATIONS_RESULTS_CACHE`; the
  measure level reads the kept measure-level parquet for runs of up to 50,000 measures. Without the engine the pages say so.
- Hosting: ECS Fargate (web, behind an HTTPS load balancer with AWS WAF; daily jobs on EventBridge Scheduler), AWS Batch on Fargate
  (workers; a large-run queue and a small-run queue with its own capacity, submitted to directly by the Calculations module), RDS
  PostgreSQL 16 (core, and the vault as its own instance and KMS key; workers reach neither), S3 (uploads, results, data packs,
  snapshots), the CPUC's OIDC provider or the platform's own MFA sign-in. `infra/terraform/` describes it; nothing is applied until
  the CPUC's cloud team reviews it in their account.
- Output templates (BR-06.10; `calculations/output_templates.py`, page `/calculations/templates/`) name fields of the published field
  catalog (Measure inputs and outputs; CET and older names saved as the canonical name). Templates with no owner are offered to everyone
  and kept by CPUC system administrators (ReCET's two shipped ones added by migration); the rest are their owner's. A run carries the
  chosen template's columns in its settings (`output_template`, `output_template_name`), so a later change never alters what it asked for.
- The sandbox is the production stack with `cedars.settings.sandbox`: no PII vault, a banner on every page, [SANDBOX] in every email
  subject, test filings deleted after the sandbox_data period, `seed_sandbox` to fill it.
