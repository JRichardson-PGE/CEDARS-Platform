"""Limits a CPUC system administrator changes on Change limits: runs one account may have waiting, how long a sign-in lasts and may
sit unused, and the pre-upload checker's file size. Each has its setting as the first-run default."""

import io
import time

import pytest
from django.test import Client

from cedars.modules.calculations import service as calculations
from cedars.modules.calculations.models import Run
from cedars.services.accounts.middleware import SessionTimeoutMiddleware
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.releases import limits
from cedars.services.releases import service as releases
from cedars.services.releases.models import LimitChange
from cedars.services.releases.service import ReleaseError
from cedars.services.rules import precheck

from .test_calculations import analyst, job_zip, release, request  # noqa: F401 (fixtures)

# --- setting a limit


def test_a_limit_starts_at_its_setting_and_follows_what_an_administrator_sets(settings, cpuc_admin):
    settings.CALCULATIONS_MAX_QUEUED_PER_ACCOUNT = 5
    assert limits.value("queued_runs") == 5 and limits.current("queued_runs") == "5 runs"
    releases.set_limit(cpuc_admin, "queued_runs", 3, "the queue is busy this week")
    assert limits.value("queued_runs") == 3
    settings.CALCULATIONS_MAX_QUEUED_PER_ACCOUNT = 9  # the setting is only the default once a value is set
    assert limits.value("queued_runs") == 3
    releases.set_limit(cpuc_admin, "queued_runs", None, "back to normal")
    assert limits.value("queued_runs") == 9
    assert [c.value for c in LimitChange.objects.filter(key="queued_runs")] == [None, 3]  # newest first; rows are kept
    e = AuditEvent.objects.filter(action="limit.changed").order_by("pk").first()
    assert e.before == {"queued_runs": "5 runs"} and e.after["queued_runs"] == "3 runs" and e.reason == "the queue is busy this week"


def test_only_a_system_administrator_sets_a_limit_within_its_bounds_and_with_a_reason(cpuc_admin, pge_admin):
    with pytest.raises(AccessDenied):
        releases.set_limit(pge_admin, "queued_runs", 3, "mine")
    with pytest.raises(ReleaseError, match="from 1 to 100 runs"):
        releases.set_limit(cpuc_admin, "queued_runs", 0, "none")
    with pytest.raises(ReleaseError, match="reason"):
        releases.set_limit(cpuc_admin, "queued_runs", 3, " ")
    with pytest.raises(ReleaseError, match="not a limit that can be changed"):
        releases.set_limit(cpuc_admin, "upload_mb", 10, "not editable")
    assert not LimitChange.objects.exists()


def test_the_change_limits_page(signed_in, cpuc_admin, pge_admin, client):
    assert signed_in(pge_admin).get("/limits/manage/").status_code == 404
    c = signed_in(cpuc_admin)
    html = c.get("/limits/manage/").content.decode()
    for lim in limits.editable():
        assert f'id="{lim.key}"' in html
    r = c.post("/limits/manage/", {"key": "session_idle_minutes", "value": "30", "reason": "tighter for PII", "action": "set"}, follow=True)
    assert "now 30 minutes" in r.content.decode() and limits.value("session_idle_minutes") == 30
    r = c.post("/limits/manage/", {"key": "session_idle_minutes", "value": "2", "reason": "too short", "action": "set"}, follow=True)
    assert "from 5 to 1,440 minutes" in r.content.decode() and limits.value("session_idle_minutes") == 30
    assert "tighter for PII" in c.get("/limits/manage/").content.decode()
    public = Client().get("/limits/").content.decode()
    assert "30 minutes" in public and "Set by the CPUC" in public


# --- runs waiting per account


def test_an_account_may_have_only_so_many_runs_waiting(tmp_path, settings, analyst, pge, release, cpuc_admin):  # noqa: F811
    settings.CALCULATIONS_MAX_QUEUED_PER_ACCOUNT = 2
    first = calculations.create_run(analyst, pge, request(release, name="a"), job_zip(tmp_path))
    calculations.create_run(analyst, pge, request(release, name="b"), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="You have 2 runs waiting or running; one account may have at most 2"):
        calculations.create_run(analyst, pge, request(release, name="c"), job_zip(tmp_path))
    Run.objects.filter(pk=first.pk).update(status=Run.Status.SUCCEEDED)  # one finishes: there is room again
    calculations.create_run(analyst, pge, request(release, name="c"), job_zip(tmp_path))
    releases.set_limit(cpuc_admin, "queued_runs", 3, "more room")
    calculations.create_run(analyst, pge, request(release, name="d"), job_zip(tmp_path))
    assert calculations.waiting_runs(analyst) == 3


