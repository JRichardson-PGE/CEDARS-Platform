"""The submission state machine. Modules call these functions; every transition checks the actor's permission in the submitting
organisation (`<module>.submit`, `<module>.confirm`; approvers hold `<module>.approve` in the CPUC organisation), writes an audit
event, and runs the module's hooks.

Hooks (`register_publisher`): when a version is confirmed it is published at once with no further calculation; a module or the
publication service registers a function that writes the snapshot, and an un-confirm before the due date removes it again (versions
and snapshots superseded before the due date are not kept). Listeners (`register_listener`) hear every module's publications and
un-confirmations after the module's own hook: the publication snapshots service is one.

Pricing (`register_pricer`, `price`): the Calculations module registers the function that prices a version's job, so a filing module
asks this service to price its version and never imports the Calculations module (modules never import each other).

Imported versions (`import_version`): legacy CEDARS' years load as published versions marked imported (origin "imported"), as CEDARS
published them. They are read-only history: every path that would change, reprice, un-confirm, reopen or correct a submission that holds
one refuses (`_refuse_imported`), and no publisher hook runs for them (the loading module writes its own derived lines).

Claims totals (`register_claims_totals`, `claims_totals`): the Claims module registers the function that gives a PA's confirmed claims
for a year by program, so another module (Monthly reports' 13th-month report, BR-14.2) reads them without importing Claims.
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

VERSION_FIELDS = [
    "number",
    "status",
    "origin",
    "reason",
    "files",
    "rule_set",
    "findings",
    "calculation",
    "late",
    "confirmed_by",
    "published_at",
]
_publishers: dict[str, tuple[Callable, Callable]] = {}
_listeners: list[tuple[Callable, Callable]] = []
_pricers: list[Callable] = []
_claims_totals: list[Callable] = []


def register_publisher(module: str, publish: Callable[[SubmissionVersion], None], unpublish: Callable[[SubmissionVersion], None]) -> None:
    _publishers[module] = (publish, unpublish)


def register_listener(published: Callable[[SubmissionVersion], None], unpublished: Callable[[SubmissionVersion], None]) -> None:
    """Called for every module's version once it is published, and when one is un-confirmed (before it goes back to validated)."""
    if (published, unpublished) not in _listeners:
        _listeners.append((published, unpublished))


def register_pricer(price: Callable) -> None:
    """The Calculations module's pricing function: price(actor, version, job_zip, *, name, acc_version, allocation_version, settings)
    starts a run on the job (a local file) for the version and returns the run's id; the run's end calls record_calculation."""
    _pricers[:] = [price]


def register_claims_totals(totals: Callable) -> None:
    """The Claims module's reader: totals(organisation, year) -> [{prg_id, pa, year, quarters, final, records, expenditures, incentives,
    gross_kwh, gross_kw, gross_therm, net_kwh, net_kw, net_therm, tsb}], one per program, from the year to date of the PA's latest
    confirmed claims (quarters: the quarters confirmed; final: the annual confirmation is)."""
    _claims_totals[:] = [totals]


def claims_totals(organisation: Organisation, year: int) -> list[dict]:
    """A PA's confirmed claims for a year by program; [] when Claims is not installed or nothing is confirmed."""
    return _claims_totals[0](organisation, year) if _claims_totals else []


def policy(module: str) -> WorkflowPolicy:
    return WorkflowPolicy.objects.get_or_create(module=module)[0]


def _require(actor, submission: Submission, action: str) -> None:
    if actor is None:
        return
    if not access.has_permission(actor, submission.organisation, f"{submission.module}.{action}"):
        raise AccessDenied(f"You need {submission.module}.{action} in {submission.organisation.code} to do that.")


IMPORTED_MESSAGE = "This filing was imported from legacy CEDARS. It is read-only: it cannot be changed, repriced, reopened or corrected."


def is_imported(submission: Submission) -> bool:
    return submission.versions.filter(origin=SubmissionVersion.Origin.IMPORTED).exists()


