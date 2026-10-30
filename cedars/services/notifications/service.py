"""Notifications: subscriptions fed from the audit trail, reminders, and direct messages from modules (a run finished, access
changed). Every message lands on the user's notifications page; email is per subscription (or the user's preference for direct
messages). Delivery happens after the transaction that wrote the event commits, so a rolled-back change notifies nobody."""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from cedars.services.accounts import service as access

from .models import Notification, Preference, Subscription

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventType:
    code: str
    label: str
    scope: str  # "public": anyone; "organisation": members of the organisation it concerns, and CPUC staff; "cpuc": CPUC staff only
    group: str


EVENTS = [
    EventType("submission.published", "A filing is confirmed and published", "organisation", "Filings"),
    EventType("submission.unconfirmed", "A filing is un-confirmed before its due date", "organisation", "Filings"),
    EventType("submission.reopened", "A filing is reopened for a correction", "organisation", "Filings"),
    EventType("reopen.requested", "A correction is requested after a due date (for approvers)", "cpuc", "Filings"),
    EventType("reopen.declined", "A correction request is declined", "organisation", "Filings"),
    EventType("deadline.created", "A deadline is added to the filing calendar", "public", "Filing calendar"),
    EventType("deadline.moved", "A deadline moves", "public", "Filing calendar"),
    EventType("deadline.reminder", "A deadline is coming up (7 days and 1 day before)", "public", "Filing calendar"),
    EventType("pack.published", "Reference data is published (ACC, impact profiles, DEER, value lists ...)", "public", "Reference data"),
    EventType("pack.withdrawn", "Reference data is withdrawn", "public", "Reference data"),
    EventType("rule_set.published", "A validation rule set is published", "public", "Rules and dictionary"),
    EventType("dictionary.published", "A new version of the data dictionary is published", "public", "Rules and dictionary"),
    EventType("allocation_version.confirmed", "Statewide allocation factors are confirmed", "public", "Programs"),
    EventType("program_change.requested", "A change to a locked program attribute is requested (for approvers)", "cpuc", "Programs"),
    EventType("program_change.approved", "A program change request is approved", "organisation", "Programs"),
    EventType("program_change.declined", "A program change request is declined", "organisation", "Programs"),
    EventType("program_policy.changed", "The locked program attributes change", "public", "Programs"),
]
BY_CODE = {e.code: e for e in EVENTS}
REMINDER_DAYS = (7, 1)


# --- what an event concerns: its organisation, module and page


def _concerns(event) -> tuple[int | None, str, str]:
    """(organisation id, module, link) for an audit event, from the record it is about."""
    t, oid = event.object_type, event.object_id
    try:
        if t == "submissions.submissionversion":
            from cedars.services.submissions.models import SubmissionVersion

            v = SubmissionVersion.objects.select_related("submission__deadline").get(pk=oid)
            return v.submission.organisation_id, v.submission.deadline.module, ""
        if t == "submissions.reopenrequest":
            from cedars.services.submissions.models import ReopenRequest

            r = ReopenRequest.objects.select_related("submission__deadline").get(pk=oid)
            return r.submission.organisation_id, r.submission.deadline.module, "/filings/reopen-requests/"
        if t == "submissions.deadline":
            from cedars.services.submissions.models import Deadline

            return None, Deadline.objects.get(pk=oid).module, "/filings/calendar/"
        if t == "registry.programchangerequest":
            from cedars.services.registry.models import ProgramChangeRequest

            return ProgramChangeRequest.objects.get(pk=oid).organisation_id, "programs", f"/programs/requests/{oid}/"
        if t == "registry.programchangepolicy":
            return None, "programs", "/programs/locked/"
        if t == "refdata.packversion":
            return None, "", f"/reference-data/{oid}/"
        if t == "rules.ruleset":
            return None, "", f"/rules/{oid}/"
        if t == "dictionary.catalogversion":
            return None, "", "/dictionary/"
    except Exception:  # the record may be gone (a discarded draft); the event still says what happened
        log.debug("notification: %s %s not found", t, oid)
    return event.organisation_id, "", ""


def may_see(user, scope: str, organisation_id: int | None) -> bool:
    if scope == "public":
        return True
    mine = set(access.organisations_of(user).values_list("pk", flat=True))
    cpuc = access.cpuc()
    staff = cpuc is not None and cpuc.pk in mine
    if scope == "cpuc":
        return staff
    return staff or (organisation_id is not None and organisation_id in mine)


# --- delivery


def on_audit_event(sender, instance, created, **kwargs):
    if created and instance.action in BY_CODE:
        transaction.on_commit(lambda: deliver(instance))


