"""The submission state machine. Modules call these functions; every transition checks the actor's permission in the submitting
organization (`<module>.submit`, `<module>.confirm`; approvers hold `<module>.approve` in the CPUC organization), writes an audit
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
for a year by program, so another module (Monthly reports' 13th-month report) reads them without importing Claims.

Restated runs (`register_restater`, `restate`): the Calculations module registers the function that re-prices a published
version's job as an analysis run, owned by whoever asks and never recorded on the version, so a filing module can show restated results
beside the filed ones without importing the Calculations module. The version itself is never changed: only an audit event says a
restatement was started on it.
"""

import hashlib
from collections.abc import Callable

from django.db import transaction
from django.db.models import Count, Max, Q
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


class PricingUnavailable(AccessDenied):
    """A pricer's refusal that the CPUC's set-up causes and the filer cannot fix (no default engine release, a release without the
    filing's ACC, a reference data pack not yet published). `price` tells the filer that in their words and sends the technical reason
    to the CPUC's system administrators (UAT 2, U04)."""


PRICING_UNAVAILABLE = (
    "This filing cannot be priced yet: the CPUC has not finished setting up pricing for it. Its system administrators have been told. "
    "Nothing in your upload needs to change; price it again once they have."
)
_run_states: list[Callable] = []
_claims_totals: list[Callable] = []
_acc_needs: dict[str, Callable] = {}
_restaters: list[tuple[Callable, Callable]] = []
_deadline_pages: dict[str, Callable] = {}
_setup_pages: dict[str, dict] = {}
_nothing_to_file: dict[str, Callable] = {}
_setup_groups: dict[str, Callable] = {}
_setup_links: dict[str, Callable] = {}
_readers: dict[str, Callable] = {}
_version_pages: dict[str, Callable] = {}
_run_walkthroughs: list[Callable] = []
_filers: dict[str, Callable] = {}
_streams: dict[str, Callable] = {}


def register_publisher(module: str, publish: Callable[[SubmissionVersion], None], unpublish: Callable[[SubmissionVersion], None]) -> None:
    _publishers[module] = (publish, unpublish)


def register_listener(published: Callable[[SubmissionVersion], None], unpublished: Callable[[SubmissionVersion], None]) -> None:
    """Called for every module's version once it is published, and when one is un-confirmed (before it goes back to validated)."""
    if (published, unpublished) not in _listeners:
        _listeners.append((published, unpublished))


def register_pricer(price: Callable) -> None:
    """The Calculations module's pricing function: price(actor, version, job_zip, *, name, acc_version, allocation_version, settings,
    cost_test_set, profiles_8760) starts a run on the job (a local file) for the version and returns the run's id; the run's end calls
    record_calculation. cost_test_set is the published cost-test set (a refdata pack) the filing's setup requires, or None."""
    _pricers[:] = [price]


def register_run_walkthrough(page: Callable[[str], str]) -> None:
    """The Calculations module's measure walkthrough for a pricing run: page(run_id) -> its URL, or "" when the run has none (it did not
    finish, or kept no measure-level table). A filing module's version page links it."""
    _run_walkthroughs[:] = [page]


def run_walkthrough(v: SubmissionVersion) -> str:
    """The measure walkthrough of the run that priced this version ("" for an imported version, one not priced, or no Calculations)."""
    run_id = (v.calculation or {}).get("run")
    if v.imported or not run_id or not _run_walkthroughs:
        return ""
    return _run_walkthroughs[0](str(run_id))


def register_run_state(state: Callable[[str], dict | None]) -> None:
    """The Calculations module's reader: state(run_id) -> {"status", "label", "ended", "error"} for a run, or None for an unknown id."""
    _run_states[:] = [state]


def run_state(run_id: str) -> dict | None:
    """A run's state as the Calculations module reports it ({"status", "label", "ended", "error"}); None for an unknown run."""
    return _run_states[0](run_id) if _run_states and run_id else None


def started_run(v: SubmissionVersion, run_id: str) -> bool:
    """Whether run `run_id` was started to price the version (its submission.pricing_started event names it)."""
    from cedars.services.audit.models import AuditEvent

    return (
        bool(run_id)
        and AuditEvent.objects.filter(
            object_type="submissions.submissionversion", object_id=str(v.pk), action="submission.pricing_started", after__run=str(run_id)
        ).exists()
    )


def pricing_run(v: SubmissionVersion, *, reset_on: tuple[str, ...] = ("submission.files_attached",)) -> dict | None:
    """The pricing run started for the version since its inputs last changed, while no finished run prices them: {"id", "status",
    "label", "ended", "error"}. None when no run was started since the inputs changed, or the version already records its calculation.
    A run that ended without pricing the version (failed, refused, stopped) is returned with ended=True, so a page can say so and offer
    pricing again instead of waiting for it."""
    if v.calculation:
        return None
    from cedars.services.audit.models import AuditEvent

    events = AuditEvent.objects.filter(object_type="submissions.submissionversion", object_id=str(v.pk))
    last = events.filter(action__in=["submission.pricing_started", *reset_on]).order_by("-id").first()
    run_id = (last.after or {}).get("run") if last is not None and last.action == "submission.pricing_started" else None
    if not run_id or run_id == "none":
        return None
    state = _run_states[0](run_id) if _run_states else None
    if state is None:
        return {"id": run_id, "status": "unknown", "label": "unknown", "ended": False, "error": ""}
    return {"id": run_id, **state}


