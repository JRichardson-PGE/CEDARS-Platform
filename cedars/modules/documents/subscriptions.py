"""Subscriptions (BR-15.5), replacing the PDA's attribute subscriptions and the PSR's study and work-order notices, and the key dates
CPUC ED posts on the library's home page (BR-15.3).

A signed-in user follows a collection, a value of any list a document carries (its document type included), a work order or a study.
They hear, on the notifications page and by email unless they chose the page only:
  - when a version of a matching document is published (an unpublished upload notifies nobody), with its comment period if it has one;
  - when the comment period of a matching published version is opened or moved;
  - when a followed study's status changes.
One message per user per event however many of their subscriptions match. Only published work is announced, so anyone may follow
anything. Messages are sent after the change commits (a rolled-back change notifies nobody)."""

from __future__ import annotations

from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications

from .models import AttributeOption, Collection, Document, DocumentSubscription, DocumentVersion, KeyDate, Study, WorkOrder
from .service import DocumentError

EMAIL = DocumentSubscription.Channel.EMAIL
IN_APP = DocumentSubscription.Channel.IN_APP


def _signed_in(user) -> None:
    if user is None or not user.is_authenticated:
        raise AccessDenied("Sign in to follow documents and studies.")


def _target(collection: str = "", option: AttributeOption | None = None, work_order: WorkOrder | None = None, study: Study | None = None):
    given = [x for x in (collection, option, work_order, study) if x]
    if len(given) != 1:
        raise DocumentError("Follow one collection, value, work order or study at a time.")
    if collection and collection not in Collection.values:
        raise DocumentError("Choose a collection of the library.")
    return {"collection": collection, "option": option, "work_order": work_order, "study": study}


def subscribe(user, *, collection: str = "", option=None, work_order=None, study=None, channel: str = EMAIL) -> DocumentSubscription:
    """Follow one thing (again: change how its messages come)."""
    _signed_in(user)
    if channel not in DocumentSubscription.Channel.values:
        raise DocumentError("Choose how the messages come.")
    target = _target(collection, option, work_order, study)
    sub, _ = DocumentSubscription.objects.update_or_create(user=user, **target, defaults={"channel": channel})
    return sub


def unsubscribe(user, *, collection: str = "", option=None, work_order=None, study=None) -> None:
    _signed_in(user)
    DocumentSubscription.objects.filter(user=user, **_target(collection, option, work_order, study)).delete()


@transaction.atomic
def set_all(user, *, collections: list[str], options: list[AttributeOption], channel: str = EMAIL) -> None:
    """The subscriptions page's collections and values, as ticked: every other collection and value subscription is ended; the work
    orders and studies followed are kept, and all of them take the chosen channel."""
    _signed_in(user)
    if channel not in DocumentSubscription.Channel.values:
        raise DocumentError("Choose how the messages come.")
    mine = DocumentSubscription.objects.filter(user=user)
    mine.filter(~Q(collection=""), ~Q(collection__in=collections)).delete()
    mine.filter(option__isnull=False).exclude(option__in=options).delete()
    for c in collections:
        subscribe(user, collection=c, channel=channel)
    for o in options:
        subscribe(user, option=o, channel=channel)
    mine.update(channel=channel)


def of(user) -> list[DocumentSubscription]:
    if user is None or not user.is_authenticated:
        return []
    return list(DocumentSubscription.objects.filter(user=user).select_related("option__attribute_list", "work_order", "study"))


def follows(user, *, work_order=None, study=None) -> DocumentSubscription | None:
    if user is None or not user.is_authenticated:
        return None
    return DocumentSubscription.objects.filter(user=user, **_target(work_order=work_order, study=study)).first()


def matching(doc: Document) -> QuerySet:
    """The subscriptions a document matches: its collection, its document type or any value it carries, its work order or its study."""
    q = Q(collection=doc.collection) | Q(option=doc.document_type_id) | Q(option__in=doc.attributes.values("pk"))
    if doc.work_order_id:
        q |= Q(work_order=doc.work_order_id)
    if doc.study_id:
        q |= Q(study=doc.study_id)
    return DocumentSubscription.objects.filter(q, user__is_active=True)


def _recipients(subs) -> dict:
    """{user: email?}: one message per user, by email if any of their matching subscriptions chose it."""
    out = {}
    for s in subs.select_related("user"):
        out[s.user] = out.get(s.user, False) or s.channel == EMAIL
    return out


def _send_all(subs, title: str, body: str, link: str, key: str) -> None:
    for user, email in _recipients(subs).items():
        notifications.send(user, title, body, link, email=email, key=key)


def _period_text(version: DocumentVersion) -> str:
    period = getattr(version, "comment_period", None)
    if period is None:
        return ""
    return f"Comments are open {period.opens:%Y-%m-%d} to {period.closes:%Y-%m-%d}."


def on_published(version: DocumentVersion) -> None:
    """Tell the followers of a document that a version of it is published (called by publish_version)."""
    pk = version.pk

    def deliver():
        v = DocumentVersion.objects.select_related("document__document_type", "comment_period").get(pk=pk)
        doc = v.document
        what = v.stage_label or f"Version {v.number}"
        body = "\n".join(
            x for x in [f"{doc.get_collection_display()}, {doc.document_type.label}. {what} is published.", v.reason, _period_text(v)] if x
        )
        _send_all(matching(doc), f"Published: {doc.title}"[:300], body, doc.get_absolute_url(), f"documents:published:{pk}")

    transaction.on_commit(deliver)


