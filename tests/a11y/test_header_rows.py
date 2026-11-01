"""The signed-in header across window widths (UAT 2, U02). A CPUC system administrator has every menu; with a three-digit unread count the
header must stay one row tall from 1280 px up, and so must an organization administrator's under a long code. Someone who belongs to several
organizations also has the organization switcher: their header is one row from 1440 px. Wherever the header does wrap (a narrower window) it
must scroll away rather than stick to the top at twice its height. Runs on its own small scene, not test_axe's."""

import pytest
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.notifications import service as notifications

from ..conftest import create_cpuc_admin, create_user

pytestmark = [pytest.mark.a11y, pytest.mark.django_db(transaction=True)]


def _cookie(user, url):
    s = SessionStore()
    s[SESSION_KEY] = str(user.pk)
    s[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    s[HASH_SESSION_KEY] = user.get_session_auth_hash()
    s["account_authentication_methods"] = [{"method": "password", "at": 0}, {"method": "mfa", "at": 0}]
    s["account_authentication_methods_user"] = str(user.pk)
    s.create()
    return {"name": settings.SESSION_COOKIE_NAME, "value": s.session_key, "url": url}


def _unread(user, n=120):
    for i in range(n):
        notifications.notify(user, f"Notice {i}", key=f"n-{i}")


@pytest.fixture
def people():
    """A CPUC system administrator, an organization administrator of a REN with a long code, and a system administrator who also belongs
    to that REN (so has the organization switcher); each with 120 unread notifications."""
    cpuc = create_cpuc_admin()
    ren = service.create_organisation(cpuc, code="SOCALREN", name="Southern California Regional Energy Network", kind="pa", pa_type="REN")
    ren_admin = create_user("ren-admin@example.org", "Ren Admin")
    service.grant(cpuc, ren_admin, ren, perms.ORG_ADMIN, reason="first admin")
    helper = create_user("helper@example.org", "Hel Per")
    service.grant(ren_admin, helper, ren, perms.ORG_ADMIN, reason="second admin")
    service.grant(helper, ren_admin, ren, "programs.read", reason="reads its programs")
    both = create_user("both@example.org", "Both Orgs")
    for code in (perms.ORG_ADMIN, perms.SYSTEM_ADMIN):
        service.grant(cpuc, both, service.cpuc(), code, reason="system administrator")
    service.grant(helper, both, ren, "programs.read", reason="reads its programs")
    for user in (cpuc, ren_admin, both):
        _unread(user)
    return {"cpuc": cpuc, "ren": ren_admin, "both": both}


def _header(browser, live_server, user, width, org=None):
    context = browser.new_context(viewport={"width": width, "height": 800})
    context.add_cookies([_cookie(user, live_server.url)])
    page = context.new_page()
    page.goto(live_server.url + "/data/overview/?by=program")
    if org and page.locator("#org-switch").count():
        page.select_option("#org-switch", org)
        page.locator(".org-switch button").click()
        page.wait_for_load_state()
    page.wait_for_timeout(100)  # the ResizeObserver's first report
    box = page.evaluate(
        """(() => { const q = s => document.querySelector(s); return {
        row: q('.top .who').getBoundingClientRect().top - q('.top .brand').getBoundingClientRect().top,
        top: q('.top').getBoundingClientRect().height,
        position: getComputedStyle(q('body > header')).position,
        wrapped: document.documentElement.classList.contains('header-wrapped') }; })()"""
    )
    context.close()
    return box


def _one_row(box, width):
    assert abs(box["row"]) < 20 and box["top"] <= 64, f"{width} px: header {box['top']:.0f} px tall, cluster {box['row']:.0f} px down"
    assert box["position"] == "sticky" and not box["wrapped"]


@pytest.mark.parametrize("width", [1280, 1366, 1440, 1536, 1920])
def test_a_cpuc_administrators_header_is_one_row_from_1280(browser, live_server, people, width):
    _one_row(_header(browser, live_server, people["cpuc"], width), width)


@pytest.mark.parametrize("width", [1280, 1366, 1440])
def test_a_long_organization_code_stays_one_row_from_1280(browser, live_server, people, width):
    _one_row(_header(browser, live_server, people["ren"], width), width)


@pytest.mark.parametrize("width", [1440, 1536, 1920])
def test_the_organization_switcher_fits_one_row_from_1440(browser, live_server, people, width):
    _one_row(_header(browser, live_server, people["both"], width, org="SOCALREN"), width)


@pytest.mark.parametrize(("who", "width"), [("cpuc", 1024), ("ren", 900), ("both", 1280)])
def test_a_wrapped_header_scrolls_away(browser, live_server, people, who, width):
    box = _header(browser, live_server, people[who], width, org="SOCALREN" if who == "both" else None)
    assert box["row"] > 20, "the header fits on one row here: pick a narrower width for this check"
    assert box["wrapped"] and box["position"] == "static"


def test_the_acting_for_badge_reads_in_order(browser, live_server, make_user):
    admin = create_cpuc_admin()
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.add_cookies([_cookie(admin, live_server.url)])
    page = context.new_page()
    page.goto(live_server.url + "/")
    text = page.locator(".org-badge").inner_text()
    context.close()
    assert text.split() == ["Acting", "for", "CPUC"]