def test_a_filings_pricing_runs_are_never_counted_or_refused(tmp_path, settings, analyst, pge, pge_admin, cpuc_admin, release):  # noqa: F811
    from django.utils import timezone

    from cedars.services.accounts import service as access
    from cedars.services.registry import service as registry
    from cedars.services.submissions import service as submissions

    settings.CALCULATIONS_MAX_QUEUED_PER_ACCOUNT = 1
    access.grant(pge_admin, analyst, pge, "claims.submit")
    quarter = [p for p in registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]
    deadline = submissions.add_deadline(
        cpuc_admin, module="claims", kind="quarterly claim", period=quarter, due_at=timezone.now() + timezone.timedelta(days=10)
    )
    v = submissions.start(analyst, deadline, pge)
    submissions.record_validation(analyst, v, None, {"records": 3, "rejected": 0, "warnings": 0})
    calculations.create_run(analyst, pge, request(release), job_zip(tmp_path))  # the account's one
    filing = calculations.create_run(analyst, pge, request(release), job_zip(tmp_path), submission_version=v)
    assert filing.submission_version == v and calculations.waiting_runs(analyst) == 1


# --- sessions


def test_a_session_unused_for_longer_than_the_idle_limit_is_signed_out(signed_in, make_user, settings):
    settings.CEDARS_SESSION_IDLE_MINUTES = 60
    u = make_user("analyst@pge.example.org")
    c = signed_in(u)
    assert c.get("/account/").status_code == 200
    s = c.session
    s[SessionTimeoutMiddleware.SEEN] = time.time() - 61 * 60
    s.save()
    r = c.get("/account/", follow=True)
    assert "without using CEDARS" in r.content.decode()
    assert c.get("/account/").status_code == 302  # signed out: the account page sends you to sign in


def test_a_sign_in_ends_after_the_longest_it_may_last(signed_in, make_user, settings):
    settings.CEDARS_SESSION_MAX_HOURS = 12
    c = signed_in(make_user("analyst@pge.example.org"))
    c.get("/account/")
    s = c.session
    s[SessionTimeoutMiddleware.STARTED] = time.time() - 13 * 3600
    s[SessionTimeoutMiddleware.SEEN] = time.time()
    s.save()
    assert "sign-ins last at most 12 hours" in c.get("/account/", follow=True).content.decode()


def test_a_pages_background_checks_do_not_keep_a_session_alive(signed_in, make_user):
    c = signed_in(make_user("analyst@pge.example.org"))
    c.get("/account/")
    s = c.session
    old = time.time() - 30 * 60
    s[SessionTimeoutMiddleware.SEEN] = old
    s.save()
    c.get("/account/", HTTP_ACCEPT="application/json")  # a poll: checked, not counted as use
    assert c.session[SessionTimeoutMiddleware.SEEN] == old
    c.get("/account/", HTTP_ACCEPT="text/html")  # a page: counted
    assert c.session[SessionTimeoutMiddleware.SEEN] > old


def test_the_session_limits_follow_what_an_administrator_sets(signed_in, make_user, cpuc_admin):
    releases.set_limit(cpuc_admin, "session_idle_minutes", 10, "shorter")
    c = signed_in(make_user("analyst@pge.example.org"))
    c.get("/account/")
    s = c.session
    s[SessionTimeoutMiddleware.SEEN] = time.time() - 11 * 60
    s.save()
    assert "after 10 minutes without using CEDARS" in c.get("/account/", follow=True).content.decode()


# --- the pre-upload checker's file size


def test_the_checker_takes_smaller_files_than_an_upload(settings, cpuc_admin):
    settings.CEDARS_CHECKER_MAX_MB = 25
    assert precheck.too_large("Measure.csv", 25 * 1024 * 1024) == ""
    assert "the pre-upload checker reads files of up to 25 MB" in precheck.too_large("Measure.csv", 26 * 1024 * 1024)
    releases.set_limit(cpuc_admin, "checker_mb", 30, "bigger spot checks")
    assert precheck.too_large("Measure.csv", 26 * 1024 * 1024) == ""


def test_the_checker_page_and_api_refuse_a_file_over_the_limit(signed_in, make_user, settings):
    settings.CEDARS_CHECKER_MAX_MB = 1
    c = signed_in(make_user("analyst@pge.example.org"))
    big = io.BytesIO(b"CEInputID\n" + b"A\n" * 600_000)
    big.name = "Measure.csv"
    r = c.post("/api/v1/rules/check", {"file": big, "table": "Measure"})
    assert r.status_code == 400 and "the pre-upload checker reads files of up to 1 MB" in r.content.decode()
    assert c.get("/rules/check/").context["max_mb"] == 1  # the page says it beside the file field