def _refuse_imported(submission: Submission) -> None:
    if is_imported(submission):
        raise AccessDenied(IMPORTED_MESSAGE)


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
    _refuse_imported(submission)
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
    _refuse_imported(v.submission)
    if v.locked:
        raise AccessDenied("A confirmed version cannot change; un-confirm it or ask to reopen.")
    return _change(actor, v, "submission.files_attached", files=files, status=SubmissionVersion.Status.DRAFT, findings={}, calculation={})


@transaction.atomic
def record_validation(actor, v: SubmissionVersion, rule_set, summary: dict) -> SubmissionVersion:
    """The module ran the rule set on the version's rows: summary = {records, rejected, warnings}. With no rejected records the version
    is validated; otherwise it stays a draft with its findings."""
    v = SubmissionVersion.objects.select_for_update().get(pk=v.pk)
    _require(actor, v.submission, "submit")
    _refuse_imported(v.submission)
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
    _refuse_imported(v.submission)
    if v.locked:
        raise AccessDenied("A confirmed version cannot change; un-confirm it or ask to reopen.")
    status = v.status if still_valid else SubmissionVersion.Status.DRAFT
    return _change(actor, v, "submission.changed", what, calculation={}, status=status)


@transaction.atomic
def price(actor, v: SubmissionVersion, job_zip, *, name: str, acc_version: str, allocation_version=None, settings: dict | None = None):
    """Start the calculation and statewide allocation run on a validated version's job (BR-02.5: priced before confirmation). Returns
    the run's id; when it finishes, the Calculations module records it here (record_calculation) and the version can be confirmed."""
    v = SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation").get(pk=v.pk)
    _require(actor, v.submission, "submit")
    _refuse_imported(v.submission)
    if v.status != SubmissionVersion.Status.VALIDATED:
        raise AccessDenied("Only a validated version is priced for confirmation.")
    if not _pricers:
        raise AccessDenied("The calculation service is not installed here, so filings cannot be priced.")
    run = _pricers[0](actor, v, job_zip, name=name, acc_version=acc_version, allocation_version=allocation_version, settings=settings or {})
    audit.record("submission.pricing_started", obj=v, after={"run": str(run), "acc": acc_version}, actor=actor)
    return str(run)


@transaction.atomic
def record_calculation(
    actor, v: SubmissionVersion, *, run: str, input_hash: str, pack_versions: dict | None = None, details: dict | None = None
) -> SubmissionVersion:
    """A calculation and statewide allocation run on the version's current rows has finished (the Calculations module calls this).
    `details` adds what the run was priced with and where its results are (job_sha256, release, acc, allocation_version, results)."""
    v = SubmissionVersion.objects.select_for_update().get(pk=v.pk)
    _refuse_imported(v.submission)
    if v.status != SubmissionVersion.Status.VALIDATED:
        raise AccessDenied("Only a validated version is priced for confirmation.")
    return _change(
        actor,
        v,
        "submission.priced",
        calculation={**(details or {}), "run": run, "input_hash": input_hash, "finished_at": timezone.now().isoformat()},
        pack_versions=pack_versions or {},
    )


# --- imported (legacy CEDARS) versions