def deliver(event) -> list[Notification]:
    """Fan one audit event out to its subscribers. Never raises: a failed delivery is logged, not the change that caused it."""
    try:
        kind = BY_CODE[event.action]
        org_id, module, link = _concerns(event)
        subs = Subscription.objects.filter(event=event.action).select_related("user")
        out = []
        for s in subs:
            if (s.module and module and s.module != module) or (s.organisation_id and s.organisation_id != org_id):
                continue
            if not s.user.is_active or not may_see(s.user, kind.scope, org_id):
                continue
            title = f"{kind.label}: {event.object_repr}" if event.object_repr else kind.label
            body = event.reason and f"Reason: {event.reason}" or ""
            out.append(_create(s.user, title, body, link, email=s.channel == Subscription.Channel.EMAIL, event=event))
        return [n for n in out if n]
    except Exception:
        log.exception("notification delivery failed for audit event %s", event.pk)
        return []


def _create(user, title, body="", link="", *, email=False, event=None, key="") -> Notification | None:
    try:
        with transaction.atomic():
            n = Notification.objects.create(user=user, title=title[:300], body=body, link=link, email=email, event=event, key=key)
    except IntegrityError:  # this key was sent already
        return None
    if email:
        _send(n)
    return n


def _send(n: Notification) -> None:
    link = f"{settings.CEDARS_BASE_URL.rstrip('/')}{n.link}" if n.link else ""
    text = "\n\n".join(
        x for x in [n.body, link, "Change what you are sent: " + settings.CEDARS_BASE_URL.rstrip("/") + "/notifications/settings/"] if x
    )
    try:
        prefix = "[SANDBOX] " if settings.ENVIRONMENT == "sandbox" else ""
        send_mail(f"{prefix}CEDARS: {n.title}"[:200], text, None, [n.user.email])
    except Exception as e:  # the message stays on the page; the error is shown to admins and the user
        n.email_error = str(e)[:1000]
        log.warning("notification email to user %s failed: %s", n.user_id, e)
    else:
        n.emailed_at = timezone.now()
    n.save(update_fields=["emailed_at", "email_error"])


def notify(user, title: str, body: str = "", link: str = "", *, key: str = "") -> Notification | None:
    """A message to one user from a module (e.g. Calculations when a run finishes or fails). Emailed unless the user turned that off."""
    pref = Preference.objects.filter(user=user).first()
    return _create(user, title, body, link, email=pref.email_direct if pref else True, key=key)


def send_deadline_reminders(today=None) -> int:
    """Remind subscribers of deadlines due in REMINDER_DAYS days. Safe to run more than once a day (each reminder is sent once)."""
    from cedars.services.submissions.models import Deadline

    today = today or timezone.localdate()
    sent = 0
    for days in REMINDER_DAYS:
        day = today + timedelta(days=days)
        for d in Deadline.objects.filter(due_at__date=day).select_related("period", "cycle"):
            for s in Subscription.objects.filter(event="deadline.reminder").select_related("user"):
                if (s.module and s.module != d.module) or not s.user.is_active:
                    continue
                n = _create(
                    s.user,
                    f"Due in {days} day{'s' if days > 1 else ''}: {d.module} {d.kind} {d.period or d.cycle}, {d.due_at:%Y-%m-%d}",
                    d.notes,
                    "/filings/calendar/",
                    email=s.channel == Subscription.Channel.EMAIL,
                    key=f"deadline:{d.pk}:{days}",
                )
                sent += n is not None
    return sent


def expired(cutoff):
    """Notifications sent before the cutoff, for the retention job (record class "notification")."""
    for n in Notification.objects.filter(created_at__lt=cutoff).only("pk").iterator():
        yield f"notification:{n.pk}", n.delete


# --- reading and settings


def unread(user) -> int:
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def mark_read(user, ids=None) -> int:
    qs = Notification.objects.filter(user=user, read_at__isnull=True)
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    return qs.update(read_at=timezone.now())


def subscribe(user, event: str, channel: str = Subscription.Channel.EMAIL, *, module: str = "", organisation=None) -> Subscription:
    if event not in BY_CODE:
        raise ValueError(f"{event} is not an event you can subscribe to")
    s, _ = Subscription.objects.update_or_create(
        user=user, event=event, module=module, organisation=organisation, defaults={"channel": channel}
    )
    return s


def unsubscribe(user, event: str, *, module: str = "", organisation=None) -> None:
    Subscription.objects.filter(user=user, event=event, module=module, organisation=organisation).delete()
