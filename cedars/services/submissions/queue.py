"""The submissions service's work in each user's queue (cedars.core.queue): correction requests waiting on an approver or their
delegate, priced versions waiting for someone who may confirm them, and filings whose latest comment came from the other side."""

from __future__ import annotations

from datetime import datetime

from django.db.models import Max, Q

from cedars.core.queue import Item
from cedars.services.accounts import service as access

from . import comments, service
from .models import ReopenRequest, Submission, SubmissionVersion

MODULE_NAMES = {"budgets": "Budget filing", "claims": "Claims", "monthly": "Monthly report"}


def describe(submission: Submission, *, due: bool = True) -> str:
    """A filing as My queue names it: "Claims: PGE 2027 Q1 claim, due 2026-11-01" (Deadline.plain_name; due=False where the queue
    shows the due date in a column of its own)."""
    d = submission.deadline
    return f"{MODULE_NAMES.get(d.module, d.module)}: {d.plain_name(submission.organisation.code, due=due)}"


def _filing_page(submission: Submission, v: SubmissionVersion | None = None) -> str:
    return service.filing_page(submission, v)


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
                f"{describe(v.submission, due=False)}, version {v.number}",
                _filing_page(v.submission, v),
                v.submission.organisation.code,
                since,
                v.submission.deadline.due_at,
                "Priced on its current rows: review the results and confirm.",
            )
        )
    return out


def corrections(user) -> list[Item]:
    """Filings reopened for a correction (version 2 or later, the latest of its submission, not confirmed yet) in organizations where
    the user holds `<module>.submit`, until the correction is priced (then it waits in confirmations, for those who may confirm it)."""
    if user is None or not user.is_authenticated:
        return []
    allowed = Q(pk__in=[])
    for org in access.organisations_of(user):
        modules = [p.split(".")[0] for p in access.effective_permissions(user, org) if p.endswith(".submit")]
        if modules:
            allowed |= Q(submission__organisation=org, submission__deadline__module__in=modules)
    open_ = (
        SubmissionVersion.objects.filter(
            allowed,
            number__gt=1,
            origin=SubmissionVersion.Origin.FILED,
            status__in=[SubmissionVersion.Status.DRAFT, SubmissionVersion.Status.VALIDATED],
        )
        .select_related("submission__deadline__period", "submission__deadline__cycle", "submission__organisation")
        .order_by("created_at")
    )
    latest = dict(
        SubmissionVersion.objects.filter(submission__in=[v.submission_id for v in open_])
        .values_list("submission_id")
        .annotate(n=Max("number"))
    )
    out = []
    for v in open_:
        if latest.get(v.submission_id) != v.number:
            continue
        if v.status == SubmissionVersion.Status.VALIDATED and v.calculation:
            continue  # priced: it waits on whoever confirms it (confirmations)
        note = f"Reopened for a correction{': ' + v.reason if v.reason else ''}. Correct the records, price the filing and confirm it."
        out.append(
            Item(
                f"{describe(v.submission)}, version {v.number}",
                _filing_page(v.submission, v),
                v.submission.organisation.code,
                v.created_at,
                None,
                note,
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