def register_deadline_page(module: str, page: Callable[[Deadline], str]) -> None:
    """A module's page for one of its deadlines: page(deadline) -> its URL, or "" when the module has no page for it (a deadline
    added by hand on the filing calendar, a year not set up). The filing calendar links each deadline through it."""
    _deadline_pages[module] = page


def deadline_page(deadline: Deadline) -> str:
    page = _deadline_pages.get(deadline.module)
    return page(deadline) if page else ""


def register_setup_page(module: str, *, label: str, url_name: str, about: str, order: int = 0) -> None:
    """A module's page where a CPUC system administrator sets up its filings (a budget filing, a claim year, a year of monthly
    reports): setting one up puts its deadlines on the filing calendar, whose Add a filing page lists these pages. `label` names
    what is set up, `about` says in one sentence what it puts on the calendar."""
    _setup_pages[module] = {"module": module, "label": label, "url_name": url_name, "about": about, "order": order}


def setup_pages() -> list[dict]:
    """Every module's set-up page (register_setup_page), in their order."""
    return sorted(_setup_pages.values(), key=lambda p: (p["order"], p["label"]))


def register_nothing_to_file(module: str, check: Callable[[Deadline], bool]) -> None:
    """A module's deadlines that take no filing: check(deadline) -> True for one that only marks a date (the 13th-month report,
    generated from claims). No filing starts for it, and the Filings page's status leaves it out."""
    _nothing_to_file[module] = check


def nothing_to_file(deadline: Deadline) -> bool:
    check = _nothing_to_file.get(deadline.module)
    return bool(check and check(deadline))


def register_filers(module: str, filers: Callable[[Deadline], set[int] | None]) -> None:
    """Which organizations file for one of a module's deadlines: filers(deadline) -> their ids, or None for every program administrator.
    The Filings page shows the others as having nothing to file (statewide claims: only the statewide programs' lead PAs file them)."""
    _filers[module] = filers


def expected(deadline: Deadline) -> set[int] | None:
    """The program administrators the CPUC expects to file for the deadline, as set on its set-up page (`set_filers`), by id; None
    when none are listed (every PA files)."""
    ids = {o.pk for o in deadline.filers.all()}
    return ids or None


def on_filing(deadline: Deadline, organisation: Organisation) -> bool:
    """Whether the organization is one of the PAs on the filing (every PA, unless its set-up lists who files)."""
    ids = expected(deadline)
    return ids is None or organisation.pk in ids


def files_for(deadline: Deadline, organisation: Organisation) -> bool:
    """Whether the organization is one that files for the deadline: one of the PAs on the filing (`on_filing`) and, for a module that
    narrows it further (register_filers; statewide claims: the statewide programs' lead PAs), one of the module's filers."""
    if not on_filing(deadline, organisation):
        return False
    filers = _filers.get(deadline.module)
    ids = filers(deadline) if filers else None
    return ids is None or organisation.pk in ids


def register_stream(module: str, stream: Callable[[Deadline], str]) -> None:
    """A module whose deadlines of one period fall in separate runs of filings: stream(deadline) -> the run's name ("" the main one).
    A PA's statewide claims stand beside its own claims of the same year, so the snapshots keep a slot for each (`stream`)."""
    _streams[module] = stream


def stream(deadline: Deadline) -> str:
    fn = _streams.get(deadline.module)
    return fn(deadline) if fn else ""


def register_setup_groups(module: str, groups: Callable[[], list[tuple[str, list[int], Callable[..., None]]]]) -> None:
    """A module's set-ups and the deadlines each put on the calendar, for clear_calendar: groups() -> [(what, deadline ids,
    remove)], where remove(actor, reason) deletes the set-up's own records, audited (called just before its deadlines are deleted)."""
    _setup_groups[module] = groups


def register_setup_link(module: str, link: Callable[[Deadline], str]) -> None:
    """A module's set-up page for one of its deadlines: link(deadline) -> the URL of the set-up it belongs to (a claim year's, a year
    of monthly reports', a budget filing's), or "" for one no set-up put on the calendar. The calendar's pages and Filing sets link
    there, so a CPUC system administrator edits a filing set from wherever they meet one of its filings."""
    _setup_links[module] = link


def setup_link(deadline: Deadline) -> str:
    link = _setup_links.get(deadline.module)
    return link(deadline) if link else ""


