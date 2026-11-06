"""Each header menu's own page (Jake, 2026-11-06): the heading links to it, it lists the menu's pages the visitor may open with a sentence
on each, the Administration dropdown is short with the full list on its page, and Filings shows where each PA's budgets, claims and
monthly reports stand (submissions/status.py)."""

import re
from datetime import timedelta

import pytest
from django.utils import timezone
from django.utils.html import escape

from cedars.core import nav
from cedars.services.accounts import service as access
from cedars.services.registry import service as registry
from cedars.services.submissions import service as submissions
from cedars.services.submissions import status
from cedars.services.submissions.models import Submission, SubmissionVersion

from .conftest import page_only

pytestmark = pytest.mark.django_db

PAGES = {
    "filings": "/filings/",
    "calculations": "/calculations/",
    "published": "/published/",
    "reference": "/reference-data/",
    "admin": "/admin/",
}


def main(html: str) -> str:
    return page_only(html).split("<main", 1)[1].split("</main>", 1)[0]


def header(html: str) -> str:
    return html.split('<nav aria-label="Main"', 1)[1].split("</nav>", 1)[0]


def test_every_menu_heading_links_to_its_own_page(signed_in, cpuc_admin):
    html = signed_in(cpuc_admin).get("/").content.decode()
    h = header(html)
    for key, url in PAGES.items():
        menu = h.split(f'data-tour="nav-{key}"', 1)[1].split("</details>", 1)[0]
        assert f'<a class="nav-head" href="{url}">' in menu, key
        assert re.search(r"<summary><span class=\"sr-only\">[^<]+ pages</span></summary>", menu), key  # the arrow has a name


@pytest.mark.parametrize("key", list(PAGES))
def test_each_page_lists_its_menu_s_pages_with_what_each_is_for(signed_in, cpuc_admin, key):
    c = signed_in(cpuc_admin)
    page = c.get(PAGES[key])
    assert page.status_code == 200
    html = page_only(page.content.decode())
    menu = next(m for m in nav.header(page.wsgi_request) if m.key == key)
    for _, items in menu.groups:
        for item, url in items:
            assert f'<li><a href="{url}">{escape(item.label)}</a><p>{escape(item.about)}</p></li>' in html, item.label
            assert item.about, f"{item.label} needs a sentence on what it is for"
    h = header(page.content.decode())
    assert f'<a class="nav-head" href="{PAGES[key]}" aria-current="page">' in h  # the open page: its menu highlighted
    assert len(re.findall(r'aria-current="(?:page|true)"', h)) == 1


def test_a_page_lists_only_what_the_visitor_may_open(client, signed_in, pge_admin):
    html = page_only(client.get("/published/").content.decode())  # signed out: the public pages
    assert 'href="/allocations/"' in html and 'href="/filings/calendar/"' in html and 'href="/data/views/"' not in html
    assert client.get("/filings/")["Location"].startswith("/accounts/login/")  # nothing under Filings for them: sign in
    assert client.get("/admin/")["Location"].startswith("/accounts/login/")
    c = signed_in(pge_admin)
    html = main(c.get("/admin/").content.decode())  # an organization administrator: their two pages
    assert re.findall(r'<li><a href="([^"]+)">', html) == ["/admin/organizations/", "/admin/audit/"]
    assert "Jobs and health" not in html


def test_someone_with_no_administration_page_is_not_shown_its_page(signed_in, make_user):
    c = signed_in(make_user("consultant@example.org", "Con Sultant"))
    assert c.get("/admin/").status_code == 404 and c.get("/filings/").status_code == 404
    assert c.get("/calculations/").status_code == 200 and c.get("/reference-data/").status_code == 200


def test_the_moved_pages(signed_in, pge_admin):
    c = signed_in(pge_admin)
    assert "<h1>Runs</h1>" in c.get("/calculations/runs/").content.decode()
    assert "<h1>Data packs</h1>" in c.get("/reference-data/packs/").content.decode()
    r = c.get("/reference-data/?kind=acc_electric")  # the packs' filtered list, as it was linked before it moved
    assert r.status_code == 302 and r["Location"] == "/reference-data/packs/?kind=acc_electric"
    run = c.get("/calculations/engine-docs/").content.decode()
    assert '<li><a href="/calculations/">Calculations</a></li>' in run  # the section's own page heads the breadcrumb


def test_the_administration_dropdown_is_short(signed_in, cpuc_admin):
    html = signed_in(cpuc_admin).get("/").content.decode()
    admin = header(html).split('data-tour="nav-admin"', 1)[1].split("</details>", 1)[0]
    shown = re.findall(r"<li><a href=\"[^\"]+\">([^<]+)</a></li>", admin)
    assert len(shown) <= 7, shown
    assert "Every administration page" in admin
    landing = page_only(signed_in(cpuc_admin).get("/admin/").content.decode())
    every = [i.label for i in nav.landing("admin").items if "sysadmin" in i.who or "steward" in i.who]
    for label in every:  # everything the dropdown leaves out is on the page
        assert f">{label}</a>" in landing, label


