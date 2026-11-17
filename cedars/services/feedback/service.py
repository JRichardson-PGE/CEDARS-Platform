"""Requests from any page. The Feedback link in every page's footer opens a short form: a question, a problem, an idea or a
comment. The request keeps the page it came from, the sender (when signed in) and the organization they act for, the site's version
and environment, and the message; each one is an audit event, and every CPUC system administrator is told of it (on their
Notifications page, and by email unless they turned direct emails off), as is the team inbox CEDARS_FEEDBACK_EMAIL when it is set.

Administrators work through the requests on the Feedback page: they answer one, ask its sender a question, keep a note for
themselves, or close it with a note. The CPUC's support desk (the support permission) does the same without being a system
administrator, for every kind but a security concern. Accessibility barriers and security concerns are urgent: said so when the
staff are told, and first in the list and the queue. A signed-in sender follows theirs on My requests, is told of each answer
and question, replies there, and can say it is settled. The administrators of the organization a sender acted for can read (not
answer) its questions, problems and accessibility barriers, unless the sender kept one private. A question from the CPUC is in
the sender's My queue until they reply; requests waiting on the CPUC are in the staff's.

Limits (releases/limits.py): a message has at most CEDARS_FEEDBACK_MAX_CHARS characters, and one sender sends at most
CEDARS_FEEDBACK_PER_HOUR an hour (signed in: per account; signed out: per keyed hash of the address, which is all that is kept)."""

import hashlib
import hmac
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import models, transaction
from django.utils import timezone

import cedars
from cedars.core import queue as my_queue
from cedars.services import audit
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications

from .models import Feedback, Reply

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


def staff_for(fb: Feedback) -> list:
    """Who works on a request: the CPUC's system administrators and its support desk; a security concern, the administrators alone."""
    org = access.cpuc()
    if org is None:
        return []
    people = {u.pk: u for u in access.holders(org, perms.SYSTEM_ADMIN)}
    if fb.kind != Feedback.Kind.SECURITY:
        people.update({u.pk: u for u in access.holders(org, perms.SUPPORT)})
    return list(people.values())


def can_answer(user, fb: Feedback | None = None) -> bool:
    """Whether the user works on requests (on this one, when given): a system administrator on any, the support desk on all but
    security concerns."""
    if user is None or not user.is_authenticated:
        return False
    if access.is_system_admin(user):
        return True
    return access.is_support(user) and (fb is None or fb.kind != Feedback.Kind.SECURITY)


def for_staff(user):
    """The requests a member of staff may read."""
    qs = Feedback.objects.all()
    return qs if access.is_system_admin(user) else qs.exclude(kind=Feedback.Kind.SECURITY)