def filing_sets() -> list[dict]:
    """Every filing set on the calendar (each module set-up: a budget filing, a claim year, a year of monthly reports), for the Filing
    sets page: what it is, its module, its set-up page, its filings in due order, how many are canceled and whether any organization
    has started filing for it."""
    labels = {p["module"]: p["label"] for p in setup_pages()}
    out = []
    for module, fn in sorted(_setup_groups.items()):
        for what, ids, _remove in fn():
            deadlines = list(Deadline.objects.filter(pk__in=list(ids)).select_related("period", "cycle").order_by("due_at", "pk"))
            if not deadlines:
                continue
            active = [d for d in deadlines if d.retired_at is None]
            coming = [d for d in active if d.due_at > timezone.now()]
            out.append(
                {
                    "module": module,
                    "kind": labels.get(module, module),
                    "what": what,
                    "url": setup_link(deadlines[0]),
                    "deadlines": deadlines,
                    "canceled": len(deadlines) - len(active),
                    "next_due": coming[0] if coming else None,
                    "last_due": deadlines[-1].due_at,
                    "started": filed(ids),
                }
            )
    return sorted(out, key=lambda s: (-s["last_due"].timestamp(), s["what"]))


@transaction.atomic
def clear_calendar(actor, reason: str, *, apply: bool = False) -> dict:
    """Empty the filing calendar, for a demo or sandbox site (manage.py clear_filing_calendar). Each module set-up (a budget filing,
    a claim year, a year of monthly reports) and each deadline added by hand is taken as a whole: when no organization has filed for
    any of its deadlines, its set-up and deadlines are deleted; otherwise its deadlines are retired (off the calendar, their filings
    kept and readable, restorable). Every deletion and retirement is audited with the reason. Without apply, nothing changes and the
    plan is returned."""
    _calendar_admin(actor)
    if not (reason or "").strip():
        raise AccessDenied("Give the reason for clearing the calendar.")
    groups, grouped = [], set()
    for _module, fn in sorted(_setup_groups.items()):
        for what, ids, remove in fn():
            groups.append((what, list(ids), remove))
            grouped.update(ids)
    for d in Deadline.objects.exclude(pk__in=grouped).select_related("period", "cycle").order_by("due_at"):
        groups.append((f"{d.module} {d.title}, {d.period or d.cycle} (added by hand)", [d.pk], None))
    every = {pk for _, ids, _ in groups for pk in ids}
    filed_ids = set(Submission.objects.filter(deadline_id__in=every).values_list("deadline_id", flat=True))
    plan = {"deleted": [], "retired": []}
    now = timezone.now()
    for what, ids, remove in groups:
        if any(pk in filed_ids for pk in ids):
            active = list(Deadline.objects.filter(pk__in=ids, retired_at__isnull=True))
            plan["retired"].append((what, len(active)))
            if apply:
                for d in active:
                    d.retired_at, d.retired_reason = now, reason.strip()
                    d.save(update_fields=["retired_at", "retired_reason"])
                    audit.record("deadline.retired", obj=d, after={"retired_at": _when(now)}, reason=reason.strip(), actor=actor)
        else:
            plan["deleted"].append((what, len(ids)))
            if apply:
                _delete_setup(actor, ids, remove, reason.strip())
    return plan


def _delete_setup(actor, ids, remove, reason: str) -> None:
    if remove is not None:
        remove(actor, reason)
    for d in Deadline.objects.filter(pk__in=ids):
        before = deadline_fields(d) | {"module": d.module, "kind": d.kind}
        audit.record("deadline.deleted", obj=d, before=before, reason=reason, actor=actor)
        d.delete()


def filed(deadline_ids) -> bool:
    """Whether any organization has started filing for any of these deadlines."""
    return Submission.objects.filter(deadline_id__in=list(deadline_ids)).exists()


@transaction.atomic
def remove_setup(actor, deadline_ids, remove, reason: str) -> None:
    """Take one set-up (a budget filing, a claim year, a year of monthly reports) off the calendar for good, from its set-up page: its
    own records (remove(actor, reason), the module's) and its deadlines are deleted, each audited with the reason. Only while no
    organization has started filing for any of its deadlines; once one has, the set-up stays (what was filed is kept)."""
    _calendar_admin(actor)
    if not (reason or "").strip():
        raise AccessDenied("Give the reason for removing the set-up.")
    ids = list(Deadline.objects.select_for_update().filter(pk__in=list(deadline_ids)).values_list("pk", flat=True))
    if filed(ids):
        raise AccessDenied("An organization has started filing for this set-up, so it stays: what was filed is kept.")
    _delete_setup(actor, ids, remove, reason.strip())


def filing_pas():
    """The program administrators a set-up may list as its filers: every active one that files (not the CPUC or the SW pseudo-PA)."""
    return [o for o in Organisation.objects.filter(kind=Organisation.Kind.PA, active=True).order_by("code") if o.files]


def started_by(deadline_ids) -> set[int]:
    """The organizations that have started filing for any of these deadlines, by id."""
    return set(Submission.objects.filter(deadline_id__in=list(deadline_ids)).values_list("organisation_id", flat=True))


def started_counts(deadline_ids) -> dict[int, int]:
    """How many organizations have started filing for each of these deadlines, by deadline id (those none has started left out)."""
    from django.db.models import Count

    rows = Submission.objects.filter(deadline_id__in=list(deadline_ids)).values("deadline_id").annotate(n=Count("pk"))
    return {r["deadline_id"]: r["n"] for r in rows}


