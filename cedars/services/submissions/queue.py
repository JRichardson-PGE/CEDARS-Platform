"""The submissions service's work in each user's queue (cedars.core.queue, BR-09.5): correction requests waiting on an approver or their
delegate, priced versions waiting for someone who may confirm them, and filings whose latest comment came from the other side."""

from __future__ import annotations

from datetime import datetime

from django.db.models import Max, Q

from cedars.core.queue import Item
from cedars.services.accounts import service as access

from . import comments, service
from .models import ReopenRequest, Submission, SubmissionVersion

MODULE_NAMES = {"budgets": "Budget filing", "claims": "Claims", "monthly": "Monthly report"}


def describe(submission: Submission) -> str:
    d = submission.deadline
    return f"{MODULE_NAMES.get(d.module, d.module)}: {submission.organisation.code} {d.kind}, {d.period or d.cycle}"


def _filing_page(submission: Submission, v: SubmissionVersion | None = None) -> str:
    page = service.deadline_page(submission.deadline)
    if page:
        return f"{page}?org={submission.organisation.code}"
    return comments.version_page(v) if v is not None else ""


def reopen_requests(user) -> list[Item]:
    out = []
    approvers: dict[str, object] = {}
    pending = ReopenRequest.objects.filter(status=ReopenRequest.Status.PENDING).select_related(
        "submission__deadline__period", "submission__deadline__cycle", "submission__organisation", "requested_by"
    )
    for r in pending:
        module = r.submission.deadline.module
        if module not in approvers:
            approvers[module] = service.approver_for(user, module)
        approver = approvers[module]
        if approver is None:
            continue
        note = f"Reason: {r.reason}"
        if approver != user:
            note += f" You decide as {approver.name or approver.email}'s delegate."
        out.append(Item(describe(r.submission), "/filings/reopen-requests/", r.submission.organisation.code, r.requested_at, None, note))
    return out


def confirmations(user) -> list[Item]:
    """Versions validated and priced on their current rows (the latest of their submission), in organizations where the user holds
    `<module>.confirm`."""
    if user is None or not user.is_authenticated:
        return []
    allowed = Q(pk__in=[])
    for org in access.organisations_of(user):
        modules = [p.split(".")[0] for p in access.effective_permissions(user, org) if p.endswith(".confirm")]
        if modules:
            allowed |= Q(submission__organisation=org, submission__deadline__module__in=modules)
    ready = (
        SubmissionVersion.objects.filter(allowed, status=SubmissionVersion.Status.VALIDATED, origin=SubmissionVersion.Origin.FILED)
        .exclude(calculation={})
        .select_related("submission__deadline__period", "submission__deadline__cycle", "submission__organisation")
    )
    latest = dict(
        SubmissionVersion.objects.filter(submission__in=[v.submission_id for v in ready])
        .values_list("submission_id")
        .annotate(n=Max("number"))
    )
    out = []
    for v in ready:
        if latest.get(v.submission_id) != v.number:
            continue
        try:
            since = datetime.fromisoformat(v.calculation.get("finished_at", ""))
        except ValueError:
            since = v.validated_at
        out.append(
            Item(
                f"{describe(v.submission)}, version {v.number}",
                _filing_page(v.submission, v),
                v.submission.organisation.code,
                since,
                v.submission.deadline.due_at,
                "Priced on its current rows: review the results and confirm.",
            )
        )
    return out


def replies(user) -> list[Item]:
    out = []
    for sub, c in comments.awaiting_reply(user):
        text = c.text if len(c.text) <= 120 else c.text[:117] + "..."
        page = comments.version_page(c.version)
        out.append(
            Item(
                f"{describe(sub)}, version {c.version.number}",
                f"{page}#comment-{c.pk}" if page else "",
                sub.organisation.code,
                c.created_at,
                None,
                f"{c.author_name} ({c.organisation.code}): {text}",
            )
        )
    return out
