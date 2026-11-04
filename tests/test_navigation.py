"""The header's menus by what the account may do (core/nav.py; navigation and help review D1, Jake 2026-11-04): signed out, signed in with
no organization, a member, an approver and a CPUC system administrator; the highlighted menu; the engine documentation's new address;
the statewide allocation factors open to everyone; the approver's column on the filing index pages."""

import json
import re

import pytest

from cedars.core import nav
from cedars.services.accounts import service as access
from cedars.services.dictionary import service as dictionary
from cedars.services.registry import service as registry
from cedars.services.submissions.models import ReopenRequest, Submission

from .conftest import create_user, page_only
from .test_budgets import filer, filing  # noqa: F401 (fixtures)
from .test_registry import _factors
from .test_search import CATALOG

pytestmark = pytest.mark.django_db


def header(html: str) -> str:
    return html.split('<nav aria-label="Main"', 1)[1].split("</nav>", 1)[0]


def menus(html: str) -> list[str]:
    """The header's top-level entries: plain links and menus, in order."""
    h = header(html)
    return re.findall(r'data-tour="nav-([a-z]+)"', h)


def menu(html: str, key: str) -> str:
    h = header(html)
    return h.split(f'data-tour="nav-{key}"', 1)[1].split("</details>", 1)[0]


def test_signed_out(client):
    html = client.get("/").content.decode()
    assert menus(html) == ["dashboard", "programs", "published"]
    published = menu(html, "published")
    for url in ("/data/records/", "/documents/", "/documents/studies/", "/dictionary/", "/allocations/", "/filings/calendar/"):
        assert f'href="{url}"' in published, url
    for url in ("/budgets/changes/", "/claims/changes/", "/monthly/changes/"):
        assert f'href="{url}"' in published, url
    assert 'href="/data/views/"' not in published  # saved views need an account
    assert 'data-tour="queue"' not in html


def test_signed_in_with_no_organization(signed_in, make_user):
    c = signed_in(make_user("consultant@example.org", "Con Sultant"))
    html = c.get("/").content.decode()
    assert menus(html) == ["dashboard", "programs", "calculations", "published", "reference"]
    reference = menu(html, "reference")
    assert (
        'href="/reference-data/cost-tests/"' in reference
        and 'href="/rules/"' not in reference
        and 'href="/reference-data/"' not in reference
    )
    assert 'href="/allocations/"' in menu(html, "published")  # with the public data, as signed out
    assert 'data-tour="queue"' not in html
    # the one step they need, on the front page and the Calculations index
    for page in ("/", "/calculations/"):
        assert 'href="/account/request-access/"' in page_only(c.get(page).content.decode()), page


def test_a_member(signed_in, pge_admin):
    html = signed_in(pge_admin).get("/").content.decode()
    assert menus(html) == ["dashboard", "programs", "filings", "calculations", "published", "reference", "admin"]
    filings = menu(html, "filings")
    for url in ("/budgets/", "/budgets/shifts/", "/claims/", "/monthly/", "/filings/calendar/", "/rules/check/", "/budgets/changes/"):
        assert f'href="{url}"' in filings, url
    assert 'href="/filings/reopen-requests/"' not in filings  # approvers only
    reference = menu(html, "reference")
    for url in ("/reference-data/", "/reference-data/cost-tests/", "/allocations/", "/rules/", "/dictionary/lists/", "/mandates/"):
        assert f'href="{url}"' in reference, url
    assert 'href="/allocations/"' not in menu(html, "published")  # once: under Reference data for members
    assert 'href="/reference-data/cost-tests/"' not in menu(html, "calculations")  # cost-test sets are listed once (Jake)
    admin = menu(html, "admin")
    assert "People and access" in admin and 'href="/admin/organizations/"' in admin and "Jobs and health" not in admin
    assert 'data-tour="queue"' in html and 'class="ask-access"' not in html


def test_a_cpuc_system_administrator(signed_in, cpuc_admin):
    html = signed_in(cpuc_admin).get("/").content.decode()
    admin = menu(html, "admin")
    groups = re.findall(r'class="nav-group-title"[^>]*>([^<]+)<', admin)
    assert groups == ["People and access", "Data and publishing", "Communications", "System"]
    for url in ("/releases/manage/", "/mandates/manage/", "/admin/health/", "/publication/holds/", "/admin/goals/"):
        assert f'href="{url}"' in admin, url
    assert 'href="/calculations/engines/"' in menu(html, "calculations") and "/calculations/engines/" not in admin  # once (Jake)


