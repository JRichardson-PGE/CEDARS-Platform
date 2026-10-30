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
| `scripts/` | One-off tools (the catalog export) |

## Shared services

| Service | App | Phase | Status | Requirements |
| --- | --- | --- | --- | --- |
| Identity and access (organisations, memberships, grants, recertification) | `cedars.services.accounts` | 1 | Built: rules (`service.py`), sign-in with MFA for everyone and OIDC federation (django-allauth), session assurance, recertification job; API tokens come with the API (Calculations work) | BR-01, D6, F, G |
| Audit and change log | `cedars.services.audit` | 1 | Built: append-only in the database (migration 0002); log page in Administration | BR-09.1, 09.3, BR-12.7 |
| Program registry (programs, effective-dated versions, allocation versions, filing cycles, periods) | `cedars.services.registry` | 1 | Built: programs with effective-dated versions (no overlaps, enforced in PostgreSQL), filing cycles, reporting periods, statewide allocation versions (import, check, confirm, ReCET CSV export, most recent for a year); `import_programs`, `import_allocation` commands. Program pages come with the Programs module | BR-13, BR-06.7, BR-13.8 |
| Dictionary and templates | `cedars.services.dictionary` | 1 | Built: catalog versions loaded from ReCET's export (`scripts/export_recet_dictionary.py`, `data/dictionary/`), aliases (CET names) and header mapping, CSV / Excel templates and JSON schemas per module table, public dictionary pages and downloads. Claims, budget and monthly tables join the catalog with their modules | BR-02.1, BR-06.9, BR-06.10, BR-08 |
| Validation rule library | `cedars.services.rules` | 1 | Built: rule sets as data (CEDARS' claims and budget-filing rules imported from the pinned engine, 215 / 221 rules), published versions never change, the version in force by date, suggested fixes, set comparison, the engine's runner (`recet.filing_rules.check_frame`) over the stored rules, findings in one format, the pre-upload checker page and `check_file` command | BR-03.1-03.4, 03.6, BR-12.6 |
| Submissions and workflow | `cedars.services.submissions` | 1 | Built: one state machine for every module's filings (draft, validated, confirmed, published; a version is never edited once confirmed), the filing calendar (deadlines per period or cycle, moving one is audited), confirm only on a finished run over the current rows (`record_calculation` + the rows' hash), publication through hooks modules register (`register_publisher`), un-confirm before the due date on the same version, after it a reopen as version n+1 under the module's policy (D1: approval, open, closed) decided by an approver or their delegate; calendar and reopen-request pages | BR-02.3-02.7, BR-05.1-05.2, BR-09.4-09.6 |
| Notifications | `cedars.services.notifications` | 1 | Built: subscriptions per event type (optionally per module or organisation) fed from the audit trail after commit, so a rolled-back change notifies nobody; filing events only to the organisation's members and CPUC staff; every notification on the user's page, email per subscription through Django's mail backend (SES SMTP in production) with failures kept on the notification; `notify(user, ...)` for modules (run finished or failed, honouring the user's preference); deadline reminders 7 days and 1 day before (`send_deadline_reminders`, once each); notifications page, settings page and an unread count in the navigation | BR-06.2, BR-09.2, BR-12.1 |
| Reference data (pack registry) | `cedars.services.refdata` | 1 | Built: pack versions by kind (ACC electric / gas, impact profiles, eTRM, DEER, value lists, run parameters, engine releases) with a SHA-256 manifest, a fingerprint (the manifest's SHA-256) and a storage URI; registered as drafts (analysis runs only, BR-11.8), published once the tie-outs their kind requires pass (`TieOutRequirement`, data; a difference over the stated precision fails, BR-06.21); published packs never change and are withdrawn, never deleted (database trigger); `in_force`, `usable(pack, purpose)`, `pin()` for runs and submissions; `register_pack` / `record_tie_out` commands; pages to view, publish, withdraw | BR-06.21, BR-11.7, BR-11.8 |
| Retention (decision M) | `cedars.services.retention` | 1 | Built: a period per record class (the decision M defaults seeded, each changed on the Retention page with a reason), legal holds on one record or a whole class, the daily `purge_expired` job (dry run, one audit event per class, a purge log); notifications and idle unconfirmed filing drafts register their deletion step, Calculations adds scratch runs | BR-09.1, BR-10 (records) |
| Publication snapshots | `cedars.services.snapshots` | 3 | Planned | BR-07, BR-08.5 |

## Modules

| Module | App | Phase | Status |
| --- | --- | --- | --- |
| Programs | `cedars.modules.programs` | 2 | Planned |
| Calculations (ReCET) | `cedars.modules.calculations` | 2 | Planned |
| Impact profiles | `cedars.modules.impact_profiles` | 2 | Planned |
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

- Pages and the API call one service layer; the API (`/api/v1`, OpenAPI) has its own tests. This keeps the design's aim (the API is never a
  less-tested path) without pages making HTTP calls to their own server.
- The calculation job API, run records and S3 orchestration live in the Calculations module; ReCET supplies the worker and the job contract.
- Hosting targets: ECS Fargate (web), AWS Batch on Fargate via SQS (workers), RDS PostgreSQL (core and vault), S3, Cognito or the CPUC's
  OIDC provider. Infrastructure as code arrives with phase 1's last PR and is not applied until the CPUC account exists.
