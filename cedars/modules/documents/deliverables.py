"""EM&V deliverables (BR-15.11): one document whose versions go through the revision stages in order (draft workplan, final workplan,
draft study results, final study results), each draft open for comments in its own window, finals revised as further numbered versions
with a reason, then the PAs' responses to the recommendations against CPUC ED's due dates and an optional CPUC ED reply.

Who may do what, beyond service.py's rules for any document:
  - the document's managers (its owner, PAs named on it, the study manager, CPUC ED) add the work-stage versions;
  - CPUC ED (documents.admin, or documents.submit in the CPUC) asks PAs to respond by a due date and adds the ED reply;
  - a PA asked to respond files its response with documents.submit and publishes it with documents.confirm, without managing the rest
    of the deliverable."""

from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied

from .models import Document, DocumentVersion, ResponseRequest, Stage
from .service import CONFIRM, SUBMIT, DocumentError, Upload, _check_uploads, _cpuc_has, _new_version, is_admin, publish_version

WORK_STAGES = [Stage.DRAFT_WORKPLAN, Stage.FINAL_WORKPLAN, Stage.DRAFT_RESULTS, Stage.FINAL_RESULTS]
DRAFTS = {Stage.DRAFT_WORKPLAN: "Workplan comments", Stage.DRAFT_RESULTS: "Study-result comments"}
RESPONSE_REASON = "Response to the recommendations"


def is_ed(user) -> bool:
    return is_admin(user) or (user is not None and user.is_authenticated and _cpuc_has(user, SUBMIT))


def _last_work_stage(doc: Document) -> int:
    stages = set(doc.versions.exclude(stage="").values_list("stage", flat=True))
    return max((WORK_STAGES.index(s) for s in stages if s in WORK_STAGES), default=-1)


def has_final_results(doc: Document, *, published: bool = True) -> bool:
    qs = doc.versions.filter(stage=Stage.FINAL_RESULTS)
    return (qs.filter(published_at__isnull=False) if published else qs).exists()


def next_stages(user, doc: Document) -> list[str]:
    """The stages a manager may add a version at: the current work stage again (another draft, a revised final) or a later one; the ED
    reply once the final results are published. Never an earlier stage."""
    if not doc.deliverable:
        return []
    last = _last_work_stage(doc)
    stages = [s.value for s in WORK_STAGES[max(last, 0) :]]
    if is_ed(user) and has_final_results(doc):
        stages.append(Stage.ED_REPLY.value)
    return stages


def stage_refusal(user, doc: Document, stage: str) -> str:
    if stage == Stage.PA_RESPONSE:
        return "A PA files its response to the recommendations from the deliverable's page, once CPUC ED has asked for it."
    if stage == Stage.ED_REPLY:
        return "Only CPUC ED replies, once the final study results are published."
    if stage in WORK_STAGES:
        current = Stage(WORK_STAGES[_last_work_stage(doc)]).label.lower()
        return f"The deliverable is at the {current} stage; a version cannot go back to an earlier stage."
    return "Choose the stage of this version."


def stage_label(doc: Document, version: DocumentVersion | None, day: date | None = None) -> str:
    """The deliverable's current stage as search and the study page show it: its newest visible version's stage, the draft's comment stage
    while the draft's comment window is open, and "PA responses" once a PA has responded."""
    if version is None or not version.stage:
        return ""
    day = day or timezone.localdate()
    if version.stage in DRAFTS:
        period = getattr(version, "comment_period", None)
        if version.published and period is not None and period.open_on(day):
            return f"{DRAFTS[version.stage]} (on {version.stage_label.lower()}, until {period.closes:%Y-%m-%d})"
    if version.stage == Stage.PA_RESPONSE:
        return "PA responses"
    return version.stage_label


def current(doc: Document) -> DocumentVersion | None:
    """The newest published version of a deliverable (the one the public sees as its stage)."""
    return doc.versions.filter(published_at__isnull=False).select_related("responding_pa", "comment_period").order_by("-number").first()


def history(versions: list[DocumentVersion]) -> list[DocumentVersion]:
    """The revision history in order, oldest first (visible_versions gives newest first)."""
    return sorted(versions, key=lambda v: v.number)


