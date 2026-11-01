"""Feedback from any page (BR-12.4). The Feedback link in every page's footer opens a short form that records the page it came from,
the sender (when signed in) and the organization they act for, and the message; each message is an audit event, and every CPUC system
administrator is told of it (on their Notifications page, and by email unless they turned direct emails off), as is the team inbox
CEDARS_FEEDBACK_EMAIL when it is set. Administrators work through the messages on the Feedback page and close each one with a note.

Limits (releases/limits.py): a message has at most CEDARS_FEEDBACK_MAX_CHARS characters, and one sender sends at most
CEDARS_FEEDBACK_PER_HOUR an hour (signed in: per account; signed out: per keyed hash of the address, which is all that is kept)."""

import hashlib
import hmac
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications

from .models import Feedback

log = logging.getLogger(__name__)


class FeedbackRefused(ValueError):
    """The message cannot be taken; str(e) says why, as the page shows it."""


def safe_page(path: str) -> str:
    """The sender's page as a path on this site ("/budgets/?year=2028"), or "/" for anything else (a full URL, "//host")."""
    path = (path or "").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path[:500]


def sender_key(address: str) -> str:
    """A keyed hash of a signed-out sender's address: enough to count their messages, not to find the address again."""
    if not address:
        return ""
    return hmac.new(settings.SECRET_KEY.encode(), address.encode(), hashlib.sha256).hexdigest()


def admins():
    org = access.cpuc()
    return access.holders(org, perms.SYSTEM_ADMIN) if org else []


@transaction.atomic
def submit(*, page: str, message: str, user=None, organisation=None, address: str = "") -> Feedback:
    message = (message or "").strip()
    if not message:
        raise FeedbackRefused("Write your feedback first.")
    if len(message) > settings.CEDARS_FEEDBACK_MAX_CHARS:
        raise FeedbackRefused(
            f"Feedback can be at most {settings.CEDARS_FEEDBACK_MAX_CHARS:,} characters; yours has {len(message):,}. "
            "Shorten it or send it in parts."
        )
    signed_in = user is not None and user.is_authenticated
    key = "" if signed_in else sender_key(address)
    since = timezone.now() - timedelta(hours=1)
    recent = Feedback.objects.filter(created_at__gte=since)
    recent = recent.filter(user=user) if signed_in else recent.filter(user__isnull=True, sender_key=key)
    if key or signed_in:
        if recent.count() >= settings.CEDARS_FEEDBACK_PER_HOUR:
            raise FeedbackRefused(
                f"You have sent {settings.CEDARS_FEEDBACK_PER_HOUR} messages in the last hour, the most CEDARS takes. Try again later."
            )
    fb = Feedback.objects.create(
        page=safe_page(page),
        user=user if signed_in else None,
        organisation=organisation if signed_in else None,
        message=message,
        sender_key=key,
    )
    audit.record(
        "feedback.received",
        obj=fb,
        after={"page": fb.page, "characters": len(message)},
        actor=fb.user,
        organisation=fb.organisation,
        actor_label="" if signed_in else "public",
    )
    transaction.on_commit(lambda: tell_admins(fb.pk))
    return fb


def tell_admins(pk: int) -> int:
    """Tell the CPUC system administrators (and the team inbox, if set) of a new message. Never raises: the message is kept either way."""
    fb = Feedback.objects.select_related("user", "organisation").get(pk=pk)
    who = (fb.user.name or fb.user.email) if fb.user else "Someone signed out"
    if fb.organisation:
        who += f" ({fb.organisation.code})"
    title = f"Feedback on {fb.page}"
    body = f"{who} wrote from {fb.page}:\n\n{fb.message}"
    link = f"/admin/feedback/{fb.pk}/"
    told = 0
    for user in admins():
        try:
            if notifications.notify(user, title, body, link, key=f"feedback:{fb.pk}"):
                told += 1
        except Exception:
            log.exception("feedback %s: telling user %s failed", fb.pk, user.pk)
    inbox = getattr(settings, "CEDARS_FEEDBACK_EMAIL", "")
    if inbox:
        prefix = "[SANDBOX] " if settings.ENVIRONMENT == "sandbox" else ""
        try:
            send_mail(f"{prefix}CEDARS: {title}"[:200], f"{body}\n\n{settings.CEDARS_BASE_URL.rstrip('/')}{link}", None, [inbox])
        except Exception:
            log.exception("feedback %s: the team inbox email failed", fb.pk)
    Feedback.objects.filter(pk=fb.pk).update(notified=told)
    return told


def _require_admin(actor) -> None:
    if not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator can work on feedback.")


@transaction.atomic
def close(actor, fb: Feedback, note: str = "") -> Feedback:
    _require_admin(actor)
    fb = Feedback.objects.select_for_update().get(pk=fb.pk)
    if fb.status == Feedback.Status.CLOSED:
        raise AccessDenied("That feedback is already closed.")
    fb.status, fb.closed_by, fb.closed_at, fb.note = Feedback.Status.CLOSED, actor, timezone.now(), note.strip()
    fb.save(update_fields=["status", "closed_by", "closed_at", "note"])
    audit.record("feedback.closed", obj=fb, before={"status": "new"}, after={"status": "closed"}, reason=fb.note, actor=actor)
    return fb


@transaction.atomic
def reopen(actor, fb: Feedback, reason: str) -> Feedback:
    _require_admin(actor)
    if not reason.strip():
        raise AccessDenied("Give a reason for reopening it.")
    fb = Feedback.objects.select_for_update().get(pk=fb.pk)
    if fb.status != Feedback.Status.CLOSED:
        raise AccessDenied("That feedback is not closed.")
    before = {"status": fb.status, "note": fb.note}
    fb.status, fb.closed_by, fb.closed_at = Feedback.Status.NEW, None, None
    fb.save(update_fields=["status", "closed_by", "closed_at"])
    audit.record("feedback.reopened", obj=fb, before=before, after={"status": "new"}, reason=reason, actor=actor)
    return fb


def expired(cutoff):
    """Closed messages sent before the cutoff, for the retention job (record class "feedback"). New ones are kept until closed."""
    for fb in Feedback.objects.filter(created_at__lt=cutoff, status=Feedback.Status.CLOSED).only("pk").iterator():
        yield f"feedback:{fb.pk}", fb.delete