@transaction.atomic
def set_filers(actor, deadlines, organisations, reason: str) -> list[Deadline]:
    """Set which program administrators file for a set-up's deadlines (a budget filing, a claim year's own claims, a year of monthly
    reports), from its set-up page or as it is set up (Jake, 2026-11-09: not every PA files every filing). Each deadline whose list
    changes gets the audit event deadline.filers_changed with the PAs before and after. At least one PA files; a PA that has started
    filing for one of the deadlines stays on it (what it filed is kept). The Filings page's status, the "Choose a program
    administrator" lists and `start` follow the list. Returns the deadlines that changed."""
    _calendar_admin(actor)
    if not (reason or "").strip():
        raise AccessDenied("Give the reason for the change.")
    orgs = {o.pk: o for o in organisations}
    if not orgs:
        raise AccessDenied("Choose at least one program administrator to file.")
    bad = sorted(o.code for o in orgs.values() if not (o.files and o.active))
    if bad:
        raise AccessDenied(f"Only active program administrators file: {', '.join(bad)} cannot be on a filing.")
    deadlines = list(deadlines)
    started = started_by(d.pk for d in deadlines)
    every = {o.pk for o in filing_pas()}
    changed = []
    for d in Deadline.objects.select_for_update().filter(pk__in=[d.pk for d in deadlines]).order_by("due_at", "pk"):
        before = {o.pk: o for o in d.filers.all()}
        before_ids = set(before) or every
        dropped = sorted(o.code for o in Organisation.objects.filter(pk__in=(before_ids - set(orgs)) & started))
        if dropped:
            raise AccessDenied(
                f"{', '.join(dropped)} {'has' if len(dropped) == 1 else 'have'} started filing for {d.title}, "
                f"{d.period or d.cycle}, so {'it stays' if len(dropped) == 1 else 'they stay'} on it: what was filed is kept."
            )
        if set(before) == set(orgs):
            continue
        d.filers.set(orgs.values())
        audit.record(
            "deadline.filers_changed",
            obj=d,
            before={"filers": sorted(o.code for o in before.values()) or "every PA"},
            after={"filers": sorted(o.code for o in orgs.values())},
            reason=reason.strip(),
            actor=actor,
        )
        changed.append(d)
    return changed


@transaction.atomic
def move_due_dates(actor, moves, reason: str) -> list[Deadline]:
    """Move several deadlines' due dates at once, from a set-up page: moves is [(deadline, new due moment)]. Each is change_deadline
    (the audit event deadline.moved, its subscribers told, reminders sent again), its opening date and notes kept. Only deadlines
    still to come move here; a past one moves on its own page of the filing calendar. All or none: one refused moves none."""
    if not (reason or "").strip():
        raise AccessDenied("Give the reason for the change.")
    now, moved = timezone.now(), []
    for d, due_at in moves:
        if due_at == d.due_at:
            continue
        what = f"{d.title}, {d.period or d.cycle}"
        if d.due_at <= now:
            raise AccessDenied(f"{what} was already due; move it on its page of the filing calendar.")
        try:
            moved.append(change_deadline(actor, d, due_at=due_at, opens_at=d.opens_at, notes=d.notes, reason=reason))
        except AccessDenied as e:
            raise AccessDenied(f"{what}: {e}") from e
    return moved


def register_reader(module: str, can_read: Callable[..., bool]) -> None:
    """A filing module's rule for who may read a filing that is not published: can_read(user, submission) (the filing organization's
    readers and CPUC staff). The Filings page's status of every PA's filings (submissions.status) shows the rest only whether a filing
    is published."""
    _readers[module] = can_read


def can_read_filing(user, submission: Submission) -> bool:
    """Whether the user may read the submission's unpublished versions and where it stands, by its module's rule (none: CPUC system
    administrators only)."""
    reader = _readers.get(submission.deadline.module)
    if reader is not None:
        return reader(user, submission)
    return access.is_system_admin(user)


def register_version_page(module: str, page: Callable[[SubmissionVersion], str]) -> None:
    """A filing module's page for one version: page(version) -> its URL. Pages about a version's pricing run (Calculations' measure
    walkthrough) link back to the filing through it."""
    _version_pages[module] = page


def version_page(v: SubmissionVersion) -> str:
    page = _version_pages.get(v.submission.deadline.module)
    return page(v) if page else ""


def can_read_version(user, v: SubmissionVersion) -> bool:
    """A published version is open to every signed-in user (as each filing module's version page has it); the rest only to those its
    module lets read the filing."""
    if not user.is_authenticated:
        return False
    return v.status == SubmissionVersion.Status.PUBLISHED or can_read_filing(user, v.submission)


def versions_priced_by(user, run_id) -> list[SubmissionVersion]:
    """The filed versions (claims, budget filings, monthly reports) whose results came from this calculation run and that the user may
    read, newest first. Their readers may follow the run's measures (Calculations' measure walkthrough) although the run itself
    belongs to the filing organization."""
    qs = (
        SubmissionVersion.objects.filter(origin=SubmissionVersion.Origin.FILED, calculation__run=str(run_id))
        .select_related("submission__deadline", "submission__organisation")
        .order_by("-pk")
    )
    return [v for v in qs if can_read_version(user, v)]