# --- the PAs' responses to the recommendations


@transaction.atomic
def request_responses(actor, doc: Document, pas: list[Organisation], due: date, *, reason: str = "") -> list[ResponseRequest]:
    """Ask PAs to respond to the recommendations by a due date (or move the due date of a PA already asked)."""
    if not is_ed(actor):
        raise AccessDenied("Only CPUC ED asks PAs for their responses to the recommendations.")
    if not doc.deliverable or not has_final_results(doc):
        raise DocumentError("PAs respond to a deliverable's published final study results.")
    if not pas:
        raise DocumentError("Choose the PAs to respond.")
    if any(o.kind != Organisation.Kind.PA for o in pas):
        raise DocumentError("Only program administrators respond to the recommendations.")
    if due is None:
        raise DocumentError("Give the date the responses are due.")
    out = []
    for pa in pas:
        req = ResponseRequest.objects.filter(document=doc, pa=pa).first()
        if req is None:
            req = ResponseRequest.objects.create(document=doc, pa=pa, due=due, requested_by=actor)
            audit.record(
                "documents.responses_requested", obj=doc, after={"pa": pa.code, "due": due.isoformat()}, reason=reason, actor=actor
            )
        elif req.due != due:
            before = {"pa": pa.code, "due": req.due.isoformat()}
            req.due = due
            req.save(update_fields=["due"])
            audit.record(
                "documents.response_due_changed",
                obj=doc,
                before=before,
                after={"pa": pa.code, "due": due.isoformat()},
                reason=reason,
                actor=actor,
            )
        out.append(req)
    return out


def responses(req: ResponseRequest) -> list[DocumentVersion]:
    return list(req.document.versions.filter(stage=Stage.PA_RESPONSE, responding_pa=req.pa).order_by("number"))


def status(req: ResponseRequest, day: date | None = None) -> str:
    """Where a PA's response stands: due, overdue, filed but not published, responded (on time or late)."""
    day = day or timezone.localdate()
    filed = responses(req)
    published = [v for v in filed if v.published]
    if published:
        first = published[0].published_at.date()
        return "Responded" if first <= req.due else f"Responded late ({(first - req.due).days} days)"
    if filed:
        return "Filed, not yet published"
    if day > req.due:
        return f"Overdue since {req.due:%Y-%m-%d}"
    return f"Due {req.due:%Y-%m-%d}"


def responding_pas(user, doc: Document) -> list[Organisation]:
    """The PAs the user may file a response for: those asked to respond in which the user holds documents.submit."""
    if user is None or not user.is_authenticated:
        return []
    return [r.pa for r in doc.response_requests.select_related("pa") if access.has_permission(user, r.pa, SUBMIT)]


@transaction.atomic
def file_response(
    actor, pa: Organisation, doc: Document, uploads: list[Upload], *, reason: str = "", publish: bool = False
) -> DocumentVersion:
    """A PA's response to the recommendations, a version of the deliverable at the stage PA response (a second one, with its reason,
    revises it)."""
    req = ResponseRequest.objects.filter(document=doc, pa=pa).first()
    if req is None:
        raise AccessDenied(f"CPUC ED has not asked {pa.code} to respond to this deliverable.")
    if not access.has_permission(actor, pa, SUBMIT):
        raise AccessDenied(f"You need the Documents: submit permission in {pa.code} to file its response.")
    Document.objects.select_for_update().get(pk=doc.pk)
    earlier = doc.versions.filter(stage=Stage.PA_RESPONSE, responding_pa=pa).exists()
    if earlier and not reason.strip():
        raise DocumentError(f"Say why this replaces {pa.code}'s earlier response.")
    checked = _check_uploads(uploads)
    version = _new_version(
        actor, pa, doc, uploads, checked, reason=reason.strip() or RESPONSE_REASON, stage=Stage.PA_RESPONSE, responding_pa=pa
    )
    if publish:
        if not access.has_permission(actor, pa, CONFIRM):
            raise AccessDenied(f"You need the Documents: confirm permission in {pa.code} to publish its response.")
        version = publish_version(actor, version)
    return version
