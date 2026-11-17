"""Comments on a proposed specification version: before a version is published, CPUC staff may propose it (editor.propose), and anyone
signed in reads it, runs the pre-upload checker against it and comments on it until its comment period closes. A comment is about the
whole version, one table or one field, and is written for the commenter's active organization; CPUC staff answer a filer's comment
with a comment that replies to it, while the version is proposed or back in draft. Comments are never changed (the database refuses,
migration 0008): a correction is another comment. They stay with the version once it is published, as the record of its consultation,
readable by anyone signed in.

Who hears: the CPUC system administrator who proposed the version gets each filer's comment as a direct message, every CPUC system
administrator has the comments not yet answered in My queue, and the filer hears when the CPUC answers. Anyone may subscribe to
`specification.proposed` and `specification.commented`. Each comment is audited on the version (`specification.commented`)."""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.urls import reverse

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import SpecComment, SpecificationVersion
from .service import SpecificationError


class CommentRefused(SpecificationError):
    """A comment that cannot be posted as given; str(e) is the sentence the page shows."""


def max_chars() -> int:
    return settings.CEDARS_COMMENT_MAX_CHARS


def is_cpuc_staff(user) -> bool:
    """Answers comments for the CPUC: a CPUC system administrator, who may manage specifications."""
    return bool(getattr(user, "is_authenticated", False)) and access.is_system_admin(user)


def side(user, organisation=None):
    """The organization the user comments for: the CPUC for its system administrators, else their active organization (one they are a
    member of), else None (they cannot comment)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if is_cpuc_staff(user):
        return access.cpuc()
    if organisation is not None and access.organisations_of(user).filter(pk=organisation.pk).exists():
        return organisation
    return None


def can_comment(user, version: SpecificationVersion, organisation=None) -> bool:
    return version.open_for_comment and side(user, organisation) is not None


def can_answer(user, version: SpecificationVersion) -> bool:
    """CPUC staff answer while the version is proposed or back in draft; a published version's comments are a closed record."""
    return version.status in (SpecificationVersion.Status.PROPOSED, SpecificationVersion.Status.DRAFT) and is_cpuc_staff(user)


def topics(version: SpecificationVersion) -> list[tuple[str, list[tuple[str, str]]]]:
    """What a comment may be about, for the form: [(group, [(value, words)])]; value "" is the whole version, "Table" a table and
    "Table.Field" one of its fields."""
    out = [("The version", [("", "The whole version")])]
    for t in version.tables.prefetch_related("fields"):
        out.append((f"The {t.name} table", [(t.name, f"The {t.name} table"), *((f"{t.name}.{f.name}", f.name) for f in t.fields.all())]))
    return out


def _about(version: SpecificationVersion, about: str) -> tuple[str, str]:
    table, _, field = (about or "").strip().partition(".")
    if not table:
        return "", ""
    t = version.tables.filter(name=table).first()
    if t is None:
        raise CommentRefused(f"{version.name} has no {table} table.")
    if field and not t.fields.filter(name=field).exists():
        raise CommentRefused(f"The {table} table of {version.name} has no field {field}.")
    return table, field


def _text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise CommentRefused("Write the comment.")
    if len(text) > max_chars():
        raise CommentRefused(f"Keep a comment to {max_chars():,} characters (see Limits, /limits/); post a longer one in parts.")
    return text


def _author_name(user, org) -> str:
    """The name shown with a comment: never an email address."""
    return (getattr(user, "name", "") or "").strip() or f"A {org.code} user"


def page(version: SpecificationVersion, comment: SpecComment | None = None) -> str:
    url = reverse("specifications:version", args=[version.specification.code, version.slug])
    return f"{url}#comment-{comment.pk}" if comment is not None else f"{url}#comments-h"


def notice_key(comment: SpecComment) -> str:
    """The key of a comment's direct message: the proposer, or the filer answered, hears of it once, whether by the direct message or
    by a subscription to `specification.commented`."""
    return f"specification-comment:{comment.pk}"


def _tell(user, comment: SpecComment, title: str) -> None:
    from cedars.services.notifications import service as notifications

    if user is None or not user.is_active or user.pk == comment.author_id:
        return
    body = comment.text[:500] + ("..." if len(comment.text) > 500 else "")
    link = page(comment.version, comment)
    key = notice_key(comment)
    transaction.on_commit(lambda: notifications.notify(user, title, body, link, key=key))


@transaction.atomic
def post(actor, version: SpecificationVersion, text: str, *, about: str = "", organisation=None) -> SpecComment:
    """A comment on a proposed version that is open for comment, about the whole version, a table ("Measure") or a field
    ("Measure.NumUnits"), written for the commenter's organization (`side`)."""
    v = SpecificationVersion.objects.select_for_update(of=("self",)).select_related("specification", "proposed_by").get(pk=version.pk)
    org = side(actor, organisation)
    if org is None:
        raise AccessDenied("Sign in, and choose the organization you comment for.")
    if v.status != SpecificationVersion.Status.PROPOSED:
        raise CommentRefused(f"{v.name} is not proposed for comment.")
    if not v.open_for_comment:
        raise CommentRefused(f"Comments on {v.name} closed {v.comments_last_day:%B} {v.comments_last_day.day}.")
    table, field = _about(v, about)
    text = _text(text)
    c = SpecComment.objects.create(
        version=v, round=v.proposals or 1, author=actor, author_name=_author_name(actor, org), organisation=org, table=table,
        field=field, text=text,
    )  # fmt: skip
    _audit(c, actor, org)
    if not is_cpuc_staff(actor):
        _tell(v.proposed_by, c, f"Comment from {org.code} on {v.specification.name}: {v.name}")
    return c