def register_acc_needs(module: str, needs: Callable) -> None:
    """A filing module's set-ups still to be filed: needs() -> [(what, ACC year)], e.g. ("2028 claims", "2026"), for each set-up with a
    deadline still to come. The Engine releases page warns when the default release lacks one (UAT 2, U05)."""
    _acc_needs[module] = needs


def acc_needs() -> list[tuple[str, str]]:
    """Every filing module's set-ups still to be filed, with the ACC each is priced on."""
    return [need for module in sorted(_acc_needs) for need in _acc_needs[module]()]


def register_claims_totals(totals: Callable) -> None:
    """The Claims module's reader: totals(organisation, year) -> [{prg_id, pa, year, quarters, final, records, expenditures, incentives,
    gross_kwh, gross_kw, gross_therm, net_kwh, net_kw, net_therm, tsb}], one per program, from the year to date of the PA's latest
    confirmed claims (quarters: the quarters confirmed; final: the annual confirmation is)."""
    _claims_totals[:] = [totals]


def claims_totals(organisation: Organisation, year: int) -> list[dict]:
    """A PA's confirmed claims for a year by program; [] when Claims is not installed or nothing is confirmed."""
    return _claims_totals[0](organisation, year) if _claims_totals else []


def register_restater(restate: Callable, options: Callable, describe: Callable) -> None:
    """The Calculations module's restater: restate(actor, organisation, version, job_zip, *, name, release, acc_version,
    record_accs, allocation_version, settings, cost_test_set, profiles_8760) starts an analysis run on the job (a local file) for
    `organisation` (None: the actor's own) and returns its id; the run is never recorded on the version. options() -> [{"release",
    "default", "accs"}], the active releases that offer ReCET's own calculation and the ACCs each has. describe(run_id) -> {"status",
    "label", "ended", "error", "url", "release", "acc", "allocation", "cost_tests", "programs", "results"} for a run, or None once it
    is gone (expired)."""
    _restaters[:] = [(restate, options, describe)]


def restated_run(run_id: str) -> dict | None:
    """What a restatement page shows about one of its runs (see register_restater); None for a run that no longer exists."""
    return _restaters[0][2](run_id) if _restaters and run_id else None


def restate_options() -> list[dict]:
    """The releases a restatement may use, default first; [] when Calculations is not installed."""
    return _restaters[0][1]() if _restaters else []


def restate(actor, v: SubmissionVersion, job_zip, *, organisation, name: str, release: str, acc_version: str, **kw) -> str:
    """Start a restated run of a published version's job: an analysis run for `organisation`, on `release` and `acc_version`
    (`record_accs`: the ACCs records name in the job's ACCVersion column). The version, its calculation and its results never change;
    the audit trail records the restatement on it. Returns the run's id."""
    v = SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation").get(pk=v.pk)
    if v.status != SubmissionVersion.Status.PUBLISHED:
        raise AccessDenied("Only a confirmed (published) version is restated; a version in progress is priced for confirmation instead.")
    if v.imported:
        raise AccessDenied("This version was imported from legacy CEDARS: it has no job of measure inputs to price again.")
    if not _restaters:
        raise AccessDenied("The calculation service is not installed here, so filings cannot be restated.")
    run = _restaters[0][0](actor, organisation, v, job_zip, name=name, release=release, acc_version=acc_version, **kw)
    audit.record(
        "submission.restatement_started",
        obj=v,
        after={"run": str(run), "release": release, "acc": acc_version, "for": organisation.code if organisation else None},
        actor=actor,
        organisation=organisation,
    )
    return str(run)


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


def filers(deadline: Deadline, can_read: Callable[[Submission], bool]) -> list[Submission]:
    """The PAs' submissions for a deadline that can_read allows, by PA code: what a page shown to the CPUC (or anyone not acting for a PA)
    offers to choose from, with ?org=CODE, instead of speaking of the acting organization's own filing."""
    subs = Submission.objects.filter(deadline=deadline).select_related("organisation").order_by("organisation__code")
    return [s for s in subs if s.organisation.files and can_read(s)]


def filer_statuses(deadline: Deadline, can_read: Callable[[Submission], bool], *, every: bool = False) -> list[dict]:
    """For the "Choose a program administrator" list (navigation review D8, L8): each PA with where its filing for the deadline stands
    ({"organisation", "status", "link"}), by code. With every (CPUC staff), every active PA on the filing is listed, one that has not
    started as "Not started"; otherwise only the filings can_read allows, as filers() gives them. Only a filing the reader may
    open is linked."""
    subs = {s.organisation_id: s for s in Submission.objects.filter(deadline=deadline).select_related("organisation")}
    rows = {}
    for s in subs.values():
        readable = s.organisation.files and can_read(s)
        if readable or (every and s.organisation.files):
            v = latest(s)
            rows[s.organisation.code] = {
                "organisation": s.organisation,
                "status": v.get_status_display() if v else "Started",
                "link": readable,
            }
    if every:
        for o in Organisation.objects.filter(kind=Organisation.Kind.PA, active=True):
            if o.files and o.code not in rows and on_filing(deadline, o):
                rows[o.code] = {"organisation": o, "status": "Not started", "link": False}
    return [rows[c] for c in sorted(rows)]


