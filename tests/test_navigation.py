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
        and 'href="/reference-data/packs/"' not in reference
    )
    assert 'href="/allocations/"' in menu(html, "published")  # with the public data, as signed out
    assert 'data-tour="queue"' not in html
    # the one step they need, on the front page and the Calculations index
    for page in ("/", "/calculations/", "/calculations/runs/"):
        assert 'href="/account/request-access/"' in page_only(c.get(page).content.decode()), page


def test_a_member(signed_in, pge_admin):
    html = signed_in(pge_admin).get("/").content.decode()
    assert menus(html) == ["dashboard", "programs", "filings", "calculations", "published", "reference", "admin"]
    filings = menu(html, "filings")
    for url in ("/budgets/", "/budgets/shifts/", "/claims/", "/monthly/", "/filings/calendar/", "/rules/check/", "/budgets/changes/"):
        assert f'href="{url}"' in filings, url
    assert 'href="/filings/reopen-requests/"' not in filings  # approvers only
    reference = menu(html, "reference")
    for url in ("/reference-data/packs/", "/reference-data/cost-tests/", "/allocations/", "/rules/", "/dictionary/lists/", "/mandates/"):
        assert f'href="{url}"' in reference, url
    assert 'href="/allocations/"' not in menu(html, "published")  # once: under Reference data for members
    assert 'href="/reference-data/cost-tests/"' not in menu(html, "calculations")  # cost-test sets are listed once (Jake)
    admin = menu(html, "admin")
    assert 'href="/admin/organizations/"' in admin and "Jobs and health" not in admin
    assert "People and access" not in admin  # a short dropdown, no group titles; the groups are on the landing page
    assert "Every administration page" not in admin  # an organization administrator's two pages are both in the dropdown
    assert 'data-tour="queue"' in html and 'class="ask-access"' not in html


def test_a_cpuc_system_administrator(signed_in, cpuc_admin):
    html = signed_in(cpuc_admin).get("/").content.decode()
    admin = menu(html, "admin")
    assert not re.findall(r'class="nav-group-title"', admin)  # Jake 2026-11-06: the dropdown is short, the landing page has every page
    listed = re.findall(r'<li><a href="([^"]+)"', admin)
    assert listed == [
        "/admin/organizations/",
        "/admin/stewardship/",
        "/admin/audit/",
        "/publication/holds/",
        "/admin/feedback/",
        "/admin/health/",
    ], listed
    assert '<li class="nav-more"><a href="/admin/">Every administration page</a></li>' in admin
    landing = page_only(signed_in(cpuc_admin).get("/admin/").content.decode())
    groups = re.findall(r'<h2 id="group-\d+">([^<]+)</h2>', landing)
    assert groups == ["People and access", "Data and publishing", "Communications", "System"]
    for url in (
        "/releases/manage/",
        "/mandates/manage/",
        "/admin/health/",
        "/publication/holds/",
        "/admin/goals/",
        "/admin/role-templates/",
    ):
        assert f'href="{url}"' in landing, url
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
    current = re.findall(r'(?:class="nav-menu current" data-tour="nav-([a-z]+)"|data-tour="nav-([a-z]+)" aria-current="(?:page|true)")', h)
    current = [a or b for a, b in current]
    assert current == [key], (path, current)
    assert len(re.findall(r'aria-current="(?:page|true)"', h)) == 1


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


# Navigation review D4 (Jake, 2026-11-04): the highlighted menu is set by the page, not its address; every page below a menu item has a
# breadcrumb back to it; a section with several sibling pages has a sub-navigation.


def _view_names() -> set[str]:
    from django.urls import get_resolver

    names = set()

    def walk(res, ns=None):
        for p in res.url_patterns:
            if hasattr(p, "url_patterns"):
                walk(p, ":".join(x for x in (ns, p.namespace) if x) if p.namespace else ns)
            elif p.name:
                names.add(f"{ns}:{p.name}" if ns else p.name)

    walk(get_resolver())
    return names


def test_every_page_belongs_to_a_menu_item_or_to_none():
    from fnmatch import fnmatchcase

    every = [i for m in nav.MENUS for i in m.items]
    lost = sorted(
        n
        for n in _view_names()
        if not nav.in_no_menu(n)
        and n not in nav.landing_names()
        and not any(fnmatchcase(n, p) for p in nav.SHARED)
        and not any(i.holds(n) for i in every)
    )
    assert not lost, f"give these pages an item in core/nav.py (Item.pages), or list them in NO_MENU or SHARED: {lost}"


