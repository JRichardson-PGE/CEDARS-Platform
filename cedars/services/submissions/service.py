"""The submission state machine. Modules call these functions; every transition checks the actor's permission in the submitting
organisation (`<module>.submit`, `<module>.confirm`; approvers hold `<module>.approve` in the CPUC organisation), writes an audit
event, and runs the module's hooks.

Hooks (`register_publisher`): when a version is confirmed it is published at once with no further calculation; a module or the
publication service registers a function that writes the snapshot, and an un-confirm before the due date removes it again (versions
and snapshots superseded before the due date are not kept).
"""

from collections.abc import Callable

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied

from .models import Deadline, Delegation, ReopenRequest, Submission, SubmissionVersion, WorkflowPolicy

VERSION_FIELDS = ["number", "status", "reason", "files", "rule_set", "findings", "calculation", "late", "confirmed_by", "published_at"]
_publishers: dict[str, tuple[Callable, Callable]] = {}


def register_publisher(module: str, publish: Callable[[SubmissionVersion], None], unpublish: Callable[[SubmissionVersion], None]) -> None:
    _publishers[module] = (publish, unpublish)


def policy(module: str) -> WorkflowPolicy:
    return WorkflowPolicy.objects.get_or_create(module=module)[0]


def _require(actor, submission: Submission, action: str) -> None:
    if actor is None:
        return
    if not access.has_permission(actor, submission.organisation, f"{submission.module}.{action}"):
        raise AccessDenied(f"You need {submission.module}.{action} in {submission.organisation.code} to do that.")


def _change(actor, v: SubmissionVersion, action: str, reason: str = "", **fields) -> SubmissionVersion:
    before = audit.snapshot(v, VERSION_FIELDS)
    for k, val in fields.items():
        setattr(v, k, val)
    v.save()
    audit.record(
        action,
        obj=v,
        before=before,
        after=audit.snapshot(v, VERSION_FIELDS),
        reason=reason,
        actor=actor,
        organisation=v.submission.organisation if actor is None else None,
    )
    return v


def past_due(deadline: Deadline, now=None) -> bool:
    return (now or timezone.now()) > deadline.due_at


# --- reading


def latest(submission: Submission) -> SubmissionVersion | None:
    return submission.versions.order_by("-number").first()


def current(submission: Submission) -> SubmissionVersion | None:
    """The published version that stands: what dashboards, downloads and later modules read."""
    return submission.versions.filter(status=SubmissionVersion.Status.PUBLISHED).order_by("-number").first()


# --- calendar


@transaction.atomic
def add_deadline(actor, *, module: str, kind: str, due_at, period=None, cycle=None, opens_at=None, notes: str = "") -> Deadline:
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator sets the filing calendar.")
    d = Deadline.objects.create(module=module, kind=kind, period=period, cycle=cycle, due_at=due_at, opens_at=opens_at, notes=notes)
    audit.record("deadline.created", obj=d, after={"module": module, "kind": kind, "due_at": due_at.isoformat()}, actor=actor)
    return d


@transaction.atomic
def move_deadline(actor, deadline: Deadline, due_at, reason: str) -> Deadline:
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator sets the filing calendar.")
    before = {"due_at": deadline.due_at.isoformat()}
    deadline.due_at = due_at
    deadline.save()
    audit.record("deadline.moved", obj=deadline, before=before, after={"due_at": due_at.isoformat()}, reason=reason, actor=actor)
    return deadline


# --- the first version and drafts


@transaction.atomic
def start(actor, deadline: Deadline, organisation: Organisation) -> SubmissionVersion:
    """Open a submission for the organisation (version 1, draft). Starting again returns the open draft."""
    submission, _ = Submission.objects.get_or_create(deadline=deadline, organisation=organisation)
    _require(actor, submission, "submit")
    if deadline.opens_at and timezone.now() < deadline.opens_at:
        raise AccessDenied(f"This filing opens on {deadline.opens_at:%Y-%m-%d}.")
    v = latest(submission)
    if v is not None:
        if v.locked:
            raise AccessDenied("This filing is already confirmed; un-confirm it before the due date, or ask to reopen it after.")
        return v
    v = SubmissionVersion.objects.create(submission=submission, number=1, created_by=actor)
    audit.record("submission.started", obj=v, actor=actor, organisation=organisation if actor is None else None)
    return v


@transaction.atomic
def attach_files(actor, v: SubmissionVersion, files: list[dict]) -> SubmissionVersion:
    """Replace the version's uploaded files (the module stores them and parses the rows). The version goes back to draft and any
    calculation is void."""
    v = SubmissionVersion.objects.select_for_update().get(pk=v.pk)
    _require(actor, v.submission, "submit")
    if v.locked:
        raise AccessDenied("A confirmed version cannot change; un-confirm it or ask to reopen.")
    return _change(actor, v, "submission.files_attached", files=files, status=SubmissionVersion.Status.DRAFT, findings={}, calculation={})


