# ruff: noqa: F811 - the fixtures are imported from test_submission_comments
"""A filing organization's comment goes straight to the module's CPUC reviewers (UAT 2, U15; Jake 2026-11-01): the approvers and their
delegates for today are sent a notification, once each, whether or not they follow the PA; a CPUC comment sends none of these."""

from datetime import date, timedelta

import pytest
from django.template import Context, Template
from django.test import RequestFactory

from cedars.services.accounts import service as access
from cedars.services.notifications import service as notifications
from cedars.services.notifications.models import Notification, Preference
from cedars.services.submissions import comments
from cedars.services.submissions import service as submissions

from .test_submission_comments import _pdf, _priced, analyst, approver, deadline  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


@pytest.fixture
def cpuc_people(make_user, cpuc_admin, cpuc, approver):
    reader = make_user("reader@cpuc.example.org", "Rea Der")
    access.grant(cpuc_admin, reader, cpuc, "claims.read")  # CPUC staff, but no approver: hears only by subscribing
    budgets = make_user("budgets@cpuc.example.org", "Bud Gets")
    access.grant(cpuc_admin, budgets, cpuc, "budgets.approve")  # another module's approver
    delegate = make_user("delegate@cpuc.example.org", "Del Egate")
    submissions.delegate(approver, module="claims", delegate_to=delegate, starts=date.today(), ends=date.today() + timedelta(days=5))
    former = make_user("former@cpuc.example.org", "For Mer")
    submissions.delegate(
        approver, module="claims", delegate_to=former, starts=date.today() - timedelta(days=9), ends=date.today() - timedelta(days=1)
    )
    return {"reader": reader, "budgets": budgets, "delegate": delegate, "former": former}


def test_a_pas_comment_reaches_the_approvers_and_their_delegates(
    analyst, approver, cpuc_people, deadline, pge, django_capture_on_commit_callbacks
):
    v = _priced(analyst, deadline, pge)
    with django_capture_on_commit_callbacks(execute=True):
        c = comments.post(analyst, v, "Line 12 is a late invoice; the support is attached.", [_pdf()])
    told = {n.user for n in Notification.objects.filter(key=comments.notice_key(c.pk))}
    assert told == {approver, cpuc_people["delegate"]}
    n = Notification.objects.get(user=approver)
    assert n.title == f"Comment on {v} from PGE"
    assert n.body == "Line 12 is a late invoice; the support is attached.\nAttached: memo.pdf"
    assert n.link == f"/claims/versions/{v.pk}/#comment-{c.pk}" and n.email


def test_a_cpuc_comment_sends_no_direct_message(analyst, approver, cpuc_people, deadline, pge, django_capture_on_commit_callbacks):
    v = _priced(analyst, deadline, pge)
    with django_capture_on_commit_callbacks(execute=True):
        comments.post(cpuc_people["delegate"], v, "Which invoice?")
    assert not Notification.objects.exists()


def test_a_reviewer_who_subscribes_is_told_once_by_their_subscription(analyst, approver, deadline, pge, django_capture_on_commit_callbacks):
    notifications.subscribe(approver, "submission.commented", "in_app")
    Preference.objects.update_or_create(user=approver, defaults={"email_direct": True})
    v = _priced(analyst, deadline, pge)
    with django_capture_on_commit_callbacks(execute=True):
        comments.post(analyst, v, "Line 12 is a late invoice.")
    (n,) = Notification.objects.filter(user=approver)
    assert not n.email and n.event is not None  # the subscription's message, by the channel it chose


def test_direct_emails_follow_the_reviewers_preference(analyst, approver, deadline, pge, django_capture_on_commit_callbacks):
    Preference.objects.update_or_create(user=approver, defaults={"email_direct": False})
    v = _priced(analyst, deadline, pge)
    with django_capture_on_commit_callbacks(execute=True):
        comments.post(analyst, v, "Line 12 is a late invoice.")
    (n,) = Notification.objects.filter(user=approver)
    assert not n.email


def test_the_form_tells_the_filer_who_is_sent_their_comment(analyst, approver, deadline, pge):
    v = _priced(analyst, deadline, pge)
    request = RequestFactory().get("/")
    template = Template("{% load filing_comments %}{% filing_comments v %}")
    note = "CPUC staff who approve claims filings are sent it as a notification."
    request.user = analyst
    assert note in template.render(Context({"request": request, "v": v, "csrf_token": "t"}))
    request.user = approver
    assert note not in template.render(Context({"request": request, "v": v, "csrf_token": "t"}))
