"""Cancel one filing of a filing set, and edit filing sets directly (Jake, 2026-11-09: "I should be able to cancel a single filing from
any of the filing sets too" and "I should be able to edit the filing sets directly too"). One quarter's claim, one month's report or one
budget filing is canceled from its set-up page or its calendar page while no organization has started it (submissions.retire_deadline),
and restored; the Filing sets page lists every set with an Edit link to its set-up page."""

from datetime import timedelta

import pytest
from django.utils import timezone

from cedars.modules.budgets import service as budgets
from cedars.modules.claims import service as claims
from cedars.modules.monthly import service as monthly
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions import service as submissions
from cedars.services.submissions import status
from cedars.services.submissions.models import Deadline, Submission

from .conftest import page_only


@pytest.fixture
def year(cpuc_admin):
    claims.add_year(
        cpuc_admin,
        year=2031,
        acc_version="2026",
        due={k: timezone.now() + timedelta(days=40 + i) for i, (k, _) in enumerate(claims.PERIODS)},
    )
    return claims.ClaimYear.objects.get(year=2031)


@pytest.fixture
def filing(cpuc_admin):
    return budgets.add_filing(
        cpuc_admin, first_year=2032, kind="Application", due_at=timezone.now() + timedelta(days=30), acc_version="2026"
    )


def test_one_claim_of_a_year_is_canceled_and_restored_from_its_set_up_page(client, signed_in, cpuc_admin, pge, year):
    q2 = claims.periods(2031)[1]
    url = "/claims/years/2031/setup/"
    c = signed_in(cpuc_admin)
    page = c.get(url).content.decode()
    assert 'id="setup-filings"' in page and 'id="cancel-filing-form"' in page and 'id="restore-filing-form"' not in page
    assert "Reason for canceling it" in c.post(url, {"action": "cancel", "cancel-filing": q2.pk, "cancel-reason": " "}).content.decode()
    r = c.post(url, {"action": "cancel", "cancel-filing": q2.pk, "cancel-reason": "No Q2 claim this year"})
    assert r.status_code == 302 and r.url == url
    q2.refresh_from_db()
    assert q2.retired_at and q2.retired_reason == "No Q2 claim this year"
    assert all(d.retired_at is None for d in claims.year_deadlines(year) if d.pk != q2.pk)  # the rest of the set stays
    ev = AuditEvent.objects.get(action="deadline.retired", object_id=str(q2.pk))
    assert ev.reason == "No Q2 claim this year" and ev.actor == cpuc_admin
    page = c.get(url).content.decode()
    assert "Canceled" in page and "No Q2 claim this year" in page and 'id="restore-filing-form"' in page
    assert q2 not in submissions.Deadline.objects.active()
    with pytest.raises(AccessDenied, match="taken off"):
        submissions.start(cpuc_admin, q2, pge)
    # the Claims page marks it, and the PA has nothing to file
    assert "Canceled" in page_only(c.get("/claims/").content.decode())
    r = c.post(url, {"action": "restore", "restore-filing": q2.pk, "restore-reason": "Q2 is due after all"})
    assert r.status_code == 302
    q2.refresh_from_db()
    assert q2.retired_at is None and AuditEvent.objects.filter(action="deadline.restored", object_id=str(q2.pk)).exists()


def test_a_filing_an_organization_started_is_not_offered_for_canceling(client, signed_in, cpuc_admin, pge, year):
    q1 = claims.periods(2031)[0]
    Submission.objects.create(deadline=q1, organisation=pge)
    with pytest.raises(AccessDenied, match="1 organization has started filing"):
        submissions.retire_deadline(cpuc_admin, q1, "too late")
    c = signed_in(cpuc_admin)
    page = c.get("/claims/years/2031/setup/").content.decode()
    form = page.split('id="cancel-filing-form"', 1)[1].split("</form>", 1)[0]
    assert f'value="{q1.pk}"' not in form and f'value="{claims.periods(2031)[1].pk}"' in form
    r = c.post("/claims/years/2031/setup/", {"action": "cancel", "cancel-filing": q1.pk, "cancel-reason": "x"})
    assert r.status_code == 200  # not a choice: refused, nothing canceled
    q1.refresh_from_db()
    assert q1.retired_at is None


def test_only_a_cpuc_system_administrator_cancels(pge_admin, filing, client, signed_in):
    with pytest.raises(AccessDenied):
        submissions.retire_deadline(pge_admin, filing, "mine")
    c = signed_in(pge_admin)
    assert c.get(f"/budgets/filings/{filing.pk}/setup/").status_code == 404
    assert c.post(f"/filings/calendar/{filing.pk}/", {"action": "retire", "reason": "x"}).status_code == 404
    assert c.get("/filings/calendar/sets/").status_code == 404


def test_a_budget_filing_is_canceled_from_its_calendar_page_which_links_its_set(client, signed_in, cpuc_admin, filing):
    c = signed_in(cpuc_admin)
    url = f"/filings/calendar/{filing.pk}/"
    page = c.get(url).content.decode()
    assert 'id="cancel-h"' in page and f'href="/budgets/filings/{filing.pk}/setup/"' in page
    r = c.post(url, {"action": "retire", "reason": "The CPUC withdrew the request"})
    assert r.status_code == 302
    filing.refresh_from_db()
    assert filing.retired_at
    page = c.get(url).content.decode()
    assert "Canceled" in page and 'id="restore-h"' in page
    assert "Canceled" in page_only(c.get("/budgets/").content.decode())
    assert "Canceled" in page_only(c.get(f"/budgets/filings/{filing.pk}/").content.decode())


def test_a_month_of_monthly_reports_is_canceled_and_left_off_the_filings_status(cpuc_admin, pge):
    s = monthly.set_up_year(cpuc_admin, year=2031, acc_version="2026", due_day=20)
    months = list(monthly.months(s.year))
    submissions.retire_deadline(cpuc_admin, months[2], "No March report")
    assert months[2].pk not in {d.pk for d in Deadline.objects.active()}
    assert all(c.deadline.pk != months[2].pk for c in status.board(cpuc_admin).columns)


def test_filing_sets_lists_every_set_with_its_edit_link(client, signed_in, cpuc_admin, year, filing):
    submissions.retire_deadline(cpuc_admin, claims.periods(2031)[3], "No Q4 claim")
    sets = {s["what"]: s for s in submissions.filing_sets()}
    assert sets["2031 claims"]["url"] == "/claims/years/2031/setup/" and sets["2031 claims"]["canceled"] == 1
    assert any(s["url"] == f"/budgets/filings/{filing.pk}/setup/" for s in sets.values())
    c = signed_in(cpuc_admin)
    page = c.get("/filings/calendar/sets/").content.decode()
    assert 'href="/claims/years/2031/setup/"' in page and f'href="/budgets/filings/{filing.pk}/setup/"' in page
    assert "(1 canceled)" in page
    assert 'href="/filings/calendar/sets/"' in c.get("/filings/calendar/").content.decode()
    assert 'href="/filings/calendar/sets/"' in c.get("/filings/calendar/new/").content.decode()
    assert 'href="/claims/years/2031/setup/"' in c.get(f"/filings/calendar/{claims.periods(2031)[0].pk}/").content.decode()