@transaction.atomic
def import_version(
    actor,
    *,
    deadline: Deadline,
    organisation: Organisation,
    files: list[dict],
    reason: str,
    calculation: dict | None = None,
    findings: dict | None = None,
) -> SubmissionVersion:
    """Load a filing as legacy CEDARS published it: a new version, published and marked imported, with no validation, pricing or
    publisher hook (a system job, or a CPUC system administrator). The first import is version 1; a later download that differs is
    the next version, with its reason. A submission already filed on this platform is never given an imported version."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator imports legacy CEDARS filings.")
    if not reason.strip():
        raise AccessDenied("Give the reason (the source it was imported from).")
    submission, _ = Submission.objects.select_for_update().get_or_create(deadline=deadline, organisation=organisation)
    if submission.versions.filter(origin=SubmissionVersion.Origin.FILED).exists():
        raise AccessDenied(f"{organisation.code} has filed {deadline} on this platform; legacy data is not imported over it.")
    n = (submission.versions.aggregate(n=Max("number"))["n"] or 0) + 1
    now = timezone.now()
    v = SubmissionVersion.objects.create(
        submission=submission,
        number=n,
        status=SubmissionVersion.Status.PUBLISHED,
        origin=SubmissionVersion.Origin.IMPORTED,
        reason=reason,
        files=files,
        findings=findings or {},
        calculation=calculation or {},
        created_by=actor,
        confirmed_at=now,
        published_at=now,
    )
    audit.record(
        "submission.imported",
        obj=v,
        after=audit.snapshot(v, VERSION_FIELDS),
        reason=reason,
        actor=actor,
        organisation=organisation if actor is None else None,
    )
    return v


# --- confirm, un-confirm, reopen


@transaction.atomic
def confirm(actor, v: SubmissionVersion, current_input_hash: str) -> SubmissionVersion:
    """Confirm and publish. Offered only once a run on the current rows has finished (the module passes the rows' hash now), so
    confirming is the sign-off on exactly the results the PA reviewed; publication follows at once with no further calculation."""
    v = SubmissionVersion.objects.select_for_update().select_related("submission__deadline").get(pk=v.pk)
    _require(actor, v.submission, "confirm")
    _refuse_imported(v.submission)
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
    v = _change(actor, v, "submission.published", status=SubmissionVersion.Status.PUBLISHED, published_at=timezone.now())
    for published, _ in _listeners:
        published(v)
    return v


@transaction.atomic
def unconfirm(actor, v: SubmissionVersion, reason: str = "") -> SubmissionVersion:
    """Before the due date a PA may un-confirm, change and re-confirm as often as needed. The same version goes back to validated and
    its publication is withdrawn, so nothing superseded before the due date is kept."""
    v = SubmissionVersion.objects.select_for_update().select_related("submission__deadline").get(pk=v.pk)
    _require(actor, v.submission, "confirm")
    _refuse_imported(v.submission)
    if not v.locked:
        raise AccessDenied("That version is not confirmed.")
    if v.number > 1 or past_due(v.submission.deadline):
        raise AccessDenied("After the due date a confirmed filing is corrected by reopening it, not un-confirmed.")
    publisher = _publishers.get(v.submission.module)
    if publisher and v.status == SubmissionVersion.Status.PUBLISHED:
        publisher[1](v)
    if v.status == SubmissionVersion.Status.PUBLISHED:
        for _, unpublished in _listeners:
            unpublished(v)
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
    _refuse_imported(submission)
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
    _refuse_imported(submission)
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


def expired_drafts(cutoff):
    """Versions never confirmed (draft or validated) with no activity since the cutoff (record class "unsubmitted_draft"; the period
    counts from the version's last audited change, so a draft being worked on is never removed). A submission left with no versions
    and no reopen requests goes with its last draft."""
    from cedars.services.audit.models import AuditEvent

    open_statuses = [SubmissionVersion.Status.DRAFT, SubmissionVersion.Status.VALIDATED]
    for v in SubmissionVersion.objects.filter(status__in=open_statuses, created_at__lt=cutoff).select_related("submission__organisation"):
        last = AuditEvent.objects.filter(object_type="submissions.submissionversion", object_id=str(v.pk)).aggregate(m=Max("at"))["m"]
        if last and last >= cutoff:
            continue

        def delete(v=v):
            submission = v.submission
            audit.record(
                "submission.draft_expired", obj=v, before={"number": v.number, "status": v.status}, organisation=submission.organisation
            )
            v.delete()
            if not submission.versions.exists() and not submission.reopen_requests.exists():
                submission.delete()

        yield f"submission_version:{v.pk}", delete


def expired_sandbox_filings(cutoff):
    """In the sandbox only: whole submissions (every version, reopen request) started before the cutoff (record class "sandbox_data")."""
    from django.conf import settings

    if settings.ENVIRONMENT != "sandbox":
        return
    for sub in Submission.objects.filter(created_at__lt=cutoff).select_related("organisation"):

        def delete(sub=sub):
            audit.record("submission.sandbox_expired", obj=sub, organisation=sub.organisation)
            sub.reopen_requests.all().delete()
            sub.versions.all().delete()
            sub.delete()

        yield f"submission:{sub.pk}", delete