@transaction.atomic
def reply(actor, comment: SpecComment, text: str) -> SpecComment:
    """The CPUC's answer to a comment (a comment of its own that replies to it), while the version is proposed or back in draft."""
    parent = SpecComment.objects.select_related("version__specification", "author").get(pk=comment.pk)
    v = SpecificationVersion.objects.select_for_update().select_related("specification").get(pk=parent.version_id)
    if not is_cpuc_staff(actor):
        raise AccessDenied("CPUC staff answer comments on a proposed specification version.")
    if not can_answer(actor, v):
        raise CommentRefused(f"{v.name} is published: its comments are a closed record.")
    if parent.reply_to_id is not None:
        parent = parent.reply_to
    org = access.cpuc()
    c = SpecComment.objects.create(
        version=v, round=parent.round, author=actor, author_name=_author_name(actor, org), organisation=org, reply_to=parent,
        table=parent.table, field=parent.field, text=_text(text),
    )  # fmt: skip
    _audit(c, actor, org)
    _tell(parent.author, c, f"The CPUC answered your comment on {v.specification.name}: {v.name}")
    return c


def _audit(c: SpecComment, actor, org) -> None:
    audit.record(
        "specification.commented",
        obj=c.version,
        after={
            "comment": c.pk, "round": c.round, "organisation": org.code, "about": c.about, "reply_to": c.reply_to_id, "text": c.text,
        },
        actor=actor,
        organisation=org,
    )  # fmt: skip


def thread(version: SpecificationVersion) -> list[dict]:
    """The version's comments, oldest first, each with its answers: [{"c": comment, "replies": [...], "staff": a CPUC note}]."""
    rows = list(version.comments.select_related("organisation").order_by("created_at", "pk"))
    cpuc = access.cpuc()
    replies: dict[int, list] = {}
    for c in rows:
        if c.reply_to_id:
            replies.setdefault(c.reply_to_id, []).append(c)
    return [
        {"c": c, "replies": replies.get(c.pk, []), "staff": cpuc is not None and c.organisation_id == cpuc.pk}
        for c in rows
        if c.reply_to_id is None
    ]


def counts(version: SpecificationVersion) -> dict[str, int]:
    """{"comments": filers' and staff's comments (answers aside), "unanswered": filers' comments no CPUC answer replies to}."""
    cpuc = access.cpuc()
    top = version.comments.filter(reply_to__isnull=True)
    unanswered = top.filter(replies__isnull=True)
    if cpuc is not None:
        unanswered = unanswered.exclude(organisation=cpuc)
    return {"comments": top.count(), "unanswered": unanswered.distinct().count()}


def unanswered(user) -> list:
    """My queue: filers' comments on a proposed or draft version that no CPUC answer replies to, for CPUC system administrators."""
    from cedars.core.queue import Item

    if not is_cpuc_staff(user):
        return []
    cpuc = access.cpuc()
    qs = (
        SpecComment.objects.filter(reply_to__isnull=True, replies__isnull=True, version__status__in=["proposed", "draft"])
        .exclude(organisation=cpuc)
        .select_related("version__specification", "organisation")
        .order_by("created_at")
    )
    return [
        Item(
            f"{c.version.specification.name}: {c.version.name}, about {c.about}",
            page(c.version, c),
            c.organisation.code,
            c.created_at,
            None,
            c.text[:200] + ("..." if len(c.text) > 200 else ""),
        )
        for c in qs
    ]


def concern(event):
    """What a specification version's audit event concerns, for notifications: its page, and for a comment what was said."""
    from cedars.services.notifications.service import Concern

    v = SpecificationVersion.objects.select_related("specification").get(pk=event.object_id)
    url = reverse("specifications:version", args=[v.specification.code, v.slug])
    after = event.after or {}
    if event.action == "specification.commented":
        text = after.get("text", "")
        who = after.get("organisation", "")
        answer = " (the CPUC's answer)" if after.get("reply_to") else ""
        return Concern(
            link=f"{url}#comment-{after.get('comment', '')}",
            title=f"Comment from {who} on {v.specification.name}: {v.name}{answer}",
            body=f"About {after.get('about', 'the whole version')}: " + text[:500] + ("..." if len(text) > 500 else ""),
            key=f"specification-comment:{after['comment']}" if after.get("comment") else "",
        )
    if event.action == "specification.proposed":
        last = after.get("last_day", "")
        return Concern(
            link=url,
            title=f"Proposed for comment: {v.specification.name}: {v.name}",
            body=(f"Comments are open through {last}. " if last else "") + (after.get("note") or v.summary or "")[:500],
        )
    return Concern(link=url)