def test_the_most_specific_item_holds_a_page():
    every = [(i,) for m in nav.MENUS for i in m.items]
    cases = {
        "budgets:filing": "budgets:index",
        "budgets:shift_new": "budgets:shifts",
        "budgets:operating": "budgets:shifts",
        "budgets:change_letter": "budgets:changes",
        "claims:change_letter": "claims:changes",
        "calculations:run": "calculations:runs",
        "calculations:draft_row": "calculations:drafts",
        "calculations:engine_release": "calculations:engines",
        "core:recet_doc": "core:recet_docs",
        "documents:aging": "documents:studies",
        "documents:document": "documents:library",
        "deer_resources:home": "documents:library",
        "dictionary:code": "dictionary:lists",
        "dictionary:field": "dictionary:index",
        "refdata:build_test": "refdata:cost_tests",
        "refdata:detail": "refdata:index",
        "mandates:detail": "mandates:index",
        "mandates:edit": "mandates:manage",
        "administration:allocation": "administration:allocations",
        "administration:organisation": "administration:organisations",
        "snapshots:hold": "snapshots:holds",
    }
    for page, item in cases.items():
        assert nav.holder(page, every)[0].url_name == item, page
    assert nav.holder("releases:index", every) is None and nav.holder("administration:account", every) is None


def test_a_filing_s_records_highlight_its_own_item():
    class Request:
        nav_item = ""
        path = "/filings/versions/1/records/"

        class resolver_match:  # noqa: N801
            view_name = "submissions:records"

    r = Request()
    nav.belongs_to(r, "claims:index")
    shown = nav.header(r, {"", "user", "member"})
    current = [(s.key, s.current_url) for s in shown if s.current]
    assert current == [("filings", "/claims/")]
    assert not any(s.current_exact for s in shown)  # a page below the item: aria-current="true", not "page"


def test_a_page_below_an_item_is_marked_true_and_its_own_page_page(signed_in, pge_admin):
    c = signed_in(pge_admin)
    h = header(c.get("/budgets/shifts/").content.decode())
    assert '<a href="/budgets/shifts/" aria-current="page">' in h
    h = header(c.get("/dictionary/lists/").content.decode())
    assert '<a href="/dictionary/lists/" aria-current="page">' in h
    html = c.get("/documents/lists/").content.decode()
    assert '<a href="/documents/" aria-current="true">' in header(html)


TOP_PAGES = {
    # A menu item's own page, or a page in no menu (core/nav.py NO_MENU): no breadcrumb.
    "accessibility/files", "administration/account", "administration/allocations", "administration/audit", "administration/goal_sets",
    "administration/health", "administration/organisations", "administration/role_templates", "administration/settings",
    "administration/stewardship", "budgets/index", "calculations/runs", "claims/index", "data/dashboard", "data/front", "data/records",
    "data/views", "dictionary/index", "feedback/form", "feedback/inbox", "monthly/index", "notifications/index", "programs/catalog",
    "refdata/index", "releases/index", "releases/limits", "releases/mandates", "retention/index", "rules/index", "snapshots/holds",
    "submissions/calendar", "submissions/reopen_requests", "core/about", "core/help/index", "core/queue", "core/search",
    # the error pages (core/errors.py): in no menu, with their own ways on
    "500", "core/error", "core/form_expired",
    # a menu's own page (its heading's link): the top of its section
    "core/section_administration", "core/section_calculations", "core/section_filings", "core/section_published", "core/section_reference",
}  # fmt: skip
CRUMBS = ('aria-label="Breadcrumb"', "_record_crumbs", "documents/_subnav.html", "impact_profiles/_nav.html", "refdata/builder/base.html")


def test_every_page_below_a_menu_item_has_a_breadcrumb():
    from pathlib import Path

    from cedars.core import page_help

    missing = []
    for p in sorted(Path(page_help.ROOT).rglob("templates/**/*.html")):
        key = str(p).split("/templates/", 1)[1].removesuffix(".html")
        if key.startswith(("account/", "mfa/", "allauth/")):  # sign-in pages: in no menu
            continue
        text = p.read_text(encoding="utf-8")
        if "<h1" in text and key not in TOP_PAGES and not any(c in text for c in CRUMBS):
            missing.append(key)
    assert not missing, f"add a breadcrumb back to the page's menu item: {missing}"


@pytest.mark.parametrize(
    "path,crumb",
    [
        ("/impact-profiles/packages/", "/impact-profiles/"),
        ("/documents/studies/aging/", "/documents/studies/"),
        ("/documents/lists/", "/documents/"),
        ("/data/downloads/", "/data/records/"),
    ],
)
def test_section_pages_keep_their_breadcrumb(signed_in, cpuc_admin, path, crumb):
    html = signed_in(cpuc_admin).get(path).content.decode()
    crumbs = html.split('<nav aria-label="Breadcrumb">', 1)[1].split("</nav>", 1)[0]
    assert f'href="{crumb}"' in crumbs


def test_reference_data_has_a_sub_navigation(signed_in, pge_admin, client):
    c = signed_in(pge_admin)
    for path in ("/reference-data/packs/", "/rules/", "/dictionary/lists/", "/allocations/", "/mandates/"):
        html = c.get(path).content.decode()
        sub = html.split('<nav class="subnav" aria-label="Reference data">', 1)[1].split("</nav>", 1)[0]
        assert sub.count("<a ") == 6 and f'href="{path}" aria-current="page"' in sub, path
    client.logout()
    assert 'aria-label="Reference data"' not in client.get("/allocations/").content.decode()  # signed out: no Reference data menu