def on_period(version: DocumentVersion) -> None:
    """Tell the followers of a published document that a version's comment period opened or moved (called by comments.set_period)."""
    if not version.published:
        return
    pk = version.pk

    def deliver():
        v = DocumentVersion.objects.select_related("document", "comment_period").get(pk=pk)
        p = v.comment_period
        key = f"documents:period:{p.pk}:{p.opens:%Y%m%d}:{p.closes:%Y%m%d}"
        what = v.stage_label.lower() if v.stage_label else f"version {v.number}"
        _send_all(
            matching(v.document),
            f"Open for comment: {v.document.title}"[:300],
            f"Comments on the {what} are open {p.opens:%Y-%m-%d} to {p.closes:%Y-%m-%d}.",
            f"{v.document.get_absolute_url()}comments/",
            key,
        )

    transaction.on_commit(deliver)


def on_study_status(study: Study, before: str, after: str) -> None:
    """Tell a study's followers its status changed (called by studies.change_study)."""
    if before == after:
        return
    pk = study.pk

    def deliver():
        s = Study.objects.get(pk=pk)
        subs = DocumentSubscription.objects.filter(study=s, user__is_active=True)
        title = f"{s.study_id} is now {after}" if after else f"{s.study_id} has no status"
        _send_all(
            subs, title[:300], f"{s.title}: the status was {before or 'not set'}, now {after or 'not set'}.", s.get_absolute_url(), ""
        )

    transaction.on_commit(deliver)


def notice(event) -> notifications.Concern | None:
    """What the notification service tells the followers of a document's PAs and programs when a version is published (registered for
    documents.documentversion, BR-09.2): the PAs it applies to (else the organization that filed it), its programs and its study's, and
    the same message key as on_published, so a user who also follows its collection or study hears once."""
    if event.action != "documents.version_published":
        return None
    v = DocumentVersion.objects.select_related("document__document_type", "document__study").get(pk=event.object_id)
    doc = v.document
    pas = set(doc.pas.values_list("pk", flat=True)) or {doc.owner_id}
    programs = set(doc.programs.values_list("prg_id", flat=True))
    if doc.study_id:
        programs |= set(doc.study.coverage.values_list("program__prg_id", flat=True))
    what = v.stage_label or f"Version {v.number}"
    return notifications.Concern(
        pas,
        "documents",
        doc.get_absolute_url(),
        programs,
        title=f"Published: {doc.title}"[:300],
        body=f"{doc.get_collection_display()}, {doc.document_type.label}. {what} is published.",
        key=f"documents:published:{v.pk}",
    )


def subscribers(collection: str = "") -> QuerySet:
    """Every subscription, for CPUC ED's list of who follows what (the PDA's subscriber report)."""
    qs = DocumentSubscription.objects.select_related("user", "option__attribute_list", "work_order", "study").order_by(
        "user__email", "collection", "option__attribute_list__position", "option__position", "work_order__number", "study__number"
    )
    return qs.filter(user__is_active=True)


# --- key dates (BR-15.3)


def _require_ed(actor) -> None:
    from .deliverables import is_ed

    if not is_ed(actor):
        raise AccessDenied("Only CPUC ED posts key dates.")


@transaction.atomic
def add_key_date(actor, on: date, title: str, *, detail: str = "", link: str = "") -> KeyDate:
    _require_ed(actor)
    if on is None or not title.strip():
        raise DocumentError("Give the date and what happens on it.")
    if len(title.strip()) > 200 or len(detail.strip()) > 1000:
        raise DocumentError("Keep the title to 200 characters and the detail to 1,000.")
    kd = KeyDate.objects.create(on=on, title=title.strip(), detail=detail.strip(), link=link.strip(), posted_by=actor)
    audit.record("documents.key_date_added", obj=kd, after=audit.snapshot(kd, ["on", "title", "detail", "link"]), actor=actor)
    return kd


@transaction.atomic
def remove_key_date(actor, kd: KeyDate, *, reason: str = "") -> KeyDate:
    """Take a key date off the home page; the row and its audit events stay."""
    _require_ed(actor)
    kd = KeyDate.objects.select_for_update().get(pk=kd.pk)
    if kd.removed_at is not None:
        raise DocumentError("That key date is already removed.")
    kd.removed_at = timezone.now()
    kd.save(update_fields=["removed_at"])
    audit.record("documents.key_date_removed", obj=kd, before=audit.snapshot(kd, ["on", "title"]), reason=reason, actor=actor)
    return kd


RECENT_DAYS = 30


def key_dates(day: date | None = None) -> tuple[list[KeyDate], list[KeyDate]]:
    """(upcoming from today, oldest first; the past RECENT_DAYS days, newest first) of the key dates shown."""
    day = day or timezone.localdate()
    shown = KeyDate.objects.filter(removed_at__isnull=True)
    upcoming = list(shown.filter(on__gte=day).order_by("on", "id")[:20])
    recent = list(shown.filter(on__lt=day, on__gte=day - timedelta(days=RECENT_DAYS)).order_by("-on", "-id"))
    return upcoming, recent


def open_for_comment(day: date | None = None) -> list[DocumentVersion]:
    """The published versions whose comment period is open today, closing soonest first."""
    day = day or timezone.localdate()
    return list(
        DocumentVersion.objects.filter(published_at__isnull=False, comment_period__opens__lte=day, comment_period__closes__gte=day)
        .select_related("document", "comment_period")
        .order_by("comment_period__closes", "document__title")
    )