def latest(submission: Submission) -> SubmissionVersion | None:
    return submission.versions.order_by("-number").first()


def current(submission: Submission) -> SubmissionVersion | None:
    """The published version that stands: what dashboards, downloads and later modules read."""
    return submission.versions.filter(status=SubmissionVersion.Status.PUBLISHED).order_by("-number").first()


def standing_numbers(submission_ids) -> dict[int, tuple[int | None, int]]:
    """{submission id: (the number of its standing published version or None, its latest version's number)} in one query, for pages
    that list many submissions' versions at once (the program page)."""
    published = SubmissionVersion.Status.PUBLISHED
    rows = (
        SubmissionVersion.objects.filter(submission_id__in=list(submission_ids))
        .values("submission_id")
        .annotate(latest=Max("number"), standing=Max("number", filter=Q(status=published)))
    )
    return {r["submission_id"]: (r["standing"], r["latest"]) for r in rows}


# --- calendar


CALENDAR_ONLY = "Only a CPUC system administrator sets the filing calendar."


def _calendar_admin(actor) -> None:
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied(CALENDAR_ONLY)


def _when(value) -> str | None:
    return value.isoformat() if value else None


def deadline_fields(deadline: Deadline) -> dict:
    """What a CPUC system administrator changes on a deadline, as its audit events record it: its name, its program years (a filing
    on a cycle), its dates and its notes."""
    out = {"name": deadline.title}
    if deadline.cycle_id:
        out["years"] = deadline.cycle.slug
    return out | {"due_at": _when(deadline.due_at), "opens_at": _when(deadline.opens_at), "notes": deadline.notes}


@transaction.atomic
def add_deadline(
    actor, *, module: str, kind: str, due_at, period=None, cycle=None, opens_at=None, notes: str = "", name: str = ""
) -> Deadline:
    """Put a deadline on the filing calendar. The modules call it as a CPUC system administrator sets up a claim year, a year of
    monthly reports or a budget filing, so the calendar fills from those set-ups; on the calendar it is then changed, not added."""
    _calendar_admin(actor)
    name = (name or "").strip()
    if cycle is not None:
        same = same_filing(module, cycle, name or kind)
    else:
        same = Deadline.objects.filter(module=module, kind=kind, period=period, cycle=None).first()
    if same is not None:
        if same.retired_at:
            raise AccessDenied(f"{same} was canceled; restore it from its page instead of adding it again.")
        raise AccessDenied(f"The calendar already has {same}; change its dates on its page.")
    d = Deadline.objects.create(
        module=module, kind=kind, name=name, period=period, cycle=cycle, due_at=due_at, opens_at=opens_at, notes=notes
    )
    after = {"module": module, "kind": kind, "due_at": due_at.isoformat(), "opens_at": _when(opens_at), "notes": notes}
    if name:
        after["name"] = name
    if cycle is not None:
        after["years"] = cycle.slug
    audit.record("deadline.created", obj=d, after=after, actor=actor)
    return d


@transaction.atomic
def move_deadline(actor, deadline: Deadline, due_at, reason: str) -> Deadline:
    """Move a deadline's due date (change_deadline with its other values kept)."""
    return change_deadline(actor, deadline, due_at=due_at, opens_at=deadline.opens_at, notes=deadline.notes, reason=reason)


def same_filing(module: str, cycle, title: str, exclude=None) -> Deadline | None:
    """The module's filing on a cycle called `title` (its name, else its kind; case aside), if there is one. A cycle holds one filing
    of each name, so two advice letters (a TUAL and an MCAL) are told apart by their names."""
    qs = Deadline.objects.filter(module=module, cycle=cycle, period=None)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    title = (title or "").strip().lower()
    return next((d for d in qs if d.title.strip().lower() == title), None)


def renamable(deadline: Deadline) -> bool:
    """Whether a CPUC system administrator may rename the deadline and change its program years: a filing on a cycle (a budget
    filing). A claim's or a monthly report's deadline is named by what is due in its period, which its module reads."""
    return deadline.cycle_id is not None


MAX_CYCLE_YEARS = 20


