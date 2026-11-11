# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""Notices for requests and decisions (UI review 3, U2): a person's own requests and the requests waiting on them are told directly,
on the page and by email unless they turn it off on Notification settings; a decider who also subscribed to the audit event is told
once. My queue lists a filing reopened for a correction until it is priced, and the approver's card says what they need to decide."""

from datetime import date, timedelta

import pytest
from django.utils import timezone

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications
from cedars.services.notifications.models import Notification, Preference
from cedars.services.registry import service as registry
from cedars.services.submissions import queue, service
from cedars.services.submissions.models import SubmissionVersion

from .test_program_change_requests import approver as programs_approver  # noqa: F401 - fixture
from .test_program_change_requests import soon
from .test_programs import programs  # noqa: F401 - fixture
from .test_submissions import _priced, analyst, deadline  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


@pytest.fixture
def claims_approver(make_user, cpuc, cpuc_admin):
    u = make_user("approver@cpuc.example.org", "Avery Prover")
    access.grant(cpuc_admin, u, cpuc, "claims.approve")
    return u


@pytest.fixture
def past_due(analyst, deadline, pge, cpuc_admin):
    v1 = service.confirm(analyst, _priced(analyst, deadline, pge), "h1")
    service.move_deadline(cpuc_admin, deadline, timezone.now() - timedelta(minutes=1), "the due date passes")
    return SubmissionVersion.objects.get(pk=v1.pk)


def titles(user):
    return [n.title for n in Notification.objects.filter(user=user).order_by("id")]


def test_a_correction_request_reaches_the_approvers_and_their_delegates_and_the_decision_the_filer(
    analyst,
    past_due,
    claims_approver,
    make_user,
    cpuc_admin,
    cpuc,
    django_capture_on_commit_callbacks,
):
    deputy = make_user("deputy@cpuc.example.org")
    access.grant(cpuc_admin, deputy, cpuc, "claims.read")
    service.delegate(claims_approver, module="claims", delegate_to=deputy, starts=date.today(), ends=date.today() + timedelta(days=7))
    name = past_due.submission.plain_name
    with django_capture_on_commit_callbacks(execute=True):
        req = service.request_reopen(analyst, past_due.submission, "double counted")
    for person in (claims_approver, deputy):
        n = Notification.objects.get(user=person)
        assert n.title == f"Correction requested: {name}" and n.link == "/filings/reopen-requests/" and n.email
        assert "Reason: double counted" in n.body
    assert titles(analyst) == []  # the person who asked is not told of their own request
    with pytest.raises(AccessDenied, match="Say why"):
        service.decide_reopen(claims_approver, req, False, " ")
    with django_capture_on_commit_callbacks(execute=True):
        service.decide_reopen(claims_approver, req, False, "send the evaluator's memo first")
    n = Notification.objects.get(user=analyst)
    assert n.title == f"Correction declined: {name}" and n.body == "Reason: send the evaluator's memo first"
    with django_capture_on_commit_callbacks(execute=True):
        req2 = service.request_reopen(analyst, past_due.submission, "double counted, memo attached")
        service.decide_reopen(deputy, req2, True)
    assert titles(analyst)[-1] == f"Correction approved: {name}"
    assert "Version 2 is open for your correction" in Notification.objects.filter(user=analyst).order_by("id").last().body


def test_a_subscribed_approver_is_told_once_and_a_person_may_turn_request_notices_off(
    analyst,
    past_due,
    claims_approver,
    django_capture_on_commit_callbacks,
):
    notifications.subscribe(claims_approver, "reopen.requested", "email")
    notifications.subscribe(analyst, "reopen.declined", "in_app")
    Preference.objects.create(user=analyst, own_requests="off")
    with django_capture_on_commit_callbacks(execute=True):
        req = service.request_reopen(analyst, past_due.submission, "double counted")
    assert len(titles(claims_approver)) == 1  # the direct notice and the subscription share a key
    with django_capture_on_commit_callbacks(execute=True):
        service.decide_reopen(claims_approver, req, False, "no")
    # direct notices are off for the filer, so their own subscription brings the decision, on the page only
    n = Notification.objects.get(user=analyst)
    assert not n.email and n.key == notifications.request_key("submissions.reopenrequest", req.pk, "declined")


def test_page_only_notices(analyst, past_due, claims_approver, django_capture_on_commit_callbacks):
    Preference.objects.create(user=claims_approver, requests_to_decide="in_app")
    with django_capture_on_commit_callbacks(execute=True):
        service.request_reopen(analyst, past_due.submission, "double counted")
    assert not Notification.objects.get(user=claims_approver).email


