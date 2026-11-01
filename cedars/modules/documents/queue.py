"""Documents' work in each user's queue (cedars.core.queue, BR-09.5): versions filed but not yet published, for the people who may publish
them (documents.confirm in an organization that manages the document); responses to recommendations CPUC ED asked of the user's PAs and
not yet published (documents.submit there); and questions nobody has answered, for those who may respond to them."""

from __future__ import annotations

from django.db.models import Q

from cedars.core.queue import Item
from cedars.services.accounts import service as access

from . import comments, deliverables
from .models import Comment, DocumentVersion, ResponseRequest, Stage
from .service import CONFIRM, SUBMIT


def _orgs_with(user, permission: str) -> list:
    if user is None or not user.is_authenticated:
        return []
    return [o for o in access.organisations_of(user) if access.has_permission(user, o, permission)]


def drafts(user) -> list[Item]:
    orgs = _orgs_with(user, CONFIRM)
    if not orgs:
        return []
    managed = Q(document__owner__in=orgs) | Q(document__pas__in=orgs) | Q(document__study__manager__in=orgs)
    versions = (
        DocumentVersion.objects.filter(managed, published_at__isnull=True)
        .exclude(stage=Stage.PA_RESPONSE)  # a PA's response is the responding PA's to publish (responses below)
        .select_related("document", "organisation")
        .distinct()
    )
    return [
        Item(
            f"{v.document.title}: {v.stage_label or f'version {v.number}'}",
            v.document.get_absolute_url(),
            v.organisation.code,
            v.created_at,
            None,
            "Filed, not yet published.",
        )
        for v in versions
    ]


def responses(user) -> list[Item]:
    orgs = _orgs_with(user, SUBMIT)
    out = []
    for r in ResponseRequest.objects.filter(pa__in=orgs).select_related("pa", "document"):
        state = deliverables.status(r)
        if state.startswith("Responded"):
            continue
        out.append(
            Item(f"Response to the recommendations: {r.document.title}", r.document.get_absolute_url(), r.pa.code, None, r.due, state)
        )
    return out


def questions(user) -> list[Item]:
    if user is None or not user.is_authenticated:
        return []
    open_questions = (
        Comment.objects.filter(kind=Comment.Kind.QUESTION, withdrawn_at__isnull=True, responses__isnull=True, pda_id__isnull=True)
        .select_related("version__document__owner", "version__document__study__manager")
        .distinct()
    )
    out = []
    can_respond: dict[int, bool] = {}
    for c in open_questions:
        doc = c.version.document
        if doc.pk not in can_respond:
            can_respond[doc.pk] = bool(comments.responder_organisations(user, doc))
        if can_respond[doc.pk]:
            out.append(
                Item(
                    f"Question on {doc.title}: {c.subject}",
                    f"{doc.get_absolute_url()}comments/#comment-{c.pk}",
                    c.organisation.code if c.organisation else "",
                    c.created_at,
                    None,
                    f"Asked by {c.author_name}.",
                )
            )
    return out