@transaction.atomic
def change_deadline(
    actor,
    deadline: Deadline,
    *,
    due_at,
    opens_at=None,
    notes: str = "",
    reason: str,
    name: str | None = None,
    years: tuple[int, int] | None = None,
) -> Deadline:
    """Change a deadline's due date, opening date or notes, with a reason; for a filing on a cycle (renamable), also its name and its
    program years, the years only while no organization has started filing for it (what is filed is checked against them). A new
    due date is the audit event deadline.moved (so its subscribers hear of it and reminders are sent again for the new date); any
    other change is deadline.changed. What is due and a period's deadline's period never change."""
    _calendar_admin(actor)
    if not (reason or "").strip():
        raise AccessDenied("Give the reason for the change.")
    deadline = Deadline.objects.select_for_update(of=("self",)).select_related("cycle").get(pk=deadline.pk)
    if deadline.retired_at:
        raise AccessDenied("This filing is canceled; restore it before changing it.")
    if opens_at and opens_at >= due_at:
        raise AccessDenied("A filing opens before it is due: choose an opening date before the due date.")
    before = deadline_fields(deadline)
    before_cycle, before_name = deadline.cycle_id, deadline.name
    if name is not None and renamable(deadline):
        name = name.strip()
        deadline.name = "" if name == deadline.kind else name  # blank: called by what is due
    if years is not None and renamable(deadline) and tuple(years) != (deadline.cycle.first_year, deadline.cycle.last_year):
        first, last = years
        if last < first:
            raise AccessDenied("A filing's last program year is on or after its first.")
        if last - first + 1 > MAX_CYCLE_YEARS:
            raise AccessDenied(f"A filing covers at most {MAX_CYCLE_YEARS} program years.")
        started = deadline.submissions.count()
        if started:
            orgs = "organization has" if started == 1 else "organizations have"
            raise AccessDenied(f"{started} {orgs} started this filing, so its program years no longer change.")
        from cedars.services.registry import service as registry  # registry imports this service, so here, not at the top

        deadline.cycle = registry.ensure_cycle(first, last_year=last)
    if deadline.cycle_id and (deadline.cycle_id, deadline.name) != (before_cycle, before_name):
        if same_filing(deadline.module, deadline.cycle, deadline.title, exclude=deadline) is not None:
            raise AccessDenied(f"The {deadline.cycle} cycle already has a filing called {deadline.title}.")
    moved = deadline.due_at != due_at
    deadline.due_at, deadline.opens_at, deadline.notes = due_at, opens_at, notes or ""
    after = deadline_fields(deadline)
    if before == after:
        return deadline
    deadline.save()
    changed = [k for k in after if before[k] != after[k]]
    audit.record(
        "deadline.moved" if moved else "deadline.changed",
        obj=deadline,
        before={k: before[k] for k in changed},
        after={k: after[k] for k in changed},
        reason=reason.strip(),
        actor=actor,
    )
    return deadline


@transaction.atomic
def retire_deadline(actor, deadline: Deadline, reason: str) -> Deadline:
    """Cancel one filing (take its deadline off the calendar), with a reason: one quarter's claim, one month's report or one budget
    filing of a filing set the CPUC no longer asks for (Jake, 2026-11-09: a single filing of any filing set, from its set-up page or
    the calendar), or a deadline added by hand by mistake. Only while no organization has started filing for it (what was filed is
    kept). The deadline stays, with its history and the rest of its set, and no filing can start for it until it is restored; the
    calendar, reminders, the Filings page's status and its module's pages leave it out or mark it canceled."""
    _calendar_admin(actor)
    if not (reason or "").strip():
        raise AccessDenied("Give the reason for canceling the filing.")
    deadline = Deadline.objects.select_for_update().get(pk=deadline.pk)
    if deadline.retired_at:
        return deadline
    started = deadline.submissions.count()
    if started:
        orgs = "organization has" if started == 1 else "organizations have"
        raise AccessDenied(
            f"{started} {orgs} started filing {deadline.title}, {deadline.period or deadline.cycle}, so it cannot be canceled: "
            "what was filed is kept. Change its due date instead."
        )
    deadline.retired_at, deadline.retired_reason = timezone.now(), reason.strip()
    deadline.save(update_fields=["retired_at", "retired_reason"])
    audit.record("deadline.retired", obj=deadline, after={"retired_at": _when(deadline.retired_at)}, reason=reason.strip(), actor=actor)
    return deadline


@transaction.atomic
def restore_deadline(actor, deadline: Deadline, reason: str) -> Deadline:
    """Put a canceled filing's deadline back on the calendar, with a reason."""
    _calendar_admin(actor)
    if not (reason or "").strip():
        raise AccessDenied("Give the reason for restoring the filing.")
    deadline = Deadline.objects.select_for_update().get(pk=deadline.pk)
    if not deadline.retired_at:
        return deadline
    before = {"retired_at": _when(deadline.retired_at)}
    deadline.retired_at, deadline.retired_reason = None, ""
    deadline.save(update_fields=["retired_at", "retired_reason"])
    audit.record("deadline.restored", obj=deadline, before=before, reason=reason.strip(), actor=actor)
    return deadline


# --- the first version and drafts


@transaction.atomic
def start(actor, deadline: Deadline, organisation: Organisation) -> SubmissionVersion:
    """Open a submission for the organization (version 1, draft). Starting again returns the open draft."""
    if deadline.retired_at:
        raise AccessDenied("This deadline was taken off the filing calendar, so no filing starts for it.")
    if nothing_to_file(deadline):
        raise AccessDenied(f"Nothing is filed for the {deadline.title}: it marks a date on the calendar.")
    if not on_filing(deadline, organisation) and not Submission.objects.filter(deadline=deadline, organisation=organisation).exists():
        raise AccessDenied(
            f"{organisation.code} is not one of the PAs filing {deadline.title}: the CPUC lists who files on the filing's set-up page."
        )
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


