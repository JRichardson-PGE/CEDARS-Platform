"""Program registry rules. Every change is audited; versions and confirmed allocation tables are never edited.

Effective dates: a program's attributes for program year Y are the version in force on 31 December of Y, or, for a program that ended
during Y, its last version that overlapped Y (`version_for_year`). Any date: `version_on`.
"""

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.submissions.service import approver_for

from .models import (
    AllocationEntry,
    AllocationShare,
    AllocationVersion,
    FilingCycle,
    Program,
    ProgramChangePolicy,
    ProgramChangeRequest,
    ProgramVersion,
    ReportingPeriod,
)

VERSION_FIELDS = [
    "name",
    "commercial_name",
    "status",
    "segment",
    "primary_sector",
    "sectors",
    "implementer",
    "category",
    "implementation_contractor",
    "start_year",
    "end_year",
    "flags",
    "statewide_program",
    "parent_program",
    "exclude_from_budget",
    "exclude_from_ce",
    "exclude_from_statewide",
    "website",
]
FUNDING_PAS = ("PGE", "SCE", "SCG", "SDGE")  # the IOUs that fund statewide programs, as the factor files have them
SUM_TOLERANCE = Decimal("0.0005")


# --- reading


def version_on(program: Program, day: date) -> ProgramVersion | None:
    return (
        program.versions.filter(valid_from__lte=day).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=day)).order_by("-valid_from").first()
    )


def version_for_year(program: Program, year: int) -> ProgramVersion | None:
    last_day = version_on(program, date(year, 12, 31))
    if last_day is not None:
        return last_day
    return (
        program.versions.filter(valid_from__lte=date(year, 12, 31))
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=date(year, 1, 1)))
        .order_by("-valid_from")
        .first()
    )


def current_version(program: Program) -> ProgramVersion | None:
    return version_on(program, timezone.localdate())


def _in_force(versions, day: date) -> ProgramVersion | None:
    """version_on over versions already read (newest start first)."""
    return next((v for v in versions if v.valid_from <= day and (v.valid_to is None or v.valid_to >= day)), None)


def _for_year(versions, year: int) -> ProgramVersion | None:
    """version_for_year over versions already read (newest start first)."""
    last_day = _in_force(versions, date(year, 12, 31))
    if last_day is not None:
        return last_day
    first, last = date(year, 1, 1), date(year, 12, 31)
    return next((v for v in versions if v.valid_from <= last and (v.valid_to is None or v.valid_to >= first)), None)


def catalog(year: int | None = None):
    """(program, version) for every program not flagged as a portfolio cost, closed programs included (BR-13.5): the version for the
    program year, or with no year the version in force today (else the latest). Two queries, whatever the number of programs."""
    out = []
    today = timezone.localdate()
    programs = Program.objects.filter(portfolio_cost=False).select_related("administrator")
    for p in programs.prefetch_related(Prefetch("versions", queryset=ProgramVersion.objects.order_by("-valid_from"))):
        versions = list(p.versions.all())
        v = _for_year(versions, year) if year else (_in_force(versions, today) or (versions[0] if versions else None))
        out.append((p, v))
    return out


def can_edit(actor, program: Program) -> bool:
    """Whether actor may change the program: a CPUC system administrator, or its administrator's org admins and programs.admin holders."""
    if actor is None or access.is_system_admin(actor):
        return True
    org = program.administrator
    return org is not None and (access.is_org_admin(actor, org) or access.has_permission(actor, org, "programs.admin"))


# --- changing programs