@transaction.atomic
def record_validation(actor, v: SubmissionVersion, rule_set, summary: dict) -> SubmissionVersion:
    """The module ran the rule set on the version's rows: summary = {records, rejected, warnings}. With no rejected records the version
    is validated; otherwise it stays a draft with its findings."""
    v = SubmissionVersion.objects.select_for_update().get(pk=v.pk)
    _require(actor, v.submission, "submit")
    if v.locked:
        raise AccessDenied("A confirmed version cannot be re-validated.")
    ok = summary.get("rejected", 0) == 0
    return _change(
        actor,
        v,
        "submission.validated" if ok else "submission.validation_failed",
        rule_set=rule_set,
        findings=summary,
        status=SubmissionVersion.Status.VALIDATED if ok else SubmissionVersion.Status.DRAFT,
        validated_at=timezone.now() if ok else None,
    )


@transaction.atomic
def record_change(actor, v: SubmissionVersion, what: str, still_valid: bool = True) -> SubmissionVersion:
    """A cell corrected in place. The module re-validates that row and says whether the version is still valid; either way the
    calculation no longer matches the rows and is void."""
    v = SubmissionVersion.objects.select_for_update().get(pk=v.pk)
    _require(actor, v.submission, "submit")
    if v.locked:
        raise AccessDenied("A confirmed version cannot change; un-confirm it or ask to reopen.")
    status = v.status if still_valid else SubmissionVersion.Status.DRAFT
    return _change(actor, v, "submission.changed", what, calculation={}, status=status)


@transaction.atomic
def record_calculation(actor, v: SubmissionVersion, *, run: str, input_hash: str, pack_versions: dict | None = None) -> SubmissionVersion:
    """A calculation and statewide allocation run on the version's current rows has finished (the Calculations module calls this)."""
    v = SubmissionVersion.objects.select_for_update().get(pk=v.pk)
    if v.status != SubmissionVersion.Status.VALIDATED:
        raise AccessDenied("Only a validated version is priced for confirmation.")
    return _change(
        actor,
        v,
        "submission.priced",
        calculation={"run": run, "input_hash": input_hash, "finished_at": timezone.now().isoformat()},
        pack_versions=pack_versions or {},
    )


# --- confirm, un-confirm, reopen


@transaction.atomic
def confirm(actor, v: SubmissionVersion, current_input_hash: str) -> SubmissionVersion:
    """Confirm and publish. Offered only once a run on the current rows has finished (the module passes the rows' hash now), so
    confirming is the sign-off on exactly the results the PA reviewed; publication follows at once with no further calculation."""
    v = SubmissionVersion.objects.select_for_update().select_related("submission__deadline").get(pk=v.pk)
    _require(actor, v.submission, "confirm")
    if v.status != SubmissionVersion.Status.VALIDATED:
        raise AccessDenied("Only a validated version can be confirmed.")
    if not v.calculation or v.calculation.get("input_hash") != current_input_hash:
        raise AccessDenied("Price the current rows first: confirming needs a finished run on exactly these rows.")
    late = past_due(v.submission.deadline)
    if late and v.number == 1 and not policy(v.submission.module).late_confirmation:
        raise AccessDenied("The due date has passed and this module does not take late filings.")
    now = timezone.now()
    _change(
        actor,
        v,
        "submission.confirmed",
        status=SubmissionVersion.Status.CONFIRMED,
        confirmed_by=actor,
        confirmed_at=now,
        late=late and v.number == 1,
    )
    publisher = _publishers.get(v.submission.module)
    if publisher:
        publisher[0](v)
    return _change(actor, v, "submission.published", status=SubmissionVersion.Status.PUBLISHED, published_at=timezone.now())


@transaction.atomic
def unconfirm(actor, v: SubmissionVersion, reason: str = "") -> SubmissionVersion:
    """Before the due date a PA may un-confirm, change and re-confirm as often as needed. The same version goes back to validated and
    its publication is withdrawn, so nothing superseded before the due date is kept."""
    v = SubmissionVersion.objects.select_for_update().select_related("submission__deadline").get(pk=v.pk)
    _require(actor, v.submission, "confirm")
    if not v.locked:
        raise AccessDenied("That version is not confirmed.")
    if v.number > 1 or past_due(v.submission.deadline):
        raise AccessDenied("After the due date a confirmed filing is corrected by reopening it, not un-confirmed.")
    publisher = _publishers.get(v.submission.module)
    if publisher and v.status == SubmissionVersion.Status.PUBLISHED:
        publisher[1](v)
    return _change(
        actor,
        v,
        "submission.unconfirmed",
        reason,
        status=SubmissionVersion.Status.VALIDATED,
        confirmed_by=None,
        confirmed_at=None,
        published_at=None,
        late=False,
    )