def price(actor, v: SubmissionVersion, job_zip, **options):
    """Start the calculation and statewide allocation run on a validated version's job (priced before confirmation). Returns
    the run's id; when it finishes, the Calculations module records it here (record_calculation) and the version can be confirmed.
    Options: name, acc_version, allocation_version, settings, cost_test_set and `profiles_8760` (a supplied 8,760-profile file the
    job's records name: a claims true-up's parent profiles). A refusal the CPUC's set-up causes (PricingUnavailable) is
    recorded and sent to the system administrators outside the refused transaction, and the filer is told in their words."""
    try:
        return _price(actor, v, job_zip, **options)
    except PricingUnavailable as e:
        _pricing_unavailable(actor, v, str(e))
        raise AccessDenied(str(e) if access.is_system_admin(actor) else PRICING_UNAVAILABLE) from e


@transaction.atomic
def _price(
    actor,
    v: SubmissionVersion,
    job_zip,
    *,
    name: str,
    acc_version: str,
    allocation_version=None,
    settings: dict | None = None,
    cost_test_set=None,
    profiles_8760: bytes | None = None,
):
    v = SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation").get(pk=v.pk)
    _require(actor, v.submission, "submit")
    _refuse_imported(v.submission)
    if v.status != SubmissionVersion.Status.VALIDATED:
        raise AccessDenied("Only a validated version is priced for confirmation.")
    if not _pricers:
        raise AccessDenied("The calculation service is not installed here, so filings cannot be priced.")
    run = _pricers[0](
        actor,
        v,
        job_zip,
        name=name,
        acc_version=acc_version,
        allocation_version=allocation_version,
        settings=settings or {},
        cost_test_set=cost_test_set,
        **({"profiles_8760": profiles_8760} if profiles_8760 else {}),
    )
    audit.record("submission.pricing_started", obj=v, after={"run": str(run), "acc": acc_version}, actor=actor)
    return str(run)


def _pricing_unavailable(actor, v: SubmissionVersion, reason: str) -> None:
    """Record a refused pricing and tell each CPUC system administrator once per version and reason."""
    from cedars.services.accounts import permissions as perms
    from cedars.services.notifications import service as notifications

    audit.record("submission.pricing_unavailable", obj=v, after={"reason": reason}, actor=actor)
    cpuc = access.cpuc()
    if cpuc is None:
        return
    who = f"{v.submission.organisation.code}, {v.submission.deadline}, version {v.number}"
    key = f"pricing-unavailable-{v.pk}-{hashlib.sha256(reason.encode()).hexdigest()[:12]}"
    for admin in access.holders(cpuc, perms.SYSTEM_ADMIN):
        notifications.notify(
            admin, f"A filing cannot be priced: {who}", f"{reason}\n\nThe filer was told pricing is not set up yet.", key=key
        )


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
    """After the due date: ask to correct this organization's filing for this deadline only. Under an "open" policy the new draft
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
    """The approver the actor acts for: themselves if they hold `<module>.approve` in the CPUC organization, else the approver who
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


def pending_corrections(actor, module: str) -> dict[int, int] | None:
    """The correction requests waiting for a decision on each of the module's deadlines (deadline id -> count), for an approver of the
    module or their delegate; None for anyone else (navigation review, D1: the index pages show approvers what waits)."""
    if approver_for(actor, module) is None:
        return None
    rows = (
        ReopenRequest.objects.filter(status=ReopenRequest.Status.PENDING, submission__deadline__module=module)
        .values("submission__deadline")
        .annotate(n=Count("pk"))
    )
    return {r["submission__deadline"]: r["n"] for r in rows}


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


def _delete_comment_files(version_ids) -> None:
    """The files attached to comments on versions being removed (comments.py keeps them under the version)."""
    from cedars.core import storage

    store = storage.store("uploads")
    for pk in version_ids:
        store.delete_prefix(f"submissions/versions/{pk}/")


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
            pk = v.pk
            v.delete()  # its comments go with it; so do their files
            _delete_comment_files([pk])
            if not submission.versions.exists() and not submission.reopen_requests.exists():
                submission.delete()

        yield f"submission_version:{v.pk}", delete


def expired_sandbox_filings(cutoff):
    """In the sandbox only: whole submissions (every version, reopen request) started before the cutoff (record class "sandbox_data"),
    except the published filings a sandbox refresh copied from production."""
    from django.conf import settings

    if settings.ENVIRONMENT != "sandbox":
        return
    from cedars.services.sandbox import bundle

    subs = list(Submission.objects.filter(created_at__lt=cutoff).select_related("organisation"))
    copied = bundle.is_copied("submissions.submission", [s.pk for s in subs])  # production's, copied by a refresh: kept
    for sub in (s for s in subs if str(s.pk) not in copied):

        def delete(sub=sub):
            audit.record("submission.sandbox_expired", obj=sub, organisation=sub.organisation)
            sub.reopen_requests.all().delete()
            ids = list(sub.versions.values_list("pk", flat=True))
            sub.versions.all().delete()
            _delete_comment_files(ids)
            sub.delete()

        yield f"submission:{sub.pk}", delete
