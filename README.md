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

## Checks

    uv run pytest                # unit and page tests (PostgreSQL)
    uv run pytest -m a11y        # axe-core WCAG 2.2 AA checks in Chromium (uv run playwright install chromium)
    uv run ruff check . && uv run ruff format --check .

CI runs all of them, checks that migrations match the models, runs Django's deployment checks on the production settings and builds the image.