@transaction.atomic
def create_program(
    actor,
    *,
    prg_id: str,
    administrator: Organisation | None,
    valid_from: date,
    statewide=False,
    portfolio_cost=False,
    reason: str = "",
    **fields,
) -> Program:
    if administrator is None and not statewide:
        raise AccessDenied("A program needs an administrator unless it is statewide.")
    if actor is not None and not (
        access.is_system_admin(actor) or (administrator and can_edit(actor, Program(administrator=administrator)))
    ):
        raise AccessDenied("Only the program's administrator or the CPUC can add it.")
    p = Program.objects.create(prg_id=prg_id.strip(), administrator=administrator, statewide=statewide, portfolio_cost=portfolio_cost)
    v = ProgramVersion.objects.create(program=p, valid_from=valid_from, created_by=actor, reason=reason, **_clean(fields))
    audit.record(
        "program.created",
        obj=p,
        after={
            "prg_id": p.prg_id,
            "administrator": administrator.code if administrator else None,
            "version": audit.snapshot(v, ["valid_from", *VERSION_FIELDS]),
        },
        reason=reason,
        actor=actor,
    )
    return p


def _clean(fields: dict) -> dict:
    unknown = set(fields) - set(VERSION_FIELDS)
    if unknown:
        raise ValueError(f"not program attributes: {', '.join(sorted(unknown))}")
    return fields


@transaction.atomic
def change_program(actor, program: Program, *, effective: date, reason: str, **changes) -> ProgramVersion:
    """Start a new version on `effective` with `changes` applied to the latest version; the version before it ends the day before.
    The date may be in the future (a resolution entered before its effective date); it cannot be on or before the latest version's
    start, because versions already in force are history. A change to a locked attribute is a request instead (request_change)."""
    if not can_edit(actor, program):
        raise AccessDenied(
            f"Only {program.administrator.code if program.administrator else 'the CPUC'}'s administrators can change {program}."
        )
    _clean(changes)
    held = needs_approval(actor, changes)
    if held:
        raise AccessDenied(f"{_names(held)} {'is' if len(held) == 1 else 'are'} locked: request the change and a CPUC approver decides it.")
    return _start_version(actor, program, effective=effective, reason=reason, changes=changes, created_by=actor)


def _latest(program: Program, effective: date, *, lock: bool = False) -> ProgramVersion:
    qs = program.versions.select_for_update() if lock else program.versions
    latest = qs.order_by("-valid_from").first()
    if latest is None:
        raise ValueError(f"{program} has no versions")
    if effective <= latest.valid_from:
        raise AccessDenied(f"A change must start after {latest.valid_from:%Y-%m-%d}, when the latest version started.")
    if latest.valid_to is not None and effective > latest.valid_to + timedelta(days=1):
        raise AccessDenied(f"{program}'s versions end on {latest.valid_to:%Y-%m-%d}; a change cannot leave a gap.")
    return latest


def _start_version(actor, program: Program, *, effective: date, reason: str, changes: dict, created_by) -> ProgramVersion:
    if not reason.strip():
        raise AccessDenied("Give a reason for the change.")
    latest = _latest(program, effective, lock=True)
    before = audit.snapshot(latest, ["valid_from", "valid_to", *VERSION_FIELDS])
    values = {f: getattr(latest, f) for f in VERSION_FIELDS} | changes
    ProgramVersion.objects.filter(pk=latest.pk).update(valid_to=effective - timedelta(days=1))
    new = ProgramVersion.objects.create(
        program=program, valid_from=effective, valid_to=latest.valid_to, created_by=created_by, reason=reason, **values
    )
    audit.record(
        "program.changed",
        obj=program,
        before=before,
        after=audit.snapshot(new, ["valid_from", "valid_to", *VERSION_FIELDS]),
        reason=reason,
        actor=actor,
    )
    return new


# --- locked attributes and change requests (BR-13.7)

APPROVALS = "programs"  # programs.approve in the CPUC organisation decides requests (submissions' approvers and delegates)


def _names(fields) -> str:
    return ", ".join(ProgramVersion._meta.get_field(f).verbose_name.capitalize() for f in fields)


def change_policy(day: date | None = None) -> ProgramChangePolicy | None:
    """The policy in force on `day` (today by default): the newest one effective by then."""
    return ProgramChangePolicy.objects.filter(effective_from__lte=day or timezone.localdate()).order_by("-effective_from").first()