@transaction.atomic
def submit(
    *,
    page: str,
    message: str,
    user=None,
    organisation=None,
    address: str = "",
    kind: str = Feedback.Kind.COMMENT,
    private: bool = False,
    limited: bool = True,
) -> Feedback:
    """A new request. `limited=False` is for another service sending on a signed-in person's behalf (a tester's findings),
    which the hourly limit on what one person sends from the form does not count or stop."""
    message = (message or "").strip()
    if kind not in Feedback.Kind.values:
        raise FeedbackRefused(
            "Choose what this is: a question, a problem, an accessibility barrier, a security concern, an idea or a comment."
        )
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
    if limited and (key or signed_in):
        recent = recent.exclude(kind=Feedback.Kind.FINDING)
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
        kind=kind,
        private=bool(private) and signed_in and shared_with(organisation),
        site_version=cedars.__version__,
        environment=settings.ENVIRONMENT,
        active_at=timezone.now(),
    )
    audit.record(
        "feedback.received",
        obj=fb,
        after={"page": fb.page, "kind": fb.kind, "characters": len(message)},
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
    title = ("Urgent: " if fb.is_urgent else "") + f"{fb.get_kind_display()} from {fb.page}"
    body = f"{who} wrote from {fb.page}:\n\n{fb.message}"
    link = f"/admin/feedback/{fb.pk}/"
    told = 0
    for user in staff_for(fb):
        try:
            if notifications.notify(user, title, body, link, key=f"feedback:{fb.pk}"):
                told += 1
        except Exception:
            log.exception("feedback %s: telling user %s failed", fb.pk, user.pk)
    inbox = getattr(settings, "CEDARS_FEEDBACK_EMAIL", "")
    if inbox and fb.kind != Feedback.Kind.SECURITY:  # a security concern goes to the system administrators only
        prefix = "[SANDBOX] " if settings.ENVIRONMENT == "sandbox" else ""
        try:
            send_mail(f"{prefix}CEDARS: {title}"[:200], f"{body}\n\n{settings.CEDARS_BASE_URL.rstrip('/')}{link}", None, [inbox])
        except Exception:
            log.exception("feedback %s: the team inbox email failed", fb.pk)
    Feedback.objects.filter(pk=fb.pk).update(notified=told)
    return told


def _require_staff(actor, fb: Feedback) -> None:
    if not can_answer(actor, fb):
        if fb.kind == Feedback.Kind.SECURITY and access.is_support(actor):
            raise AccessDenied("Only a CPUC system administrator can work on a security concern.")
        raise AccessDenied("Only the CPUC's system administrators and support desk can work on requests.")


@transaction.atomic
def close(actor, fb: Feedback, note: str = "") -> Feedback:
    _require_staff(actor, fb)
    fb = Feedback.objects.select_for_update().get(pk=fb.pk)
    if fb.status == Feedback.Status.CLOSED:
        raise AccessDenied("That feedback is already closed.")
    before_status = fb.status
    fb.status, fb.closed_by, fb.closed_at, fb.note = Feedback.Status.CLOSED, actor, timezone.now(), note.strip()
    fb.save(update_fields=["status", "closed_by", "closed_at", "note"])
    audit.record("feedback.closed", obj=fb, before={"status": before_status}, after={"status": "closed"}, reason=fb.note, actor=actor)
    return fb


@transaction.atomic
def reopen(actor, fb: Feedback, reason: str) -> Feedback:
    _require_staff(actor, fb)
    if not reason.strip():
        raise AccessDenied("Give a reason for reopening it.")
    fb = Feedback.objects.select_for_update().get(pk=fb.pk)
    if fb.status != Feedback.Status.CLOSED:
        raise AccessDenied("That feedback is not closed.")
    before = {"status": fb.status, "note": fb.note}
    fb.status, fb.closed_by, fb.closed_at, fb.active_at = Feedback.Status.NEW, None, None, timezone.now()
    fb.save(update_fields=["status", "closed_by", "closed_at", "active_at"])
    audit.record("feedback.reopened", obj=fb, before=before, after={"status": "new"}, reason=reason, actor=actor)
    return fb


# ---- the conversation


def can_read(user, fb: Feedback) -> bool:
    """A request is read by its sender, by the CPUC's staff who work on it, and (a question, a problem or an accessibility barrier
    the sender did not keep private) by the administrators of the organization the sender acted for."""
    if user is None or not user.is_authenticated:
        return False
    return fb.user_id == user.pk or can_answer(user, fb) or organisation_reads(user, fb)


def shared_with(organisation) -> bool:
    """Whether an organization's administrators read its members' requests: any organization but the CPUC, whose own staff's
    requests go to the CPUC's staff as everyone's do."""
    return organisation is not None and organisation.kind != organisation.Kind.CPUC


def organisation_reads(user, fb: Feedback) -> bool:
    return (
        shared_with(fb.organisation)
        and not fb.private
        and fb.kind in Feedback.FOR_ORGANISATION
        and access.is_org_admin(user, fb.organisation)
    )


def readers(fb: Feedback) -> str:
    """Who can read a request besides its sender, in words for the sender's page."""
    if fb.kind == Feedback.Kind.SECURITY:
        return "the CPUC's system administrators"
    if shared_with(fb.organisation) and not fb.private and fb.kind in Feedback.FOR_ORGANISATION:
        return f"the CPUC's CEDARS team and {fb.organisation.code}'s administrators"
    return "the CPUC's CEDARS team"


def for_organisation(organisation):
    """What an organization's administrators may read: its members' questions, problems and accessibility barriers, sent while
    acting for it and not kept private; the most recently active first."""
    return Feedback.objects.filter(organisation=organisation, private=False, kind__in=Feedback.FOR_ORGANISATION).order_by(
        models.F("active_at").desc(nulls_last=True), "-pk"
    )


def conversation(fb: Feedback, *, staff: bool) -> list[Reply]:
    """A request's replies in order; the staff's own notes only for staff."""
    replies = fb.replies.select_related("author")
    return list(replies if staff else replies.exclude(kind=Reply.Kind.NOTE))


def _body(body: str, what: str) -> str:
    body = (body or "").strip()
    if not body:
        raise FeedbackRefused(f"Write {what} first.")
    if len(body) > settings.CEDARS_FEEDBACK_MAX_CHARS:
        raise FeedbackRefused(
            f"A reply can be at most {settings.CEDARS_FEEDBACK_MAX_CHARS:,} characters; yours has {len(body):,}. "
            "Shorten it or send it in parts."
        )
    return body


def _add(fb: Feedback, kind: str, author, body: str, status: str | None, action: str) -> Reply:
    reply = Reply.objects.create(feedback=fb, kind=kind, author=author, body=body)
    before = fb.status
    fields = []
    if kind != Reply.Kind.NOTE:
        fb.active_at = timezone.now()
        fields.append("active_at")
    if status and status != fb.status:
        fb.status = status
        fields.append("status")
    if fields:
        fb.save(update_fields=fields)
    audit.record(
        action,
        obj=fb,
        before={"status": before},
        after={"status": fb.status, "reply": reply.pk, "characters": len(body)},
        actor=author,
    )
    return reply


def _tell_sender(fb: Feedback, reply: Reply, title: str) -> None:
    if fb.user_id is None:
        return
    link = f"/feedback/{fb.pk}/"

    def send():
        try:
            notifications.notify(fb.user, title, reply.body, link, key=f"feedback:{fb.pk}:reply:{reply.pk}")
        except Exception:
            log.exception("feedback %s: telling its sender of reply %s failed", fb.pk, reply.pk)

    transaction.on_commit(send)


def _staff_reply(actor, fb: Feedback, body: str, kind: str, status: str, action: str, title: str) -> Reply:
    _require_staff(actor, fb)
    body = _body(body, "your answer" if kind == Reply.Kind.ANSWER else "your question")
    fb = Feedback.objects.select_for_update(of=("self",)).select_related("user").get(pk=fb.pk)
    if not fb.is_open:
        raise FeedbackRefused("This request is closed. Reopen it to reply.")
    if fb.user_id is None:
        raise FeedbackRefused("This was sent signed out, so there is nobody to reply to. Close it with a note instead.")
    reply = _add(fb, kind, actor, body, status, action)
    _tell_sender(fb, reply, title.format(page=fb.page))
    return reply


@transaction.atomic
def answer(actor, fb: Feedback, body: str) -> Reply:
    """The CPUC's answer: the sender is told, and the request is Answered until they reply or say it is settled."""
    return _staff_reply(
        actor, fb, body, Reply.Kind.ANSWER, Feedback.Status.ANSWERED, "feedback.answered", "The CPUC answered your request from {page}"
    )


@transaction.atomic
def ask(actor, fb: Feedback, body: str) -> Reply:
    """A question to the sender: they are told, and the request waits on them (their My queue) until they reply."""
    return _staff_reply(
        actor,
        fb,
        body,
        Reply.Kind.QUESTION,
        Feedback.Status.WAITING,
        "feedback.question_asked",
        "The CPUC has a question about your request from {page}",
    )


@transaction.atomic
def add_note(actor, fb: Feedback, body: str) -> Reply:
    """A note the staff keep on a request; the sender never sees it and is not told."""
    _require_staff(actor, fb)
    fb = Feedback.objects.select_for_update().get(pk=fb.pk)
    return _add(fb, Reply.Kind.NOTE, actor, _body(body, "the note"), None, "feedback.note_added")


@transaction.atomic
def start(actor, fb: Feedback) -> Feedback:
    """Mark a received request as being looked at, so its sender and the other administrators see that someone has it."""
    _require_staff(actor, fb)
    fb = Feedback.objects.select_for_update().get(pk=fb.pk)
    if fb.status != Feedback.Status.NEW:
        raise FeedbackRefused("Only a request that is still Received can be marked as being looked at.")
    fb.status = Feedback.Status.LOOKING
    fb.save(update_fields=["status"])
    audit.record("feedback.started", obj=fb, before={"status": "new"}, after={"status": "looking"}, actor=actor)
    return fb


@transaction.atomic
def reply(user, fb: Feedback, body: str) -> Reply:
    """The sender's reply: the request goes back to the CPUC (Being looked at) and the administrators are told."""
    if user is None or not user.is_authenticated or fb.user_id != user.pk:
        raise AccessDenied("Only the person who sent a request can reply to it.")
    body = _body(body, "your reply")
    fb = Feedback.objects.select_for_update().get(pk=fb.pk)
    if not fb.is_open:
        raise FeedbackRefused("This request is closed. Send a new one from the page it is about.")
    added = _add(fb, Reply.Kind.REPLY, user, body, Feedback.Status.LOOKING, "feedback.reply_received")
    transaction.on_commit(lambda: _tell_admins_of_reply(fb.pk, added.pk))
    return added


def _tell_admins_of_reply(pk: int, reply_pk: int) -> None:
    fb = Feedback.objects.select_related("user").get(pk=pk)
    added = Reply.objects.get(pk=reply_pk)
    who = (fb.user.name or fb.user.email) if fb.user else "The sender"
    for user in staff_for(fb):
        try:
            notifications.notify(
                user, f"{who} replied about {fb.page}", added.body, f"/admin/feedback/{fb.pk}/", key=f"feedback:{fb.pk}:reply:{added.pk}"
            )
        except Exception:
            log.exception("feedback %s: telling user %s of a reply failed", fb.pk, user.pk)


@transaction.atomic
def settle(user, fb: Feedback) -> Feedback:
    """The sender says their request is settled: it closes."""
    if user is None or not user.is_authenticated or fb.user_id != user.pk:
        raise AccessDenied("Only the person who sent a request can say it is settled.")
    fb = Feedback.objects.select_for_update().get(pk=fb.pk)
    if not fb.is_open:
        raise FeedbackRefused("This request is already closed.")
    before = fb.status
    fb.status, fb.closed_by, fb.closed_at, fb.note = Feedback.Status.CLOSED, user, timezone.now(), "The sender said it is settled."
    fb.save(update_fields=["status", "closed_by", "closed_at", "note"])
    audit.record("feedback.settled", obj=fb, before={"status": before}, after={"status": "closed"}, actor=user)
    return fb


def mine(user):
    """A person's own requests, the ones waiting on them first, then the newest activity first."""
    return Feedback.objects.filter(user=user).order_by(
        models.Case(models.When(status=Feedback.Status.WAITING, then=0), default=1), models.F("active_at").desc(nulls_last=True), "-pk"
    )


def status_words(fb: Feedback, *, staff: bool) -> str:
    """A request's status as its reader sees it: the sender's "Waiting for you" is the staff's "Waiting for the sender"."""
    if staff and fb.status == Feedback.Status.WAITING:
        return "Waiting for the sender"
    return fb.get_status_display()


# ---- My queue


def questions_for(user) -> list:
    """My queue: the user's requests the CPUC asked a question about and they have not answered."""
    return [
        my_queue.Item(what=f"{fb.get_kind_display()} from {fb.page}", url=f"/feedback/{fb.pk}/", since=fb.active_at or fb.created_at)
        for fb in Feedback.objects.filter(user=user, status=Feedback.Status.WAITING).order_by("active_at")
    ]


def to_answer(user) -> list:
    """My queue: for the CPUC's staff who work on requests, those waiting on the CPUC: the urgent ones, then the oldest first."""
    if not can_answer(user):
        return []
    waiting = for_staff(user).filter(status__in=Feedback.TO_ANSWER).select_related("user", "organisation").order_by(*URGENT_FIRST)
    return [
        my_queue.Item(
            what=("Urgent: " if fb.is_urgent else "") + f"{fb.get_kind_display()} from {fb.page}",
            url=f"/admin/feedback/{fb.pk}/",
            organisation=fb.organisation.code if fb.organisation else "",
            since=fb.active_at or fb.created_at,
            note=((fb.user.name or fb.user.email) if fb.user else "Sent signed out") + f". {fb.get_status_display()}.",
        )
        for fb in waiting[:QUEUE_MOST]
    ]


URGENT_FIRST = (models.Case(models.When(kind__in=Feedback.URGENT, then=0), default=1), "active_at", "pk")
QUEUE_MOST = 50  # the queue page shows the oldest; the Feedback page lists them all


def expired(cutoff):
    """Closed messages sent before the cutoff, for the retention job (record class "feedback"). New ones are kept until closed."""
    for fb in Feedback.objects.filter(created_at__lt=cutoff, status=Feedback.Status.CLOSED).only("pk").iterator():
        yield f"feedback:{fb.pk}", fb.delete