def _new_version(actor, submission: Submission, reason: str) -> SubmissionVersion:
    n = (submission.versions.aggregate(n=Max("number"))["n"] or 0) + 1
    prev = latest(submission)
    v = SubmissionVersion.objects.create(submission=submission, number=n, reason=reason, created_by=actor, files=prev.files if prev else [])
    audit.record(
        "submission.reopened",
        obj=v,
        reason=reason,
        after={"number": n, "from_version": prev.number if prev else None},
        actor=actor,
        organisation=submission.organisation if actor is None else None,
    )
    return v


@transaction.atomic
def request_reopen(actor, submission: Submission, reason: str) -> ReopenRequest | SubmissionVersion:
    """After the due date: ask to correct this organisation's filing for this deadline only. Under an "open" policy the new draft
    version is created at once (returned); under "approval" a request waits for an approver (returned); "closed" refuses."""
    submission = Submission.objects.select_related("deadline", "organisation").get(pk=submission.pk)
    _require(actor, submission, "submit")
    if not reason.strip():
        raise AccessDenied("Give the reason for the correction.")
    v = latest(submission)
    if v is None or not v.locked:
        raise AccessDenied("There is an open draft already; correct it and confirm it.")
    if not past_due(submission.deadline):
        raise AccessDenied("Before the due date, un-confirm the filing instead.")
    rule = policy(submission.module).reopen_after_due
    if rule == WorkflowPolicy.Reopen.CLOSED:
        raise AccessDenied("This module takes no corrections after close-out.")
    if rule == WorkflowPolicy.Reopen.OPEN:
        return _new_version(actor, submission, reason)
    if submission.reopen_requests.filter(status=ReopenRequest.Status.PENDING).exists():
        raise AccessDenied("A reopen request for this filing is already waiting.")
    req = ReopenRequest.objects.create(submission=submission, requested_by=actor, reason=reason)
    audit.record("reopen.requested", obj=req, reason=reason, actor=actor)
    return req


def approver_for(actor, module: str, on=None):
    """The approver the actor acts for: themselves if they hold `<module>.approve` in the CPUC organisation, else the approver who
    delegated to them for today, else None."""
    org = access.cpuc()
    if org is None:
        return None
    if access.has_permission(actor, org, f"{module}.approve"):
        return actor
    day = on or timezone.localdate()
    for d in Delegation.objects.filter(module=module, delegate=actor, starts__lte=day, ends__gte=day).select_related("approver"):
        if access.has_permission(d.approver, org, f"{module}.approve"):
            return d.approver
    return None


@transaction.atomic
def decide_reopen(actor, req: ReopenRequest, approve: bool, note: str = "") -> SubmissionVersion | None:
    req = ReopenRequest.objects.select_for_update().select_related("submission__deadline").get(pk=req.pk)
    if req.status != ReopenRequest.Status.PENDING:
        raise AccessDenied("That request has been decided.")
    approver = approver_for(actor, req.submission.module)
    if approver is None:
        raise AccessDenied("Only an approver, or their delegate, decides reopen requests.")
    req.status = ReopenRequest.Status.APPROVED if approve else ReopenRequest.Status.DECLINED
    req.decided_by, req.decided_at, req.decision_note = actor, timezone.now(), note
    req.decided_as_delegate_of = approver if approver != actor else None
    req.save()
    audit.record(f"reopen.{req.status}", obj=req, reason=note, actor=actor)
    return _new_version(actor, req.submission, req.reason) if approve else None


@transaction.atomic
def delegate(actor, *, module: str, delegate_to, starts, ends) -> Delegation:
    org = access.cpuc()
    if org is None or not access.has_permission(actor, org, f"{module}.approve"):
        raise AccessDenied("Only an approver can name a delegate.")
    if ends < starts:
        raise AccessDenied("The delegation ends before it starts.")
    d = Delegation.objects.create(module=module, approver=actor, delegate=delegate_to, starts=starts, ends=ends)
    audit.record(
        "delegation.created",
        obj=d,
        after={"module": module, "delegate": delegate_to.email, "starts": str(starts), "ends": str(ends)},
        actor=actor,
    )
    return d


@transaction.atomic
def set_policy(actor, module: str, **settings) -> WorkflowPolicy:
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator changes workflow settings.")
    p = policy(module)
    before = {k: getattr(p, k) for k in settings}
    for k, val in settings.items():
        setattr(p, k, val)
    p.save()
    audit.record("workflow_policy.changed", obj=p, before=before, after=settings, actor=actor)
    return p