def policies():
    """Every program change policy, newest first (earlier ones are kept as history)."""
    return ProgramChangePolicy.objects.select_related("set_by")


def locked_fields(day: date | None = None) -> list[str]:
    policy = change_policy(day)
    return [f for f in VERSION_FIELDS if policy and f in policy.locked_fields]


@transaction.atomic
def set_locked_fields(actor, fields, *, effective: date, reason: str) -> ProgramChangePolicy:
    """A new policy from `effective` (today or later). Policies are never edited: one already set for a date stays as it is."""
    if not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator sets which program attributes are locked.")
    if not reason.strip():
        raise AccessDenied("Give a reason for the change.")
    _clean(dict.fromkeys(fields))
    if effective < timezone.localdate():
        raise AccessDenied("A policy cannot start in the past.")
    if ProgramChangePolicy.objects.filter(effective_from=effective).exists():
        raise AccessDenied(f"A policy already starts on {effective:%Y-%m-%d}.")
    before = locked_fields(effective)
    policy = ProgramChangePolicy.objects.create(
        effective_from=effective, locked_fields=[f for f in VERSION_FIELDS if f in set(fields)], reason=reason, set_by=actor
    )
    audit.record(
        "program_policy.changed",
        obj=policy,
        before={"locked_fields": before},
        after={"effective_from": str(effective), "locked_fields": policy.locked_fields},
        reason=reason,
        actor=actor,
    )
    return policy


def approver(actor):
    """The CPUC programs approver `actor` acts for (themselves, or the approver who delegated to them today), or None."""
    return approver_for(actor, APPROVALS) if actor is not None else None


def needs_approval(actor, changes: dict) -> list[str]:
    """The locked attributes among `changes` that `actor` may not change without a decision (none for CPUC system administrators
    and programs approvers, whose change is the decision)."""
    if actor is None or access.is_system_admin(actor) or approver(actor) is not None:
        return []
    locked = set(locked_fields())
    return [f for f in VERSION_FIELDS if f in changes and f in locked]


def _json(value):
    return value if value is None or isinstance(value, (str, int, float, bool, dict, list)) else str(value)


@transaction.atomic
def request_change(actor, program: Program, *, effective: date, reason: str, **changes) -> ProgramChangeRequest:
    """Ask for a change that touches a locked attribute. Checked now as the change itself would be; nothing changes until approved."""
    if not can_edit(actor, program):
        raise AccessDenied(
            f"Only {program.administrator.code if program.administrator else 'the CPUC'}'s administrators can change {program}."
        )
    if not reason.strip():
        raise AccessDenied("Give a reason for the change.")
    _clean(changes)
    if not changes:
        raise AccessDenied("Nothing was changed.")
    if not needs_approval(actor, changes):
        raise AccessDenied("Nothing in this change needs approval: save it as a change.")
    if program.change_requests.filter(status=ProgramChangeRequest.Status.PENDING).exists():
        raise AccessDenied(f"A change to {program} is already waiting for a decision; withdraw it first.")
    latest = _latest(program, effective)
    req = ProgramChangeRequest.objects.create(
        program=program,
        organisation=program.administrator,
        requested_by=actor,
        effective=effective,
        reason=reason,
        changes={k: _json(v) for k, v in changes.items()},
        before={k: _json(getattr(latest, k)) for k in changes},
    )
    audit.record(
        "program_change.requested",
        obj=req,
        after={"program": program.prg_id, "effective": str(effective), "changes": req.changes},
        reason=reason,
        actor=actor,
        organisation=program.administrator,
    )
    return req


def can_see_request(user, req: ProgramChangeRequest) -> bool:
    if access.is_system_admin(user) or approver(user) is not None:
        return True
    return req.organisation_id is not None and access.organisations_of(user).filter(pk=req.organisation_id).exists()


