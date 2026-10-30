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

## Shared services

| Service | App | Phase | Status | Requirements |
| --- | --- | --- | --- | --- |
| Identity and access (organisations, memberships, grants, recertification) | `cedars.services.accounts` | 1 | Planned | BR-01 |
| Audit and change log | `cedars.services.audit` | 1 | Planned | BR-09.1, 09.3, BR-12.7 |
| Program registry (programs, effective-dated versions, allocation versions, filing cycles, periods) | `cedars.services.registry` | 1 | Planned | BR-13, BR-06.7 |
| Dictionary and templates | `cedars.services.dictionary` | 1 | Planned | BR-02.1, BR-08 |
| Validation rule library | `cedars.services.rules` | 1 | Planned | BR-03 |
| Submissions and workflow | `cedars.services.submissions` | 1 | Planned | BR-02.3-02.7, BR-05.1-05.2, BR-09.4-09.6 |
| Notifications | `cedars.services.notifications` | 1 | Planned | BR-06.2, BR-09.2, BR-12.1 |
| Reference data (pack registry) | `cedars.services.refdata` | 1 | Planned | BR-06.21, BR-11.7, BR-11.8 |
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
| Admin and configuration | `cedars.modules.admin_config` | 1-2 | Planned |

## Choices made in code

- Pages and the API call one service layer; the API (`/api/v1`, OpenAPI) has its own tests. This keeps the design's aim (the API is never a
  less-tested path) without pages making HTTP calls to their own server.
- The calculation job API, run records and S3 orchestration live in the Calculations module; ReCET supplies the worker and the job contract.
- Hosting targets: ECS Fargate (web), AWS Batch on Fargate via SQS (workers), RDS PostgreSQL (core and vault), S3, Cognito or the CPUC's
  OIDC provider. Infrastructure as code arrives with phase 1's last PR and is not applied until the CPUC account exists.
