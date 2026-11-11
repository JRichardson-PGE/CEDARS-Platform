"""Your work (UI review 3, U5): the signed-in person's own work first on the front page and the section pages (core/yourwork.py), the
organization's own filings first on the Filings page, and the next one to file marked on the Claims and Monthly reports lists."""

from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.utils import timezone

from cedars.core import yourwork
from cedars.services.accounts import service as access
from cedars.services.registry import service as registry
from cedars.services.submissions import service as submissions
from cedars.services.submissions import status
from cedars.services.submissions.models import Submission, SubmissionVersion

from .conftest import page_only

pytestmark = pytest.mark.django_db
S = SubmissionVersion.Status


def main(html: str) -> str:
    return page_only(html).split("<main", 1)[1].split("</main>", 1)[0]


def your_work(html: str) -> str:
    return main(html).split('<section class="your-work"', 1)[1].split("</section>", 1)[0]


@pytest.fixture
def deadlines(cpuc_admin):
    from cedars.modules.claims.models import ClaimYear

    ClaimYear.objects.create(year=2028, acc_version="2026")  # the claims module's pages for its deadlines
    now = timezone.now()
    periods = {(p.kind, p.number): p for p in registry.ensure_periods(2028)}
    q1 = submissions.add_deadline(
        cpuc_admin, module="claims", kind="Q1 claim", period=periods[("quarter", 1)], due_at=now - timedelta(days=5)
    )
    q2 = submissions.add_deadline(
        cpuc_admin, module="claims", kind="Q2 claim", period=periods[("quarter", 2)], due_at=now + timedelta(days=60)
    )
    feb = submissions.add_deadline(
        cpuc_admin, module="monthly", kind="Monthly report", period=periods[("month", 2)], due_at=now - timedelta(days=10)
    )
    return {"q1": q1, "q2": q2, "feb": feb}


@pytest.fixture
def filer(make_user, pge, pge_admin):
    u = make_user("claims@pge.example.org", "Cla Ims")
    access.grant(pge_admin, u, pge, "claims.read")
    access.grant(pge_admin, u, pge, "claims.submit")
    return u


# --- the registry


def test_a_part_names_only_pages_that_have_your_work():
    with pytest.raises(ValueError):
        yourwork.register("x", lambda request, page: None, order=1, pages=("nowhere",))


def test_signed_out_there_is_no_your_work(client, deadlines):
    html = client.get("/").content.decode()
    assert 'class="your-work"' not in html


def test_a_failing_part_is_left_out_and_the_rest_shown(make_user, monkeypatch):
    def broken(request, page):
        raise RuntimeError("down")

    monkeypatch.setitem(yourwork._providers, "broken", yourwork.Provider("broken", 1, ("front",), broken))
    request = RequestFactory().get("/")
    request.user = make_user("someone@example.org")
    parts = yourwork.parts(request, "front")
    assert "broken" not in [p.key for p in parts] and "queue" in [p.key for p in parts]


# --- the front page


def test_the_front_page_shows_the_queue_and_the_organization_s_next_filings(signed_in, filer, pge, deadlines):
    v = SubmissionVersion.objects.create(submission=Submission.objects.create(deadline=deadlines["q1"], organisation=pge), number=1)
    assert v.status == S.DRAFT
    html = signed_in(filer).get("/").content.decode()
    work = your_work(html)
    assert '<h2 id="your-work-h">Your work</h2>' in work
    assert "Waiting on you" in work and "Nothing is waiting on you." in work
    filings = work.split('data-part="filings"', 1)[1].split('data-part="', 1)[0]
    assert "PGE&#x27;s next filings" in filings
    assert "Claims: Q1 claim, 2028Q1" in filings  # the past-due draft, not the next quarter
    assert "Q2 claim" not in filings
    assert "Monthly reports" not in filings  # a claims filer cannot open PG&E's monthly reports
    assert 'class="status ' in filings and ">Draft<" in filings and "past due" in filings
    assert f'href="/claims/periods/{deadlines["q1"].pk}/?org=PGE">Continue<span class="sr-only">' in filings
    # the portfolio section follows, unchanged
    assert main(html).index('class="your-work"') < main(html).index('class="front-trends"')


def test_a_confirmed_filing_gives_way_to_the_next_one(signed_in, filer, pge, deadlines):
    sub = Submission.objects.create(deadline=deadlines["q1"], organisation=pge)
    SubmissionVersion.objects.create(submission=sub, number=1, status=S.CONFIRMED)
    b = status.board(filer, org=pge)
    picks = status.next_filings(b)
    assert [(c.deadline, cell.action) for c, cell in picks] == [(deadlines["q2"], "Start")]


def test_an_account_with_a_pending_access_request_sees_it(signed_in, make_user, pge, pge_admin):
    u = make_user("new@example.org", "New Comer")
    access.request_access(u, pge, ["claims.read"], "I file PG&E's claims")
    work = your_work(signed_in(u).get("/").content.decode())
    assert "Your access requests" in work and "Access to PGE (Pacific Gas and Electric)" in work
    assert "waiting for PGE&#x27;s administrators to decide" in work
    assert 'data-part="filings"' not in work  # it acts for no organization yet


