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
