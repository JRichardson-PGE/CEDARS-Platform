"""Browser checks of every page with axe-core (WCAG 2.2 AA). Playwright's sync API runs an event loop in the test thread, which Django's
async-safety check would refuse for the database fixtures; the pages themselves are served by live_server's own thread."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

AXE = Path(__file__).parent / "vendor" / "axe-core-4.10.2" / "axe.min.js"


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="session")
def axe_source():
    return AXE.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker, django_db_keepdb):
    """The checks carry no django_db mark and use no live_server (either would wrap each check in a transaction or flush the scene after
    it), so pytest-django sets up no test database for them; this sets up both, unless something else in the session already has."""
    from django.db import connections

    created = []
    with django_db_blocker.unblock():
        for alias in ("default", "vault"):
            conn = connections[alias]
            if conn.settings_dict["NAME"] != conn.creation._get_test_db_name():
                created.append((conn, conn.settings_dict["NAME"]))
                conn.creation.create_test_db(verbosity=0, autoclobber=True, keepdb=django_db_keepdb)
    yield
    with django_db_blocker.unblock():
        for conn, old_name in created if not django_db_keepdb else []:
            conn.creation.destroy_test_db(old_name, verbosity=0)