def visible_requests(user):
    qs = ProgramChangeRequest.objects.select_related("program", "requested_by", "decided_by", "organisation")
    if access.is_system_admin(user) or approver(user) is not None:
        return qs
    return qs.filter(organisation__in=access.organisations_of(user))


@transaction.atomic
def decide_change(actor, req: ProgramChangeRequest, approve: bool, note: str = "") -> ProgramChangeRequest:
    """Approve (the new version starts on the requested date, entered by the requester) or decline. Someone else decides a request."""
    req = ProgramChangeRequest.objects.select_for_update().select_related("program").get(pk=req.pk)
    if req.status != ProgramChangeRequest.Status.PENDING:
        raise AccessDenied("That request has been decided.")
    on_behalf = approver(actor)
    if on_behalf is None:
        raise AccessDenied("Only a CPUC programs approver, or their delegate, decides change requests.")
    if req.requested_by_id == actor.pk:
        raise AccessDenied("Someone other than the requester decides a request.")
    if not approve and not note.strip():
        raise AccessDenied("Say why the request is declined.")
    if approve:
        req.version = _start_version(
            actor, req.program, effective=req.effective, reason=req.reason, changes=dict(req.changes), created_by=req.requested_by
        )
    req.status = ProgramChangeRequest.Status.APPROVED if approve else ProgramChangeRequest.Status.DECLINED
    req.decided_by, req.decided_at, req.decision_note = actor, timezone.now(), note
    req.decided_as_delegate_of = on_behalf if on_behalf != actor else None
    req.save()
    audit.record(f"program_change.{req.status}", obj=req, reason=note, actor=actor, organisation=req.organisation)
    return req


@transaction.atomic
def withdraw_change(actor, req: ProgramChangeRequest, reason: str = "") -> ProgramChangeRequest:
    req = ProgramChangeRequest.objects.select_for_update().select_related("program").get(pk=req.pk)
    if req.status != ProgramChangeRequest.Status.PENDING:
        raise AccessDenied("That request has been decided.")
    if req.requested_by_id != actor.pk and not can_edit(actor, req.program):
        raise AccessDenied("Only the requester or the program's administrators withdraw a request.")
    req.status = ProgramChangeRequest.Status.WITHDRAWN
    req.save(update_fields=["status"])
    audit.record("program_change.withdrawn", obj=req, reason=reason, actor=actor, organisation=req.organisation)
    return req


def history(program: Program) -> list[tuple[ProgramVersion, list[tuple[str, object, object]]]]:
    """Each version with what changed from the one before (the field-level Program History CEDARS shows)."""
    out, prev = [], None
    for v in program.versions.order_by("valid_from"):
        now = audit.snapshot(v, VERSION_FIELDS)
        out.append((v, audit.diff(prev, now) if prev is not None else []))
        prev = now
    return out


# --- cycles and periods


def ensure_cycle(first_year: int, years: int = 4) -> FilingCycle:
    cycle, _ = FilingCycle.objects.get_or_create(first_year=first_year, defaults={"last_year": first_year + years - 1})
    return cycle