def test_an_approver_sees_what_waits(signed_in, cpuc_admin, cpuc, pge, filer, filing):  # noqa: F811
    approver = create_user("approver@cpuc.example.org", "Ap Prover")
    access.grant(cpuc_admin, approver, cpuc, "budgets.approve")
    sub = Submission.objects.create(deadline=filing, organisation=pge)
    ReopenRequest.objects.create(submission=sub, requested_by=filer, reason="a late correction")
    c = signed_in(approver)
    html = c.get("/budgets/").content.decode()
    assert 'href="/filings/reopen-requests/"' in menu(html, "filings")
    assert '<th scope="col">Waiting for a decision</th>' in html and ">1 correction request</a>" in page_only(html)
    who = html.split('<div class="who">', 1)[1]
    assert re.search(r'data-tour="queue"[^>]*>My queue <span class="count wait">1</span><span class="sr-only"> waiting</span>', who)
    # someone who decides nothing sees neither
    html = signed_in(filer).get("/budgets/").content.decode()
    assert "Waiting for a decision" not in html and 'href="/filings/reopen-requests/"' not in menu(html, "filings")


@pytest.mark.parametrize(
    "path,key",
    [
        ("/calculations/engine-docs/", "calculations"),
        ("/budgets/shifts/", "filings"),
        ("/claims/changes/", "filings"),
        ("/reference-data/cost-tests/", "reference"),
        ("/allocations/", "reference"),
        ("/dictionary/lists/", "reference"),
        ("/documents/", "published"),
        ("/documents/studies/", "published"),
        ("/data/records/", "published"),
        ("/data/overview/", "dashboard"),
    ],
)
def test_the_menu_holding_the_page_is_highlighted(signed_in, pge_admin, path, key):
    html = signed_in(pge_admin).get(path).content.decode()
    h = header(html)
    current = re.findall(r'data-tour="nav-([a-z]+)"(?: class="current"| aria-current="page")', h)
    assert current == [key], (path, current)
    assert h.count('aria-current="page"') == 1


def test_the_engine_documentation_moved_under_calculations(client):
    r = client.get("/help/recet/")
    assert r.status_code == 301 and r["Location"] == "/calculations/engine-docs/"
    r = client.get("/help/recet/output-fields/")
    assert r.status_code == 301 and r["Location"] == "/calculations/engine-docs/output-fields/"
    page = client.get("/calculations/engine-docs/").content.decode()
    assert "<h1>Engine documentation</h1>" in page and '<li><a href="/calculations/">Calculations</a></li>' in page


def test_the_allocation_factors_are_public(client, signed_in, cpuc_admin):
    confirmed = registry.confirm_allocation(
        cpuc_admin, registry.import_allocation_csv(cpuc_admin, _factors(), label="2028 TUAL", purpose="filing")
    )
    draft = registry.import_allocation_csv(cpuc_admin, _factors(), label="claims draft", purpose="claims")
    page = client.get("/allocations/").content.decode()
    assert f'href="/allocations/{confirmed.pk}/"' in page and f'href="/allocations/{draft.pk}/"' not in page
    assert "Load a factor file" not in page_only(page)
    detail = client.get(f"/allocations/{confirmed.pk}/").content.decode()
    assert "Every program-year" in detail or "Problems found" in detail
    assert cpuc_admin.email not in detail and (cpuc_admin.name or "@") not in page_only(detail)  # no names signed out
    assert client.get(f"/allocations/{confirmed.pk}/factors.csv").status_code == 200
    assert (
        client.get(f"/allocations/{draft.pk}/").status_code == 404 and client.get(f"/allocations/{draft.pk}/factors.csv").status_code == 404
    )
    c = signed_in(cpuc_admin)
    assert f'href="/allocations/{draft.pk}/"' in c.get("/allocations/").content.decode()  # drafts signed in, as before


def test_every_menu_item_opens_for_those_it_is_shown_to(signed_in, cpuc_admin, client):
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    for item in nav.items({"", "anon"}):
        r = client.get(nav.reverse(item.url_name), follow=True)
        assert r.status_code == 200, item.url_name
    c = signed_in(cpuc_admin)
    for item in nav.items(nav.roles_of(cpuc_admin)):
        r = c.get(nav.reverse(item.url_name), follow=True)
        assert r.status_code == 200, item.url_name