def test_a_program_change_request_its_decision_and_its_withdrawal(
    pge_admin,
    programs_approver,
    programs,
    django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        req = registry.request_change(pge_admin, programs["hvac"], effective=soon(), reason="D.26-09-001", segment="equity")
    n = Notification.objects.get(user=programs_approver)
    assert n.title.startswith("Change requested: PGE_Res_HVAC") and n.link == f"/programs/requests/{req.pk}/"
    with django_capture_on_commit_callbacks(execute=True):
        registry.withdraw_change(pge_admin, req, "asked too early")
    assert titles(programs_approver)[-1].startswith("Change request withdrawn: PGE_Res_HVAC")
    assert titles(pge_admin) == []
    with django_capture_on_commit_callbacks(execute=True):
        req = registry.request_change(pge_admin, programs["hvac"], effective=soon(), reason="D.26-09-001", segment="equity")
        registry.decide_change(programs_approver, req, False, "needs a resolution first")
    n = Notification.objects.get(user=pge_admin)
    assert n.title.startswith("Change declined: PGE_Res_HVAC") and n.body == "Reason: needs a resolution first"


def test_an_access_request_reaches_the_organizations_administrators_or_the_cpucs(
    make_user, pge, cpuc_admin, django_capture_on_commit_callbacks
):
    person = make_user("new@pge.example.org", "Nia New")
    with django_capture_on_commit_callbacks(execute=True):
        req = access.request_access(person, pge, ["claims.read"], "I file PG&E's claims")
    n = Notification.objects.get(user=cpuc_admin)  # PGE has no administrator yet
    assert n.title == "Access requested: Nia New, PGE" and "no administrator yet" in n.body and n.link == "/admin/organizations/PGE/#req-h"
    with django_capture_on_commit_callbacks(execute=True):
        access.withdraw_request(person, req)
    assert titles(cpuc_admin)[-1] == "Access request withdrawn: Nia New, PGE"


def test_an_access_decision_reaches_the_person_who_asked(make_user, pge, pge_admin, django_capture_on_commit_callbacks):
    person = make_user("new@pge.example.org", "Nia New")
    with django_capture_on_commit_callbacks(execute=True):
        req = access.request_access(person, pge, ["claims.read", "claims.submit"])
    assert titles(pge_admin) == ["Access requested: Nia New, PGE"]
    with django_capture_on_commit_callbacks(execute=True):
        access.decide_request(pge_admin, req, True, permission_codes=["claims.read"])
    n = Notification.objects.get(user=person)
    assert n.title == "Your access request to PGE was approved" and "Claims: read" in n.body and "submit" not in n.body
    assert n.link == "/account/"


def test_my_queue_lists_a_reopened_filing_until_it_is_priced(analyst, past_due, claims_approver):
    req = service.request_reopen(analyst, past_due.submission, "double counted")
    assert queue.corrections(analyst) == []
    v2 = service.decide_reopen(claims_approver, req, True)
    items = queue.corrections(analyst)
    assert [i.what for i in items] == [f"Claims: {past_due.submission.plain_name}, version 2"]
    assert "double counted" in items[0].note and queue.corrections(claims_approver) == []
    service.attach_files(analyst, v2, [{"name": "Measure.csv", "key": "s3://bucket/y", "sha256": "def", "bytes": 9}])
    service.record_validation(analyst, v2, None, {"records": 9, "rejected": 0})
    service.record_calculation(None, v2, run="run-3", input_hash="h3")
    assert queue.corrections(analyst) == []  # priced: it waits in confirmations now
    assert [i.what for i in queue.confirmations(analyst)] == [f"Claims: {past_due.submission.plain_name.rsplit(', due', 1)[0]}, version 2"]


def test_the_approvers_card_and_the_settings_page(signed_in, analyst, past_due, claims_approver):
    service.request_reopen(analyst, past_due.submission, "double counted")
    page = signed_in(claims_approver).get("/filings/reopen-requests/").content.decode()
    assert "Version 1, the one that stands" in page and "No earlier corrections" in page and "Comments on this filing: 0" in page
    assert "a reason is needed to decline" in page
    c = signed_in(analyst)
    page = c.get("/notifications/settings/").content.decode()
    assert 'id="requests-h">Requests</h2>' in page and 'name="own_requests"' in page
    r = c.post("/notifications/settings/", {"own_requests": "in_app", "requests_to_decide": "off"})
    assert r.status_code == 302
    pref = Preference.objects.get(user=analyst)
    assert (pref.own_requests, pref.requests_to_decide) == ("in_app", "off")