def ensure_periods(year: int) -> list[ReportingPeriod]:
    """The year, its four quarters and its twelve months."""
    out = [
        ReportingPeriod.objects.get_or_create(
            kind="year", year=year, number=0, defaults={"starts": date(year, 1, 1), "ends": date(year, 12, 31)}
        )[0]
    ]
    for q in range(1, 5):
        start = date(year, 3 * q - 2, 1)
        end = (date(year, 3 * q + 1, 1) if q < 4 else date(year + 1, 1, 1)) - timedelta(days=1)
        out.append(ReportingPeriod.objects.get_or_create(kind="quarter", year=year, number=q, defaults={"starts": start, "ends": end})[0])
    for m in range(1, 13):
        start = date(year, m, 1)
        end = (date(year, m + 1, 1) if m < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
        out.append(ReportingPeriod.objects.get_or_create(kind="month", year=year, number=m, defaults={"starts": start, "ends": end})[0])
    return out


# --- statewide allocation versions (BR-06.7, BR-13.8)


@dataclass
class AllocationIssue:
    year: int
    program_id: str
    message: str
    blocking: bool = True


def _require_allocation_admin(actor):
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator manages statewide allocation versions.")


def _dec(value: str, what: str) -> Decimal:
    try:
        return Decimal(str(value).strip() or "0")
    except InvalidOperation as e:
        raise ValueError(f"{what}: {value!r} is not a number") from e


@transaction.atomic
def import_allocation_csv(
    actor, content: bytes, *, label: str, purpose: str, filing_cycle: FilingCycle | None = None, source_name: str = "", notes: str = ""
) -> AllocationVersion:
    """A draft version from a factor file in CEDARS' export layout (ReCET's statewide_factors_*.csv): Year, ProgramID, ProgramName,
    PercentElectric, then <PA>GasFactor, <PA>ElectricFactor, <PA>WeightedFactor for PGE, SDGE, SCE, SCG."""
    _require_allocation_admin(actor)
    text = content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    needed = {"Year", "ProgramID", "PercentElectric"} | {f"{pa}{k}Factor" for pa in FUNDING_PAS for k in ("Gas", "Electric", "Weighted")}
    missing = needed - set(rows[0].keys() if rows else [])
    if missing:
        raise ValueError(f"the factor file is missing columns: {', '.join(sorted(missing))}")
    version = AllocationVersion.objects.create(
        label=label,
        purpose=purpose,
        filing_cycle=filing_cycle,
        notes=notes,
        created_by=actor,
        source_name=source_name,
        source_sha256=hashlib.sha256(content).hexdigest(),
    )
    for i, r in enumerate(rows, start=2):
        where = f"line {i}"
        entry = AllocationEntry.objects.create(
            version=version,
            year=int(r["Year"]),
            program_id=r["ProgramID"].strip(),
            program_name=(r.get("ProgramName") or "").strip(),
            percent_electric=_dec(r["PercentElectric"], where),
        )
        AllocationShare.objects.bulk_create(
            AllocationShare(
                entry=entry,
                funding_pa=pa,
                electric=_dec(r[f"{pa}ElectricFactor"], where),
                gas=_dec(r[f"{pa}GasFactor"], where),
                weighted=_dec(r[f"{pa}WeightedFactor"], where),
            )
            for pa in FUNDING_PAS
        )
    audit.record(
        "allocation_version.imported",
        obj=version,
        after={"label": label, "purpose": purpose, "entries": len(rows), "source": source_name, "sha256": version.source_sha256},
        actor=actor,
    )
    return version


def check_allocation(version: AllocationVersion) -> list[AllocationIssue]:
    """Each fuel's factors must add up to 1 across the funding PAs (or all be 0: a program with no budget in that fuel), and each
    weighted factor must equal PercentElectric x electric + (1 - PercentElectric) x gas."""
    issues = []
    for e in version.entries.prefetch_related("shares"):
        shares = list(e.shares.all())
        for fuel in ("electric", "gas", "weighted"):
            total = sum(getattr(s, fuel) for s in shares)
            if total != 0 and abs(total - 1) > SUM_TOLERANCE:
                issues.append(AllocationIssue(e.year, e.program_id, f"{fuel} factors add up to {total}, not 1"))
        for s in shares:
            expected = e.percent_electric * s.electric + (1 - e.percent_electric) * s.gas
            if abs(expected - s.weighted) > Decimal("0.002"):
                issues.append(
                    AllocationIssue(e.year, e.program_id, f"{s.funding_pa}'s weighted factor {s.weighted} is not {expected:.4f}", False)
                )
        if not (0 <= e.percent_electric <= 1):
            issues.append(AllocationIssue(e.year, e.program_id, f"PercentElectric {e.percent_electric} is outside 0 to 1"))
    return issues


@transaction.atomic
def confirm_allocation(actor, version: AllocationVersion) -> AllocationVersion:
    _require_allocation_admin(actor)
    version = AllocationVersion.objects.select_for_update().get(pk=version.pk)
    if version.status != AllocationVersion.Status.DRAFT:
        raise AccessDenied("That allocation version is already confirmed.")
    if not version.entries.exists():
        raise AccessDenied("That allocation version has no factors.")
    blocking = [i for i in check_allocation(version) if i.blocking]
    if blocking:
        raise AccessDenied(
            f"{len(blocking)} problems to fix first, e.g. {blocking[0].program_id} {blocking[0].year}: {blocking[0].message}."
        )
    version.status, version.confirmed_by, version.confirmed_at = AllocationVersion.Status.CONFIRMED, actor, timezone.now()
    version.save()
    audit.record("allocation_version.confirmed", obj=version, actor=actor)
    return version


@transaction.atomic
def discard_draft(actor, version: AllocationVersion, reason: str = "") -> None:
    _require_allocation_admin(actor)
    version = AllocationVersion.objects.select_for_update().get(pk=version.pk)
    if version.status != AllocationVersion.Status.DRAFT:
        raise AccessDenied("A confirmed allocation version is kept for good.")
    audit.record("allocation_version.discarded", obj=version, reason=reason, actor=actor)
    version.delete()


def most_recent_allocation(year: int) -> AllocationVersion | None:
    """The version a run uses when it names none: the most recently confirmed one with factors for the program year."""
    return (
        AllocationVersion.objects.filter(status=AllocationVersion.Status.CONFIRMED, entries__year=year)
        .order_by("-confirmed_at", "-pk")
        .distinct()
        .first()
    )


def allocation_csv(version: AllocationVersion) -> str:
    """The version in the factor-file layout ReCET reads (`--sw-factors`), so a run can be priced with exactly these factors."""
    out = io.StringIO()
    cols = ["Year", "ProgramID", "ProgramName", "PercentElectric"]
    cols += [f"{pa}GasFactor" for pa in ("PGE", "SDGE", "SCE", "SCG")] + [f"{pa}ElectricFactor" for pa in ("PGE", "SDGE", "SCE", "SCG")]
    cols += [f"{pa}WeightedFactor" for pa in ("PGE", "SDGE", "SCE", "SCG")] + ["Summary"]
    w = csv.DictWriter(out, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for e in version.entries.prefetch_related("shares"):
        row = {
            "Year": e.year,
            "ProgramID": e.program_id,
            "ProgramName": e.program_name,
            "PercentElectric": f"{e.percent_electric:.4f}",
            "Summary": "",
        }
        for s in e.shares.all():
            row[f"{s.funding_pa}GasFactor"] = f"{s.gas:.4f}"
            row[f"{s.funding_pa}ElectricFactor"] = f"{s.electric:.4f}"
            row[f"{s.funding_pa}WeightedFactor"] = f"{s.weighted:.4f}"
        w.writerow(row)
    return out.getvalue()


# --- the operating budget (BR-04.7)

_operating_budget_sources: list = []


def register_operating_budget(source) -> None:
    """The Budgets module's operating budget: source(organisation, cycle) returns one dict per program and year, {prg_id, year, name,
    approved, shifted, operating} (Decimal dollars). Monthly reports, Claims and dashboards read it through `operating_budget`, so they
    never import Budgets (modules never import each other)."""
    _operating_budget_sources[:] = [source]


def operating_budget(organisation: Organisation, cycle: FilingCycle | None) -> list[dict]:
    """A PA's operating budget by program and year in a cycle: the approved budget plus the fund shifts in effect; [] when Budgets is
    not installed or there is no cycle."""
    if cycle is None or not _operating_budget_sources:
        return []
    return _operating_budget_sources[0](organisation, cycle)


def cycle_for_year(year: int) -> FilingCycle | None:
    """The filing cycle a program year belongs to."""
    return FilingCycle.objects.filter(first_year__lte=year, last_year__gte=year).order_by("-first_year").first()