# Where each PA's filings stand


@pytest.fixture
def deadlines(cpuc_admin):
    now = timezone.now()
    periods = {(p.kind, p.number): p for p in registry.ensure_periods(2028)}
    q1 = submissions.add_deadline(
        cpuc_admin, module="claims", kind="Q1 claim", period=periods[("quarter", 1)], due_at=now - timedelta(days=5)
    )
    q2 = submissions.add_deadline(
        cpuc_admin, module="claims", kind="Q2 claim", period=periods[("quarter", 2)], due_at=now + timedelta(days=60)
    )
    jan = submissions.add_deadline(
        cpuc_admin, module="monthly", kind="Monthly report", period=periods[("month", 1)], due_at=now - timedelta(days=40)
    )
    feb = submissions.add_deadline(
        cpuc_admin, module="monthly", kind="Monthly report", period=periods[("month", 2)], due_at=now - timedelta(days=10)
    )
    return {"q1": q1, "q2": q2, "jan": jan, "feb": feb}


@pytest.fixture
def sce(cpuc_admin):
    return access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")


def _version(sub, number, st, late=False):
    return SubmissionVersion.objects.create(submission=sub, number=number, status=st, late=late, reason="" if number == 1 else "a fix")


def test_the_columns_are_the_last_and_next_deadline_of_each_kind(deadlines):
    cols, without = status.columns()
    assert [(c.module, c.when, c.deadline) for c in cols] == [
        ("claims", status.LAST, deadlines["q1"]),
        ("claims", status.NEXT, deadlines["q2"]),
        ("monthly", status.LAST, deadlines["feb"]),  # the month most recently due, not January
    ]
    assert without == ["Budgets"]


def test_cpuc_staff_see_where_every_pa_stands(cpuc_admin, pge, sce, deadlines):
    S = SubmissionVersion.Status
    pge_q1 = Submission.objects.create(deadline=deadlines["q1"], organisation=pge)
    _version(pge_q1, 1, S.PUBLISHED, late=True)
    _version(pge_q1, 2, S.DRAFT)
    sce_feb = Submission.objects.create(deadline=deadlines["feb"], organisation=sce)
    _version(sce_feb, 1, S.VALIDATED)
    b = status.board(cpuc_admin)
    assert b.full and [r.organisation.code for r in b.rows] == ["PGE", "SCE"]  # the CPUC and the statewide pseudo-PA file nothing
    (pq1, pq2, pfeb), (sq1, sq2, sfeb) = (r.cells for r in b.rows)
    assert (pq1.status, pq1.detail) == ("Published", "correction under way (version 2, draft); filed late")
    assert (pq2.status, pq2.detail) == ("Not started", "")  # not due yet
    assert (pfeb.status, pfeb.detail) == ("Not started", "past due")
    assert (sfeb.status, sfeb.detail) == ("Validated", "past due")
    assert (sq1.status, sq2.status) == ("Not started", "Not started")
    assert b.groups() == [("Claims", 2), ("Monthly reports", 1)]


def test_a_pa_sees_its_own_and_only_whether_the_others_are_published(make_user, pge, pge_admin, sce, cpuc_admin, deadlines):
    S = SubmissionVersion.Status
    filer = make_user("claims@pge.example.org", "Cla Ims")
    access.grant(pge_admin, filer, pge, "claims.submit")
    _version(Submission.objects.create(deadline=deadlines["q1"], organisation=pge), 1, S.DRAFT)
    _version(Submission.objects.create(deadline=deadlines["q1"], organisation=sce), 1, S.VALIDATED)
    _version(Submission.objects.create(deadline=deadlines["feb"], organisation=sce), 1, S.PUBLISHED)
    b = status.board(filer)
    rows = {r.organisation.code: r.cells for r in b.rows}
    assert not b.full
    assert (rows["PGE"][0].status, rows["PGE"][0].detail) == ("Draft", "past due")
    assert rows["SCE"][0].status == "Not published yet" and not rows["SCE"][0].link  # SCE's draft is SCE's business
    assert rows["SCE"][2].status == "Published"
    assert rows["PGE"][2].status == "Not published yet"  # the filer may read claims for PG&E, not its monthly reports


def test_the_filings_page_shows_the_status_table(signed_in, cpuc_admin, pge, deadlines):
    html = page_only(signed_in(cpuc_admin).get("/filings/").content.decode())
    table = html.split('<table class="list filing-status">', 1)[1].split("</table>", 1)[0]
    assert '<th scope="colgroup" colspan="2">Claims</th>' in table and '<th scope="colgroup" colspan="1">Monthly reports</th>' in table
    assert '<th scope="row">PGE<br><span class="small muted">' in table
    assert "past due" in table.lower()
    assert "No deadlines are on the calendar yet for Budgets." in html
