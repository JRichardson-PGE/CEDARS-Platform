from datetime import date, timedelta

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from cedars.services.accounts import service as access
from cedars.services.notifications import service
from cedars.services.notifications.models import Notification, Subscription
from cedars.services.refdata import service as refdata
from cedars.services.registry import service as registry
from cedars.services.submissions import service as submissions

pytestmark = pytest.mark.django_db(transaction=True)  # delivery happens on commit

MANIFEST = {"a.parquet": {"sha256": "ab" * 32, "bytes": 10}}


@pytest.fixture
def quarter():
    return [p for p in registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]


def _filed(cpuc_admin, pge, pge_admin, make_user, quarter, due_in=timedelta(days=10)):
    analyst = make_user("analyst@pge.example.org")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, analyst, pge, p)
    d = submissions.add_deadline(cpuc_admin, module="claims", kind="quarterly claim", period=quarter, due_at=timezone.now() + due_in)
    v = submissions.start(analyst, d, pge)
    submissions.record_validation(analyst, v, None, {"records": 1})
    submissions.record_calculation(None, v, run="r1", input_hash="h")
    return analyst, submissions.confirm(analyst, v, "h")


def test_public_events_reach_subscribers_by_page_and_email(cpuc_admin, pge_admin, make_user):
    service.subscribe(pge_admin, "pack.published")
    quiet = make_user("quiet@pge.example.org")
    service.subscribe(quiet, "pack.published", Subscription.Channel.IN_APP)
    pack = refdata.register_pack(cpuc_admin, kind="value_lists", label="Value lists", version="2026-10", manifest=MANIFEST, storage_uri="x")
    assert Notification.objects.count() == 0  # registering is not publishing
    refdata.publish_pack(cpuc_admin, pack)
    n = Notification.objects.get(user=pge_admin)
    assert n.title.startswith("Reference data is published") and "Value lists 2026-10" in n.title
    assert n.link == f"/reference-data/{pack.pk}/" and n.emailed_at is not None
    assert [m.to for m in mail.outbox] == [[pge_admin.email]]
    assert f"/reference-data/{pack.pk}/" in mail.outbox[0].body
    q = Notification.objects.get(user=quiet)
    assert not q.email and q.emailed_at is None


def test_publications_reach_every_subscriber_and_work_in_progress_only_the_organisation(
    cpuc_admin, cpuc, pge, pge_admin, make_user, quarter
):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="SCE", kind="pa", pa_type="IOU")
    sce_admin = make_user("admin@sce.example.org")
    access.grant(cpuc_admin, sce_admin, sce, "org.admin")
    reviewer = make_user("reviewer@cpuc.example.org")
    access.grant(cpuc_admin, reviewer, cpuc, "claims.read")
    for u in (pge_admin, sce_admin, reviewer):
        service.subscribe(u, "submission.published", module="claims")
        service.subscribe(u, "submission.reopened")
    service.subscribe(sce_admin, "reopen.requested")  # CPUC staff only
    _, v = _filed(cpuc_admin, pge, pge_admin, make_user, quarter, due_in=timedelta(minutes=1))
    got = set(Notification.objects.values_list("user__email", flat=True))
    assert got == {pge_admin.email, sce_admin.email, reviewer.email}  # a publication is public
    Notification.objects.all().delete()
    submissions.move_deadline(cpuc_admin, v.submission.deadline, timezone.now() - timedelta(seconds=1), "close-out")
    submissions.request_reopen(v.confirmed_by, v.submission, "double counted")
    assert not Notification.objects.filter(user=sce_admin).exists()


def test_module_filter(cpuc_admin, pge_admin, quarter):
    service.subscribe(pge_admin, "deadline.created", module="budgets")
    submissions.add_deadline(cpuc_admin, module="claims", kind="quarterly claim", period=quarter, due_at=timezone.now())
    assert not Notification.objects.exists()
    submissions.add_deadline(cpuc_admin, module="budgets", kind="TUAL", period=quarter, due_at=timezone.now())
    assert Notification.objects.get().link == "/filings/calendar/"


def test_a_rolled_back_change_notifies_nobody(cpuc_admin, pge_admin):
    from django.db import transaction

    service.subscribe(pge_admin, "pack.published")
    pack = refdata.register_pack(cpuc_admin, kind="value_lists", label="L", manifest=MANIFEST, storage_uri="x")
    with pytest.raises(RuntimeError), transaction.atomic():
        refdata.publish_pack(cpuc_admin, pack)
        raise RuntimeError("something later failed")
    assert not Notification.objects.exists()


def test_a_failed_email_keeps_the_notification(cpuc_admin, pge_admin, settings):
    settings.EMAIL_BACKEND = "tests.test_notifications.FailingBackend"
    service.subscribe(pge_admin, "pack.published")
    refdata.publish_pack(cpuc_admin, refdata.register_pack(cpuc_admin, kind="value_lists", label="L", manifest=MANIFEST, storage_uri="x"))
    n = Notification.objects.get()
    assert n.emailed_at is None and "relay refused" in n.email_error


class FailingBackend:
    def __init__(self, *a, **k):
        pass

    def send_messages(self, messages):
        raise ConnectionError("relay refused")


def test_direct_messages_follow_the_users_preference(pge_admin, signed_in):
    service.notify(pge_admin, "Your run finished", "Portfolio TRC 1.21", "/runs/5/")
    assert len(mail.outbox) == 1
    c = signed_in(pge_admin)
    c.post("/notifications/settings/", {"pack.published": "in_app"})  # email_direct unticked
    service.notify(pge_admin, "Your run failed")
    assert len(mail.outbox) == 1
    assert Subscription.objects.get(user=pge_admin).channel == "in_app"


def test_deadline_reminders_are_sent_once(cpuc_admin, pge_admin, quarter):
    service.set_reminders(pge_admin, [(7, "", "email"), (1, "", "email")])
    today = date(2028, 3, 1)
    due = timezone.make_aware(timezone.datetime(2028, 3, 8, 23, 59))
    submissions.add_deadline(cpuc_admin, module="claims", kind="quarterly claim", period=quarter, due_at=due)
    assert service.send_deadline_reminders(today) == 1
    assert service.send_deadline_reminders(today) == 0
    assert Notification.objects.get().title.startswith("Due in 7 days: Claims quarterly claim 2028Q1")
    assert service.send_deadline_reminders(date(2028, 3, 4)) == 0  # the 7-day reminder is sent already
    assert service.send_deadline_reminders(date(2028, 3, 7)) == 1
    assert Notification.objects.order_by("-id").first().title.startswith("Due tomorrow")
    call_command("send_deadline_reminders")


def test_pages(pge_admin, signed_in):
    service.notify(pge_admin, "Your run finished", link="/runs/5/")
    service.notify(pge_admin, "Second")
    c = signed_in(pge_admin)
    page = c.get("/notifications/").content.decode()
    assert "2 unread" in page and "Your run finished" in page
    assert 'Notifications <span class="count">2</span><span class="sr-only"> unread</span>' in page  # the header's unread count
    c.post("/notifications/")
    assert service.unread(pge_admin) == 0
    settings_page = c.get("/notifications/settings/").content.decode()
    assert "Reference data is published" in settings_page
    c.post("/notifications/settings/", {"pack.published": "email", "submission.published:budgets": "in_app", "email_direct": "on"})
    assert set(Subscription.objects.filter(user=pge_admin).values_list("event", "module", "channel")) == {
        ("pack.published", "", "email"),
        ("submission.published", "budgets", "in_app"),
    }
