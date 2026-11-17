"""Who files a filing (Jake, 2026-11-09): "when setting up or editing a filing's set-up, CPUC should be able to add or remove PAs from
the filing. Not every PA will file in every filing." A set-up lists its filers (submissions.set_filers); only they start a filing, and
the Filings page's status lists only the PAs on the filings it shows."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.modules.budgets import service as budgets
from cedars.modules.claims import service as claims
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions import service as submissions
from cedars.services.submissions import status
from cedars.services.submissions.models import Deadline, Submission

from .conftest import page_only


@pytest.fixture
def sce(cpuc_admin):
    return access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")


@pytest.fixture
def bayren(cpuc_admin):
    return access.create_organisation(cpuc_admin, code="BAYREN", name="Bay Area Regional Energy Network", kind="pa", pa_type="REN")


@pytest.fixture
def filing(cpuc_admin):
    return budgets.add_filing(
        cpuc_admin, first_year=2028, kind="Application", due_at=timezone.now() + timedelta(days=30), acc_version="2026"
    )


def _codes(deadline):
    return sorted(o.code for o in deadline.filers.all())


def test_a_filing_set_up_with_some_pas_is_filed_by_them_only(cpuc_admin, pge, sce, bayren):
    d = budgets.add_filing(
        cpuc_admin,
        first_year=2032,
        kind="Advice letter",
        name="2032 Advice letter",
        due_at=timezone.now() + timedelta(days=30),
        acc_version="2026",
        filers=[pge, bayren],
    )
    assert _codes(d) == ["BAYREN", "PGE"]
    assert submissions.on_filing(d, pge) and not submissions.on_filing(d, sce)
    assert not submissions.files_for(d, sce)
    with pytest.raises(AccessDenied, match="SCE is not one of the PAs filing"):
        submissions.start(cpuc_admin, d, sce)
    ev = AuditEvent.objects.get(action="deadline.filers_changed", object_id=str(d.pk))
    assert ev.before == {"filers": "every PA"} and ev.after == {"filers": ["BAYREN", "PGE"]}


def test_without_a_list_every_pa_files(filing, pge, sce):
    assert submissions.expected(filing) is None
    assert submissions.on_filing(filing, pge) and submissions.on_filing(filing, sce)


def test_who_files_changes_with_a_reason_and_keeps_a_pa_that_started(cpuc_admin, pge_admin, filing, pge, sce, bayren):
    with pytest.raises(AccessDenied):
        submissions.set_filers(pge_admin, [filing], [pge], "mine")  # a CPUC system administrator sets it
    with pytest.raises(AccessDenied, match="reason"):
        submissions.set_filers(cpuc_admin, [filing], [pge], " ")
    with pytest.raises(AccessDenied, match="at least one"):
        submissions.set_filers(cpuc_admin, [filing], [], "nobody")
    Submission.objects.create(deadline=filing, organisation=sce)  # SCE has started
    with pytest.raises(AccessDenied, match="SCE has started filing"):
        submissions.set_filers(cpuc_admin, [filing], [pge], "PG&E only")
    assert submissions.set_filers(cpuc_admin, [filing], [pge, sce], "BayREN files on its own") == [filing]
    assert _codes(filing) == ["PGE", "SCE"]
    assert submissions.set_filers(cpuc_admin, [filing], [sce, pge], "the same") == []  # no change, no event
    assert AuditEvent.objects.filter(action="deadline.filers_changed").count() == 1
    submissions.set_filers(cpuc_admin, [filing], [pge, sce, bayren], "BayREN files after all")  # added back
    assert _codes(filing) == ["BAYREN", "PGE", "SCE"]


def test_the_filings_status_lists_only_the_pas_on_the_filings(cpuc_admin, filing, pge, sce, bayren):
    submissions.set_filers(cpuc_admin, [filing], [pge, sce], "BayREN does not file this application")
    b = status.board(cpuc_admin)
    assert [r.organisation.code for r in b.rows] == ["PGE", "SCE"]  # BayREN is on none of the filings shown
    claim = claims.add_year(
        cpuc_admin,
        year=2029,
        acc_version="2026",
        due={kind: timezone.now() + timedelta(days=40 + 30 * n) for n, (kind, _) in enumerate(claims.PERIODS)},
        filers=[bayren, pge],
    )
    own = claims.periods(claim.year)
    assert all(_codes(d) == ["BAYREN", "PGE"] for d in own)
    assert all(not d.filers.exists() for d in claims.periods(claim.year, statewide=True))  # the statewide ones: the lead PAs
    b = status.board(cpuc_admin)
    rows = {r.organisation.code: r.cells for r in b.rows}
    assert sorted(rows) == ["BAYREN", "PGE", "SCE"]
    budget = next(i for i, c in enumerate(b.columns) if c.deadline == filing)
    assert (rows["BAYREN"][budget].status, rows["BAYREN"][budget].detail) == ("Nothing to file", "not on this filing")
    assert rows["PGE"][budget].status == "Not started"


def test_the_choose_a_pa_list_has_only_the_filers(cpuc_admin, filing, pge, sce):
    submissions.set_filers(cpuc_admin, [filing], [sce], "SCE only")
    rows = submissions.filer_statuses(filing, lambda s: True, every=True)
    assert [r["organisation"].code for r in rows] == ["SCE"]


def test_the_set_up_page_adds_and_removes_pas(signed_in, cpuc_admin, filing, pge, sce, bayren):
    c = signed_in(cpuc_admin)
    url = reverse("budgets:setup", args=[filing.pk])
    page = page_only(c.get(url).content.decode())
    assert 'id="setup-filers"' in page and "Who files" in page and 'value="BAYREN"' in page
    r = c.post(url, {"action": "filers", "filers-pas": ["PGE", "SCE"], "filers-reason": "BayREN is not in this application"})
    assert r.status_code == 302
    assert _codes(Deadline.objects.get(pk=filing.pk)) == ["PGE", "SCE"]
    r = c.post(url, {"action": "filers", "filers-reason": "nobody"})  # at least one PA
    assert r.status_code == 200 and "Choose at least one program administrator to file." in r.content.decode()
    r = c.post(url, {"action": "filers", "filers-pas": ["PGE", "SCE", "BAYREN"], "filers-reason": "BayREN joins"})
    assert r.status_code == 302 and _codes(Deadline.objects.get(pk=filing.pk)) == ["BAYREN", "PGE", "SCE"]


def test_a_pa_off_the_filing_sees_nothing_to_file(signed_in, cpuc_admin, pge_admin, filing, pge, sce):
    submissions.set_filers(cpuc_admin, [filing], [sce], "SCE only")
    page = page_only(signed_in(pge_admin).get(reverse("budgets:index")).content.decode())
    assert "Nothing to file: PGE is not on this filing" in page


def test_the_deadline_s_history_says_who_files_changed(client, cpuc_admin, filing, pge, sce):
    submissions.set_filers(cpuc_admin, [filing], [sce], "SCE only")
    page = client.get(reverse("submissions:deadline", args=[filing.pk])).content.decode()
    assert "Who files changed" in page and "Who files: every PA to SCE" in page
