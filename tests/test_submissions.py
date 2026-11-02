from datetime import date, timedelta

import pytest
from django.utils import timezone

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from cedars.services.submissions import service
from cedars.services.submissions.models import ReopenRequest, SubmissionVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def analyst(make_user, pge, pge_admin):
    u = make_user("analyst@pge.example.org")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


@pytest.fixture
def deadline(cpuc_admin):
    period = [p for p in registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]
    return service.add_deadline(
        cpuc_admin, module="claims", kind="quarterly claim", period=period, due_at=timezone.now() + timedelta(days=10)
    )


def _priced(analyst, deadline, pge, rows_hash="h1"):
    v = service.start(analyst, deadline, pge)
    service.attach_files(analyst, v, [{"name": "Measure.csv", "key": "s3://bucket/x", "sha256": "abc", "bytes": 10}])
    service.record_validation(analyst, v, None, {"records": 10, "rejected": 0, "warnings": 2})
    service.record_calculation(None, v, run="run-1", input_hash=rows_hash)
    return SubmissionVersion.objects.get(pk=v.pk)


def test_the_happy_path_publishes_on_confirm(analyst, deadline, pge):
    published = []
    service.register_publisher("claims", lambda v: published.append(v.pk), lambda v: published.remove(v.pk))
    v = _priced(analyst, deadline, pge)
    v = service.confirm(analyst, v, current_input_hash="h1")
    assert v.status == "published" and v.confirmed_by == analyst and not v.late
    assert published == [v.pk]
    assert service.current(v.submission) == v
    actions = list(
        AuditEvent.objects.filter(object_type="submissions.submissionversion", object_id=str(v.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )
    assert actions == [
        "submission.started",
        "submission.files_attached",
        "submission.validated",
        "submission.priced",
        "submission.confirmed",
        "submission.published",
    ]


def test_confirm_needs_a_run_on_the_current_rows(analyst, deadline, pge):
    v = _priced(analyst, deadline, pge, rows_hash="h1")
    with pytest.raises(AccessDenied, match="Price the current rows"):
        service.confirm(analyst, v, current_input_hash="h2")
    service.record_change(analyst, v, "corrected NumUnits on line 4")
    v.refresh_from_db()
    assert v.calculation == {} and v.status == "validated"
    with pytest.raises(AccessDenied, match="Price the current rows"):
        service.confirm(analyst, v, current_input_hash="h1")


def test_rejected_records_keep_it_a_draft(analyst, deadline, pge):
    v = service.start(analyst, deadline, pge)
    v = service.record_validation(analyst, v, None, {"records": 10, "rejected": 3, "warnings": 0})
    assert v.status == "draft"
    with pytest.raises(AccessDenied, match="Only a validated version is priced"):
        service.record_calculation(None, v, run="r", input_hash="h")


def test_unconfirm_before_the_due_date_keeps_nothing_superseded(analyst, deadline, pge):
    published = []
    service.register_publisher("claims", lambda v: published.append(v.pk), lambda v: published.remove(v.pk))
    v = service.confirm(analyst, _priced(analyst, deadline, pge), "h1")
    v = service.unconfirm(analyst, v, "found a typo")
    assert v.status == "validated" and published == []
    service.record_change(analyst, v, "fixed the typo")
    service.record_calculation(None, v, run="run-2", input_hash="h2")
    v = service.confirm(analyst, v, "h2")
    assert v.number == 1 and v.submission.versions.count() == 1  # the same version, re-confirmed; nothing kept from before


def test_after_the_due_date_a_correction_is_a_new_version_with_approval(analyst, deadline, pge, cpuc_admin, cpuc, make_user):
    v1 = service.confirm(analyst, _priced(analyst, deadline, pge), "h1")
    service.move_deadline(cpuc_admin, deadline, timezone.now() - timedelta(minutes=1), "test: the due date passes")
    with pytest.raises(AccessDenied, match="reopening"):
        service.unconfirm(analyst, v1)
    req = service.request_reopen(analyst, v1.submission, "evaluator found double-counted units")
    assert isinstance(req, ReopenRequest)
    with pytest.raises(AccessDenied, match="approver"):
        service.decide_reopen(analyst, req, True)
    approver = make_user("approver@cpuc.example.org")
    access.grant(cpuc_admin, approver, cpuc, "claims.approve")
    deputy = make_user("deputy@cpuc.example.org")
    service.delegate(approver, module="claims", delegate_to=deputy, starts=date.today(), ends=date.today() + timedelta(days=7))
    v2 = service.decide_reopen(deputy, req, True, "approved while the approver is away")
    req.refresh_from_db()
    assert req.decided_as_delegate_of == approver
    assert (v2.number, v2.status, v2.reason) == (2, "draft", "evaluator found double-counted units")
    assert v2.files == v1.files  # starts from the filed rows
    assert service.current(v1.submission) == v1  # version 1 still stands until version 2 is confirmed
    v2 = service.attach_files(analyst, v2, [{"name": "Measure.csv", "key": "s3://bucket/y", "sha256": "def", "bytes": 9}])
    service.record_validation(analyst, v2, None, {"records": 9, "rejected": 0})
    service.record_calculation(None, v2, run="run-3", input_hash="h3")
    v2 = service.confirm(analyst, v2, "h3")
    assert service.current(v1.submission) == v2 and SubmissionVersion.objects.get(pk=v1.pk).status == "published"


def test_reopen_policies(analyst, deadline, pge, cpuc_admin):
    v1 = service.confirm(analyst, _priced(analyst, deadline, pge), "h1")
    service.move_deadline(cpuc_admin, deadline, timezone.now() - timedelta(minutes=1), "passes")
    service.set_policy(cpuc_admin, "claims", reopen_after_due="closed")
    with pytest.raises(AccessDenied, match="no corrections after close-out"):
        service.request_reopen(analyst, v1.submission, "r")
    service.set_policy(cpuc_admin, "claims", reopen_after_due="open")
    v2 = service.request_reopen(analyst, v1.submission, "r")
    assert isinstance(v2, SubmissionVersion) and v2.number == 2


def test_late_first_filings(analyst, deadline, pge, cpuc_admin):
    v = _priced(analyst, deadline, pge)
    service.move_deadline(cpuc_admin, deadline, timezone.now() - timedelta(minutes=1), "passes")
    service.set_policy(cpuc_admin, "claims", late_confirmation=False)
    with pytest.raises(AccessDenied, match="late filings"):
        service.confirm(analyst, v, "h1")
    service.set_policy(cpuc_admin, "claims", late_confirmation=True)
    assert service.confirm(analyst, v, "h1").late


def test_permissions_are_per_module_and_organisation(analyst, deadline, pge, make_user, pge_admin, cpuc_admin):
    reader = make_user("reader@pge.example.org")
    access.grant(pge_admin, reader, pge, "claims.read")
    with pytest.raises(AccessDenied, match="claims.submit in PGE"):
        service.start(reader, deadline, pge)
    v = _priced(analyst, deadline, pge)
    submitter = make_user("submitter@pge.example.org")
    access.grant(pge_admin, submitter, pge, "claims.submit")
    with pytest.raises(AccessDenied, match="claims.confirm"):
        service.confirm(submitter, v, "h1")
    sce = access.create_organisation(cpuc_admin, code="SCE", name="SCE", kind="pa")
    with pytest.raises(AccessDenied, match="in SCE"):
        service.start(analyst, deadline, sce)


def test_only_the_cpuc_sets_the_calendar(pge_admin):
    with pytest.raises(AccessDenied, match="filing calendar"):
        service.add_deadline(pge_admin, module="claims", kind="x", cycle=registry.ensure_cycle(2028), due_at=timezone.now())


def test_calendar_and_reopen_pages(signed_in, analyst, deadline, pge, cpuc_admin, cpuc):
    c = signed_in(cpuc_admin)
    page = c.get("/filings/calendar/").content.decode()
    assert "quarterly claim" in page and "Add a deadline" in page
    # a deadline added by hand has no module page behind it, so it is not a link; a claim year's periods are
    assert '<th scope="row">quarterly claim</th>' in page and f'href="/claims/periods/{deadline.pk}/"' not in page
    from cedars.modules.claims import service as claims

    claims.add_year(cpuc_admin, year=2029, acc_version="2026", due={k: timezone.now() + timedelta(days=40) for k, _ in claims.PERIODS})
    q1 = claims.periods(2029)[0]
    page = c.get("/filings/calendar/").content.decode()
    assert f'<a href="/claims/periods/{q1.pk}/">Q1 claim</a>' in page  # each row links to the filing's page
    assert c.get(f"/claims/periods/{q1.pk}/").status_code == 200
    assert 'class="due"><time datetime="' in page and "Due in 1" in page  # the date with its urgency in words
    r = c.post(
        "/filings/calendar/?range=all", {"module": "monthly", "kind": "monthly report", "year": 2028, "period": "m1", "due": "2028-02-15"}
    )
    assert r.url == "/filings/calendar/?range=all"  # back to the view the deadline was added from
    assert '<th scope="row">monthly report</th>' in c.get(r.url).content.decode()
    v1 = service.confirm(analyst, _priced(analyst, deadline, pge), "h1")
    service.move_deadline(cpuc_admin, deadline, timezone.now() - timedelta(minutes=1), "passes")
    req = service.request_reopen(analyst, v1.submission, "double counted")
    approver_page = c.get("/filings/reopen-requests/").content.decode()
    assert "double counted" not in approver_page  # the CPUC admin is not a claims approver
    from cedars.services.accounts.models import User

    approver = User.objects.create_user(email="approver@cpuc.example.org", password="x")
    access.grant(cpuc_admin, approver, cpuc, "claims.approve")
    a = signed_in(approver)
    assert "double counted" in a.get("/filings/reopen-requests/").content.decode()
    a.post(f"/filings/reopen-requests/{req.pk}", {"decision": "approve"})
    assert v1.submission.versions.count() == 2


def test_the_filing_calendar_is_public(client, deadline):
    html = client.get("/filings/calendar/").content.decode()
    assert deadline.kind in html and 'name="module"' not in html  # anyone reads it; only a CPUC system administrator sees the add form


def test_calendar_filters_from_the_query_string():
    from django.http import QueryDict

    from cedars.services.submissions.views import _add_months, calendar_filters

    today = date(2026, 11, 30)
    f = calendar_filters(QueryDict(""), today)
    assert (f["types"], f["range"], f["start"], f["end"]) == ([], "window", date(2026, 8, 30), date(2027, 8, 30))
    assert _add_months(date(2026, 5, 31), -3) == date(2026, 2, 28)  # a month without the day ends on its last day
    f = calendar_filters(QueryDict("type=monthly&type=claims&type=nonsense&range=year"), today)
    assert f["types"] == ["claims", "monthly"] and (f["start"], f["end"]) == (date(2026, 1, 1), date(2026, 12, 31))
    assert calendar_filters(QueryDict("range=all"), today)["start"] is None
    assert calendar_filters(QueryDict("range=bogus"), today)["range"] == "window"
    f = calendar_filters(QueryDict("range=custom&from=2027-03-01&to=2026-01-01"), today)
    assert (f["range"], f["start"], f["end"]) == ("custom", date(2026, 1, 1), date(2027, 3, 1))  # swapped into order
    f = calendar_filters(QueryDict("range=custom&from=2027-03-01"), today)
    assert (f["start"], f["end"], f["to"]) == (date(2027, 3, 1), None, "")  # an open end
    f = calendar_filters(QueryDict("range=custom&from=March"), today)
    assert f["range"] == "window" and "YYYY-MM-DD" in f["error"]
    assert calendar_filters(QueryDict("range=custom"), today)["range"] == "all"


def test_calendar_filters_by_type_and_due_date(client):
    from cedars.services.submissions.models import Deadline

    def add(module, kind, days):
        period = [p for p in registry.ensure_periods(2027) if p.kind == "year"][0]
        Deadline.objects.create(module=module, kind=kind, period=period, due_at=timezone.now() + timedelta(days=days))

    add("claims", "soon claim", 30)
    add("budgets", "soon budget", 60)
    add("monthly", "old report", -200)
    add("claims", "far claim", 400)

    def rows(url):
        html = client.get(url).content.decode()
        return {k for k in ("soon claim", "soon budget", "old report", "far claim") if f">{k}<" in html}, html

    shown, html = rows("/filings/calendar/")  # the default: the last 3 and next 9 months, every type
    assert shown == {"soon claim", "soon budget"}
    assert "Showing 2 deadlines for every filing type, due " in html
    assert '<a href="/filings/calendar/?type=claims">Claims</a>' in html  # each chip is a link to the view with that type added
    assert 'aria-current="true"><span aria-hidden="true">&#10003; </span>Last 3 and next 9 months</a>' in html
    shown, html = rows("/filings/calendar/?type=claims")
    assert shown == {"soon claim"} and "for Claims," in html
    assert (
        '<a href="/filings/calendar/" aria-current="true"><span aria-hidden="true">&#10003; </span>Claims</a>' in html
    )  # select again: away
    assert '<a href="/filings/calendar/?type=budgets&amp;type=claims">Budgets</a>' in html
    assert '<input type="hidden" name="type" value="claims">' in html  # the custom range keeps the chosen types
    assert rows("/filings/calendar/?type=claims&range=all")[0] == {"soon claim", "far claim"}
    assert rows("/filings/calendar/?range=all")[0] == {"soon claim", "soon budget", "old report", "far claim"}
    start = (timezone.localdate() - timedelta(days=250)).isoformat()
    end = (timezone.localdate() - timedelta(days=100)).isoformat()
    shown, html = rows(f"/filings/calendar/?range=custom&from={start}&to={end}")
    assert shown == {"old report"} and f'value="{start}"' in html
    shown, html = rows("/filings/calendar/?type=monthly")
    assert shown == set() and "No deadlines match these filters." in html


# --- changing, retiring and restoring deadlines (CPUC system administrators)


def _hand_added(cpuc_admin, days=30):
    period = [p for p in registry.ensure_periods(2028) if p.kind == "month" and p.number == 3][0]
    return service.add_deadline(
        cpuc_admin, module="monthly", kind="monthly report", period=period, due_at=timezone.now() + timedelta(days=days)
    )


def test_a_deadline_changes_with_a_reason_and_each_change_is_audited(cpuc_admin, pge_admin, deadline):
    with pytest.raises(AccessDenied, match="filing calendar"):
        service.change_deadline(pge_admin, deadline, due_at=deadline.due_at, notes="x", reason="r")
    with pytest.raises(AccessDenied, match="reason"):
        service.change_deadline(cpuc_admin, deadline, due_at=deadline.due_at, notes="x", reason=" ")
    later = deadline.due_at + timedelta(days=7)
    with pytest.raises(AccessDenied, match="opens before"):
        service.change_deadline(cpuc_admin, deadline, due_at=later, opens_at=later, reason="r")
    service.change_deadline(cpuc_admin, deadline, due_at=later, reason="Decision moved the date")
    e = AuditEvent.objects.filter(action="deadline.moved").latest("id")
    assert e.reason == "Decision moved the date" and set(e.after) == {"due_at"}
    service.change_deadline(cpuc_admin, deadline, due_at=later, notes="File through the API", reason="Guidance")
    e = AuditEvent.objects.filter(action="deadline.changed").latest("id")
    assert e.before == {"notes": ""} and e.after == {"notes": "File through the API"}
    n = AuditEvent.objects.count()
    service.change_deadline(cpuc_admin, deadline, due_at=later, notes="File through the API", reason="no change")
    assert AuditEvent.objects.count() == n  # nothing changed, nothing recorded


def test_a_deadline_is_retired_only_when_no_filing_started_and_no_module_set_it_up(cpuc_admin, analyst, deadline, pge):
    service.start(analyst, deadline, pge)
    with pytest.raises(AccessDenied, match="1 organization has started"):
        service.retire_deadline(cpuc_admin, deadline, "mistake")
    from cedars.modules.claims import service as claims

    claims.add_year(cpuc_admin, year=2030, acc_version="2026", due={k: timezone.now() + timedelta(days=40) for k, _ in claims.PERIODS})
    with pytest.raises(AccessDenied, match="module's set-up"):
        service.retire_deadline(cpuc_admin, claims.periods(2030)[0], "mistake")
    d = _hand_added(cpuc_admin)
    with pytest.raises(AccessDenied, match="reason"):
        service.retire_deadline(cpuc_admin, d, "")
    service.retire_deadline(cpuc_admin, d, "Added for the wrong month")
    d.refresh_from_db()
    assert d.retired_at and d.retired_reason == "Added for the wrong month"
    from cedars.services.submissions.models import Deadline

    assert d not in Deadline.objects.active()
    with pytest.raises(AccessDenied, match="taken off"):
        service.start(analyst, d, pge)
    with pytest.raises(AccessDenied, match="retired"):
        service.change_deadline(cpuc_admin, d, due_at=d.due_at, reason="r")
    with pytest.raises(AccessDenied, match="restore it"):
        _hand_added(cpuc_admin)  # the same deadline again: restore it instead
    service.restore_deadline(cpuc_admin, d, "It was right after all")
    d.refresh_from_db()
    assert d.retired_at is None and d in Deadline.objects.active()
    assert [e.action for e in AuditEvent.objects.filter(object_id=str(d.pk), object_type="submissions.deadline").order_by("id")] == [
        "deadline.created",
        "deadline.retired",
        "deadline.restored",
    ]


def test_the_deadline_page(client, signed_in, cpuc_admin, pge_admin, deadline):
    url = f"/filings/calendar/{deadline.pk}/"
    page = client.get(url).content.decode()  # public: the deadline and its history, no forms
    assert "Added to the calendar" in page and 'id="change-h"' not in page and 'name="reason"' not in page
    assert client.post(url, {"action": "change", "due": "2028-05-15", "reason": "x"}).status_code == 404
    signed_in(pge_admin)
    assert client.post(url, {"action": "retire", "reason": "x"}).status_code == 404
    c = signed_in(cpuc_admin)
    page = c.get(url).content.decode()
    assert 'id="change-h"' in page and 'id="retire-h"' in page
    due_time = timezone.localtime(deadline.due_at).time()
    r = c.post(url, {"action": "change", "due": "2028-05-15", "opens": "2028-04-01", "notes": "Bring the summary", "reason": "D.26-01-001"})
    assert r.status_code == 302 and r.url == url
    deadline.refresh_from_db()
    due = timezone.localtime(deadline.due_at)
    assert due.date() == date(2028, 5, 15) and due.time() == due_time  # the time of day is kept
    assert timezone.localtime(deadline.opens_at).date() == date(2028, 4, 1) and deadline.notes == "Bring the summary"
    page = c.get(url).content.decode()
    assert "Due date moved" in page and "D.26-01-001" in page and "to 2028-05-15" in page and "Opens: none to 2028-04-01" in page
    assert "Reason for the change" in c.post(url, {"action": "change", "due": "2028-05-16", "reason": ""}).content.decode()
    deadline.refresh_from_db()
    assert timezone.localtime(deadline.due_at).date() == date(2028, 5, 15)  # no reason, no change
    r = c.post(url, {"action": "retire", "reason": "Wrong period"})
    assert r.status_code == 302 and 'id="restore-h"' in c.get(url).content.decode()
    cal = c.get("/filings/calendar/?range=all").content.decode()
    assert f'href="{url}"' not in cal and "Show retired deadlines (1)" in cal
    cal = c.get("/filings/calendar/?range=all&retired=1").content.decode()
    assert f'href="{url}"' in cal and "Retired</strong>" in cal and "Hide retired deadlines" in cal


def test_the_public_calendar_hides_retired_deadlines(client, cpuc_admin, deadline):
    d = _hand_added(cpuc_admin)
    service.retire_deadline(cpuc_admin, d, "mistake")
    page = client.get("/filings/calendar/?range=all&retired=1").content.decode()
    assert f'href="/filings/calendar/{d.pk}/"' not in page and f'href="/filings/calendar/{deadline.pk}/"' in page
    assert 'id="retired-toggle"' not in page


def test_reminders_skip_retired_deadlines_and_follow_a_moved_one(cpuc_admin, make_user, deadline):
    from cedars.services.notifications import service as notifications
    from cedars.services.notifications.models import Notification

    u = make_user("follower@example.org")
    notifications.set_reminders(u, [(30, "", "in_app")])
    d = _hand_added(cpuc_admin, days=20)
    service.retire_deadline(cpuc_admin, d, "mistake")
    notifications.send_deadline_reminders()
    titles = list(Notification.objects.filter(user=u).values_list("title", flat=True))
    assert any("quarterly claim" in t for t in titles) and not any("monthly report" in t for t in titles)
