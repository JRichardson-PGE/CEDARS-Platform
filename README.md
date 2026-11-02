# CEDARS Platform

The replacement for CEDARS (cedars.cpuc.ca.gov), the CPUC system California's program administrators use to file energy-efficiency
budgets, claims and monthly reports. One Django application (a modular monolith) on the CPUC's AWS account, with the ReCET engine as its
calculation service.

The design is the CEDARS Replacement Architecture (CEDARS-Research `deliverables/cedars_platform/architecture/`) and the requirements are
BRD v0.5 (CEDARS-Research `deliverables/`). `docs/ARCHITECTURE.md` maps that design onto this code and tracks what is built.

## Running it

With Docker:

    docker compose up --build
    docker compose exec web python manage.py migrate

Then open http://localhost:8000.

Without Docker (Python 3.12, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 with a `cedars` user and database):

    uv sync
    uv run python manage.py migrate
    uv run python manage.py runserver

Every account signs in with a second factor. For a demo on your own machine you can turn that off with the environment variable
`CEDARS_REQUIRE_MFA=0` (read by the dev settings only; every other environment always requires it). With Docker, set it in the shell
before `docker compose up`.

To try the site as a program administrator's filer, fill the demo organizations and add a test account (sandbox and developer databases
only; it reads, submits and confirms in every module of that PA, and can be re-run to reset the password):

    docker compose exec web python manage.py seed_sandbox
    docker compose exec web python manage.py demo_account filer@pge.example.org --org PGE --name "Test Filer" --password "<choose one>"

`--org` can be repeated, and `--pa-type REN` (or IOU, CCA) adds every active PA of that type, so one account can file for all of the
RENs. Add `--org-admin` to make it each PA's administrator as well.

## Calculations locally

Runs are priced by the ReCET worker (`recet worker run`), which the platform starts and never imports. With the engine installed
(`engine/README.md`), add a release on the Engine releases page (CPUC system administrator), start a run on New run, and price queued
runs with:

    uv run python manage.py calculations_worker          # --once to price one run; --lane small|large

`CALCULATIONS_WORKER_COMMAND` (default `recet worker`) names the worker; files go under `CEDARS_STORAGE_ROOT` (default `var/storage`).
In AWS, `CALCULATIONS_BACKEND=batch` submits runs to the Batch queues and `manage.py calculations_sync --loop` (an always-on service,
once a minute) records their progress and receipts.

Systems start runs through the job API (OpenAPI at `/api/v1/schema`) with a token made on the Account page:

    curl -H "Authorization: Bearer $CEDARS_TOKEN" -F job=@job.zip -F name="2028 claims" -F acc_version=2024 \
         https://<host>/api/v1/calculations/runs
    curl -H "Authorization: Bearer $CEDARS_TOKEN" https://<host>/api/v1/calculations/runs/<id>     # status, receipt, file URLs

## Checks

    uv run pytest                  # unit and page tests (PostgreSQL)
    uv run pytest -m a11y -n auto  # axe-core WCAG 2.2 AA checks in Chromium (uv run playwright install chromium)
    uv run ruff check . && uv run ruff format --check .

CI runs all of them, checks that migrations match the models, runs Django's deployment checks on the production settings and builds the image.
