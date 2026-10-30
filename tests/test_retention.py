from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications import service as notifications
from cedars.services.notifications.models import Notification
from cedars.services.registry import service as registry
from cedars.services.retention import service
from cedars.services.retention.models import RetentionPolicy
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Submission, SubmissionVersion

pytestmark = pytest.mark.django_db


def _age(qs, days):
    qs.update(created_at=timezone.now() - timedelta(days=days))


def test_decision_m_defaults():
    p = {r.record_class: r for r in RetentionPolicy.objects.all()}
    assert p["filing_version"].period == "Permanent" and p["audit_event"].period == "Permanent"
    assert p["scratch_run"].period == "30 days" and p["unsubmitted_draft"].period == "30 days"
    assert p["notification"].period == "1 year" and p["superseded_version"].period == "Not kept"


def test_old_notifications_are_purged_and_holds_respected(pge_admin, cpuc_admin):
    old = notifications.notify(pge_admin, "old")
    notifications.notify(pge_admin, "new")
    _age(Notification.objects.filter(pk=old.pk), 400)
    dry = service.purge(dry_run=True)
    assert dry.counts["notification"] == {"deleted": 1, "held": 0, "failed": 0} and Notification.objects.count() == 2
    hold = service.place_hold(cpuc_admin, "notification", "PRA request 2026-114", f"notification:{old.pk}")
    assert service.purge().counts["notification"]["held"] == 1 and Notification.objects.count() == 2
    service.release_hold(cpuc_admin, hold, "request closed")
    service.place_hold(cpuc_admin, "notification", "litigation hold on everything")  # whole class
    assert service.purge().counts["notification"]["held"] == 1
    for h in service.LegalHold.objects.filter(released_at__isnull=True):
        service.release_hold(cpuc_admin, h, "lifted")
    service.purge()
    assert list(Notification.objects.values_list("title", flat=True)) == ["new"]
    assert AuditEvent.objects.filter(action="retention.purged").get().after["deleted"] == 1


def test_idle_unconfirmed_drafts_expire_and_confirmed_ones_stay(cpuc_admin, pge, pge_admin, make_user):
    analyst = make_user("a@pge.com")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, analyst, pge, p)
    quarters = [p for p in registry.ensure_periods(2028) if p.kind == "quarter"]
    due = timezone.now() + timedelta(days=60)
    idle_draft = submissions.start(
        analyst, submissions.add_deadline(cpuc_admin, module="claims", kind="claim", period=quarters[0], due_at=due), pge
    )
    idle_validated = submissions.start(
        analyst, submissions.add_deadline(cpuc_admin, module="claims", kind="claim", period=quarters[1], due_at=due), pge
    )
    submissions.record_validation(analyst, idle_validated, None, {})
    done = submissions.start(
        analyst, submissions.add_deadline(cpuc_admin, module="claims", kind="claim", period=quarters[2], due_at=due), pge
    )
    submissions.record_validation(analyst, done, None, {})
    submissions.record_calculation(None, done, run="r", input_hash="h")
    submissions.confirm(analyst, done, "h")
    run = service.purge(now=timezone.now() + timedelta(days=31))  # 31 days from now, none has changed since today
    assert run.counts["unsubmitted_draft"]["deleted"] == 2
    assert list(SubmissionVersion.objects.values_list("pk", flat=True)) == [done.pk]
    assert not Submission.objects.filter(pk__in=[idle_draft.submission_id, idle_validated.submission_id]).exists()
    assert AuditEvent.objects.filter(action="submission.draft_expired").count() == 2


def test_a_draft_worked_on_recently_is_kept(cpuc_admin, pge, pge_admin, make_user):
    analyst = make_user("a@pge.com")
    access.grant(pge_admin, analyst, pge, "claims.submit")
    q = [p for p in registry.ensure_periods(2028) if p.kind == "quarter"][0]
    d = submissions.add_deadline(cpuc_admin, module="claims", kind="quarterly claim", period=q, due_at=timezone.now() + timedelta(days=60))
    v = submissions.start(analyst, d, pge)
    _age(SubmissionVersion.objects.all(), 40)  # created long ago, but its last change is today
    assert service.purge().counts["unsubmitted_draft"]["deleted"] == 0
    assert SubmissionVersion.objects.filter(pk=v.pk).exists()


def test_only_sysadmins_and_with_reasons(pge_admin, cpuc_admin):
    policy = RetentionPolicy.objects.get(record_class="scratch_run")
    with pytest.raises(AccessDenied):
        service.set_period(pge_admin, policy, 60, "x")
    with pytest.raises(AccessDenied, match="reason"):
        service.set_period(cpuc_admin, policy, 60, "")
    service.set_period(cpuc_admin, policy, 60, "records schedule 2027")
    assert RetentionPolicy.objects.get(pk=policy.pk).keep_days == 60
    with pytest.raises(AccessDenied, match="not a record class"):
        service.place_hold(cpuc_admin, "nonsense", "r")


def test_page_and_command(signed_in, cpuc_admin, pge_admin):
    assert signed_in(pge_admin).get("/admin/retention/").status_code == 403
    c = signed_in(cpuc_admin)
    page = c.get("/admin/retention/").content.decode()
    assert "Scratch calculation runs" in page and "No legal holds." in page
    policy = RetentionPolicy.objects.get(record_class="notification")
    c.post("/admin/retention/", {"action": "period", "policy": policy.pk, "keep_days": "730", "reason": "two years"})
    assert RetentionPolicy.objects.get(pk=policy.pk).period == "2 years"
    c.post("/admin/retention/", {"action": "hold", "record_class": "notification", "object_ref": "", "reason": "matter 12"})
    assert "matter 12" in c.get("/admin/retention/").content.decode()
    call_command("purge_expired", "--dry-run")
