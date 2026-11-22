"""Asking to correct a filing imported from legacy CEDARS (submissions.request_legacy_correction): only the filing organization's filers,
only for a year a legacy correction version covers and a module that corrects legacy filings, always decided by a CPUC approver; the
approved draft is filed on the platform, holds the imported files, is pinned to the year's correction version, and is handed to the
module's starter. After it, the submission is the platform's: no longer read-only, and no legacy download is imported over it."""

from datetime import timedelta

import pytest
from django.utils import timezone

from cedars.modules.claims import legacy as claims_legacy
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.specifications import service as specifications
from cedars.services.submissions import corrections, service
from cedars.services.submissions.models import ReopenRequest, SubmissionVersion

from .conftest import page_only

pytestmark = pytest.mark.django_db
FILES = [{"name": "Measure.parquet", "key": "legacy/x/Measure.parquet", "sha256": "abc", "bytes": 10}]


@pytest.fixture
def filer(make_user, pge, pge_admin):
    u = make_user("filer@pge.example.org")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


@pytest.fixture
def approver(make_user, cpuc_admin, cpuc):
    u = make_user("approver@cpuc.example.org")
    access.grant(cpuc_admin, u, cpuc, "claims.approve")
    return u


@pytest.fixture
def started(monkeypatch):
    """Claims as a module that corrects legacy filings (its own registration comes with its correction pages): the legacy year is the
    deadline's period year, and the starter records what it was handed."""
    calls = []
    monkeypatch.setitem(
        service._legacy_corrections,
        "claims",
        service.LegacyCorrections(year=lambda d: d.period.year, start=lambda new, old: calls.append((new.pk, old.pk))),
    )
    return calls


def _imported(pge, year=2025, **calculation):
    deadline = claims_legacy._deadline(year)
    return service.import_version(
        None, deadline=deadline, organisation=pge, files=FILES, reason="legacy CEDARS download", calculation=calculation
    )


def test_a_correction_is_requested_approved_and_started_on_the_years_version(filer, approver, pge, started):
    v1 = _imported(pge)
    sub = v1.submission
    assert service.is_imported(sub) and service.legacy_correction_refusal(filer, sub) == ""
    spec = service.legacy_correction_spec(sub)
    assert spec == specifications.for_legacy("claims", 2025) and spec.calculation == "cet-24.3"
    with pytest.raises(AccessDenied, match="legacy correction"):  # the ordinary reopen path points to this one
        service.request_reopen(filer, sub, "wrong units")
    with pytest.raises(AccessDenied, match="reason"):
        service.request_legacy_correction(filer, sub, " ")
    req = service.request_legacy_correction(filer, sub, "Evaluator found double-counted units")
    assert isinstance(req, ReopenRequest) and req.legacy and req.status == "pending"
    with pytest.raises(AccessDenied, match="already waiting"):
        service.request_legacy_correction(filer, sub, "again")
    with pytest.raises(AccessDenied, match="approver"):
        service.decide_reopen(filer, req, True)
    v2 = service.decide_reopen(approver, req, True, "Go ahead")
    assert (v2.number, v2.status, v2.origin) == (2, "draft", SubmissionVersion.Origin.FILED)
    assert v2.files == v1.files and v2.specification_version == spec and v2.reason == "Evaluator found double-counted units"
    sub.deadline.refresh_from_db()
    assert sub.deadline.specification_version == spec  # its checks and pricing follow the correction version
    assert started == [(v2.pk, v1.pk)]
    assert service.current(sub) == v1  # the imported version stands until the correction is confirmed
    assert not service.is_imported(sub) and "legacy CEDARS" not in corrections.refusal(filer, v2)
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"reopen.requested", "reopen.approved", "deadline.specification_set", "submission.reopened"} <= actions
    reopened = AuditEvent.objects.get(action="submission.reopened", object_id=str(v2.pk))
    assert reopened.after["legacy"] is True and reopened.after["from_version"] == 1
    with pytest.raises(AccessDenied, match="filed"):  # no later legacy download is imported over it
        _imported(pge)
    assert service.legacy_correction_spec(sub) is None  # nothing left to ask: it is corrected like any filing now


def test_an_approval_policy_of_open_still_waits_for_an_approver(filer, pge, cpuc_admin, started):
    service.set_policy(cpuc_admin, "claims", reopen_after_due="open")
    req = service.request_legacy_correction(filer, _imported(pge).submission, "r")
    assert isinstance(req, ReopenRequest) and req.status == "pending" and started == []


def test_a_decline_leaves_it_imported(filer, approver, pge, started):
    sub = _imported(pge).submission
    req = service.request_legacy_correction(filer, sub, "r")
    with pytest.raises(AccessDenied, match="Say why"):
        service.decide_reopen(approver, req, False)
    assert service.decide_reopen(approver, req, False, "File it in this year's claims") is None
    assert service.is_imported(sub) and started == [] and sub.versions.count() == 1


@pytest.mark.parametrize(
    "case, refusal",
    [
        ("an earlier year", "cannot be corrected"),
        ("outside the totals", "cannot be corrected"),
        ("another organization", "Only a filer of PGE"),
        ("a module that corrects none", "cannot be corrected"),
    ],
)
def test_who_and_what_may_not_ask(case, refusal, filer, make_user, pge, started, monkeypatch):
    actor = filer
    if case == "an earlier year":
        sub = _imported(pge, 2023).submission
    elif case == "outside the totals":
        sub = _imported(pge, outside_totals=12).submission
    elif case == "another organization":
        sub, actor = _imported(pge).submission, make_user("someone@sce.example.org")
    else:
        sub = _imported(pge).submission
        monkeypatch.delitem(service._legacy_corrections, "claims")
    assert refusal in service.legacy_correction_refusal(actor, sub)
    with pytest.raises(AccessDenied):
        service.request_legacy_correction(actor, sub, "r")


def test_a_filing_never_imported_is_reopened_as_before(filer, pge, cpuc_admin, started):
    period = [p for p in claims_legacy.registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]
    deadline = service.add_deadline(
        cpuc_admin, module="claims", kind="quarterly claim", period=period, due_at=timezone.now() + timedelta(days=1)
    )
    v = service.start(filer, deadline, pge)
    assert not service.is_imported(v.submission) and service.legacy_correction_spec(v.submission) is None


def test_the_pages_offer_the_request_and_the_approver_sees_it_is_legacy(client, signed_in, filer, approver, pge, started):
    sub = _imported(pge).submission
    signed_in(filer)
    r = client.post(f"/filings/submissions/{sub.pk}/legacy-correction/", {"reason": "Units double counted"})
    assert r.status_code == 302
    req = ReopenRequest.objects.get(submission=sub)
    assert req.legacy
    signed_in(approver)
    html = page_only(client.get("/filings/reopen-requests/").content.decode())
    assert "Legacy correction" in html and "Legacy corrections, 2024-2025" in html and "CET 24.3 replica" in html
    client.post(f"/filings/reopen-requests/{req.pk}", {"decision": "approve", "note": ""})
    assert sub.versions.filter(number=2, origin="filed").exists()