def test_the_queue_part_shows_three_items_and_the_count(signed_in, pge_admin, pge, make_user):
    for n in range(4):
        access.request_access(make_user(f"p{n}@example.org"), pge, ["claims.read"])
    work = your_work(signed_in(pge_admin).get("/").content.decode())
    queue = work.split('data-part="queue"', 1)[1].split('data-part="', 1)[0]
    assert 'Waiting on you <span class="small muted">(4)</span>' in queue
    assert queue.count('<p class="what">') == 3 and queue.count("asks to join PGE") == 6  # each said once more by its action
    assert '<a href="/queue/">My queue (4)</a>' in queue
    assert queue.count('>Decide<span class="sr-only">') == 3


# --- the section pages


def test_the_filings_page_starts_with_your_organization_s_filings(signed_in, filer, pge, deadlines):
    html = main(signed_in(filer).get("/filings/").content.decode())
    own = html.index('id="your-filings"')
    assert own < html.index('id="filing-status"') < html.index('class="section-group"')
    section = html[own:].split("</section>", 1)[0]
    assert "PGE's filings" in section
    assert "Claims: Q1 claim, 2028Q1" in section and "Claims: Q2 claim, 2028Q2" in section
    assert f'href="/claims/periods/{deadlines["q1"].pk}/?org=PGE">Start<span class="sr-only">' in section
    board = html.split('<table class="list filing-status">', 1)[1]
    assert '<tr class="own"><th scope="row">PGE <span class="small">(yours)</span>' in board
    assert 'class="status-link"' in board  # a status that opens the filing looks like a link


def test_cpuc_staff_have_no_own_filings(signed_in, cpuc_admin, pge, deadlines):
    html = main(signed_in(cpuc_admin).get("/filings/").content.decode())
    assert 'id="your-filings"' not in html and 'id="filing-status"' in html


def test_the_calculations_page_shows_your_runs(signed_in, make_user):
    from cedars.modules.calculations.models import Run

    u = make_user("analyst@example.org", "Ana Lyst")
    c = signed_in(u)
    empty = your_work(c.get("/calculations/").content.decode())
    assert "Your recent runs" in empty and "You have not started a run yet." in empty
    assert 'data-part="runs"' not in c.get("/").content.decode()  # the front page leaves it out until there is a run
    for n in range(4):
        Run.objects.create(owner=u, name=f"Run {n}", engine_release="x", acc_version="2026", input_hash="0" * 64, storage_prefix="p")
    done = Run.objects.get(name="Run 3")
    done.status, done.finished_at = Run.Status.SUCCEEDED, timezone.now()
    done.save()
    work = your_work(c.get("/calculations/").content.decode())
    assert 'Your recent runs <span class="small muted">(4)</span>' in work
    assert f'<a href="/calculations/runs/{done.pk}/">Run 3</a>' in work
    assert f'href="/calculations/runs/{done.pk}/results/">Results<span class="sr-only">: Run 3</span>' in work
    assert "Run 0" not in work  # three shown
    assert 'data-part="runs"' in c.get("/").content.decode()


def test_the_published_and_reference_pages_show_your_views_and_sets(signed_in, make_user):
    from cedars.modules.data.models import SavedView
    from cedars.services.refdata.models import CostTestBuild

    u = make_user("analyst@example.org", "Ana Lyst")
    c = signed_in(u)
    assert 'class="your-work"' not in main(c.get("/published/").content.decode())  # nothing of theirs yet: no empty cards
    SavedView.objects.create(owner=u, name="My TSB view", page="records", query="")
    CostTestBuild.objects.create(owner=u, data={"id": "EE-2028", "version": "1", "label": "A test set"})
    published = your_work(c.get("/published/").content.decode())
    assert "Your saved views" in published and ">My TSB view</a>" in published
    reference = your_work(c.get("/reference-data/").content.decode())
    assert "Cost-test sets you are building" in reference and "EE-2028 1: A test set" in reference
    assert '>Continue<span class="sr-only">' in reference


# --- Claims and Monthly reports


def test_the_claims_list_marks_the_next_one_to_file_and_gives_each_row_its_action(signed_in, filer, pge, cpuc_admin, deadlines):
    rows = [
        {"deadline": deadlines["q1"], "latest": None, "can_open": True},
        {"deadline": deadlines["q2"], "latest": None, "can_open": True},
    ]
    status.mark_next(rows, pge)
    assert [(r["next"], r["action"]) for r in rows] == [(True, "Start"), (False, "Open")]
    confirmed = SubmissionVersion(number=1, status=S.CONFIRMED)
    rows[0]["latest"] = confirmed
    status.mark_next(rows, pge)
    assert [(r["next"], r["action"]) for r in rows] == [(False, "Open"), (True, "Start")]
    status.mark_next(rows := [{"deadline": deadlines["q1"], "latest": None}], None)
    assert "next" not in rows[0]  # the CPUC files nothing


def test_the_monthly_list_marks_the_next_report(signed_in, make_user, pge, pge_admin, cpuc_admin):
    from cedars.modules.monthly import service as monthly

    u = make_user("monthly@pge.example.org")
    for p in ("monthly.read", "monthly.submit"):
        access.grant(pge_admin, u, pge, p)
    monthly.set_up_year(cpuc_admin, year=2028, acc_version="2026", due_day=20)
    html = main(signed_in(u).get("/monthly/").content.decode())
    assert html.count("Next to file") == 1
    assert 'class="next-to-file"' in html and '>Start<span class="sr-only">: January report</span>' in html


def test_an_administrator_who_may_not_open_the_filings_gets_no_own_filings(signed_in, pge_admin, pge, deadlines):
    c = signed_in(pge_admin)
    assert 'id="your-filings"' not in main(c.get("/filings/").content.decode())
    assert 'data-part="filings"' not in main(c.get("/").content.decode())
