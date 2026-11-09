"""Program registry rules. Every change is audited; versions and confirmed allocation tables are never edited.

Effective dates: a program's attributes for program year Y are the version in force on 31 December of Y, or, for a program that ended
during Y, its last version that overlapped Y (`version_for_year`). Any date: `version_on`.
"""

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F, Max, Prefetch, Q
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import permissions as perms
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
FUNDING_PAS = access.ALLOCATION_MANAGERS  # the IOUs that fund statewide programs, as the factor files have them
SUM_TOLERANCE = Decimal("0.0005")
FACTOR_STEP = Decimal("0.0001")  # every factor is kept to 4 decimal places (Jake, 2026-11-06)


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
    """(program, version) for every program not flagged as a portfolio cost, closed programs included: the version for the
    program year, or with no year the version in force today (else the latest). Two queries, whatever the number of programs."""
    out = []
    today = timezone.localdate()
    programs = Program.objects.filter(portfolio_cost=False).select_related("administrator", "follows")
    for p in programs.prefetch_related(Prefetch("versions", queryset=ProgramVersion.objects.order_by("-valid_from"))):
        versions = list(p.versions.all())
        v = _for_year(versions, year) if year else (_in_force(versions, today) or (versions[0] if versions else None))
        out.append((p, v))
    return out


# The program table a calculation run is priced with: ReCET's supplied-programs layout (`programs.COLUMNS`, the CEDARS Programs list
# columns; `programs_file`). The registry has no `Sector` label column ("Commercial (Com)"), so the file leaves it out.
RUN_PROGRAM_COLUMNS = [
    "PA",
    "PrgID",
    "ProgramName",
    "ProgramStatus",
    "ProgramSegment",
    "StatewideProgram",
    "PrimarySector",
    "BPSector",
    "ProgramImplementer",
    "ProgramCategory",
    "ImplementationContractor",
    "StartYear",
    "EndYear",
    "Resource_Flag",
    "NonResource_Flag",
    "Deemed_Flag",
    "Custom_Flag",
    "Upstream_Flag",
    "Midstream_Flag",
    "Downstream_Flag",
    "DirectInstall",
    "Audit_Flag",
    "Financing",
    "ParentProgram",
    "Exclude_From_Budget",
    "Exclude_From_CE",
    "Exclude_From_Statewide_Flag",
]
FLAG_COLUMNS = RUN_PROGRAM_COLUMNS[13:23]


def pa_code(program: Program) -> str:
    """The program's PA as CEDARS' Programs list has it: SW for a statewide program, else its administrator's code."""
    return "SW" if program.statewide or program.administrator is None else program.administrator.code


@dataclass
class RunPrograms:
    csv: str
    year: int | None
    programs: int


def run_programs(year: int | None) -> RunPrograms:
    """Every program in the registry, portfolio-cost IDs included, as the whole program table a run for program year `year` is priced
    with (the engine's programs_replace, so its shipped copy of CEDARS' Programs list is never used: Jake, 2026-10-31): each
    program's version for that year (`version_for_year`), or for a program with none that year (not started yet, or closed before it)
    its latest version. With no year, the version in force today. A registry with no programs gives the header alone, and every PrgID
    is then unknown to the run."""
    today = timezone.localdate()
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(RUN_PROGRAM_COLUMNS)
    n = 0
    programs = Program.objects.select_related("administrator").order_by("prg_id")
    for p in programs.prefetch_related(Prefetch("versions", queryset=ProgramVersion.objects.order_by("-valid_from"))):
        versions = list(p.versions.all())
        if not versions:
            continue
        v = (_for_year(versions, year) if year else _in_force(versions, today)) or versions[0]
        flags = v.flags or {}
        w.writerow(
            [
                pa_code(p),
                p.prg_id,
                v.name,
                v.status,
                v.segment,
                v.statewide_program,
                v.primary_sector,
                ", ".join(f"{k}: {float(share):.6f}" for k, share in (v.sectors or {}).items()),
                v.implementer,
                v.category,
                v.implementation_contractor,
                v.start_year or "",
                v.end_year or "",
                *(flags.get(c, "") for c in FLAG_COLUMNS),
                v.parent_program,
                v.exclude_from_budget,
                v.exclude_from_ce,
                v.exclude_from_statewide,
            ]
        )
        n += 1
    return RunPrograms(out.getvalue(), year, n)


def can_edit(actor, program: Program) -> bool:
    """Whether actor may change the program: a CPUC system administrator, or the org admins and programs.admin holders of its
    administrator (of a statewide program, its lead PA). Nobody changes an ID that follows a statewide program: its attributes are the
    statewide program's (Jake, 2026-11-06), so a change is made there and reaches every ID that follows it."""
    if program.follows_id is not None:
        return False
    if actor is None or access.is_system_admin(actor):
        return True
    org = program.administrator or lead_of(program)
    return org is not None and (access.is_org_admin(actor, org) or access.has_permission(actor, org, "programs.admin"))


def _cannot_edit(program: Program) -> AccessDenied:
    if program.follows_id is not None:
        return AccessDenied(
            f"{program} follows the statewide program {program.follows}: change {program.follows} and the change reaches {program}."
        )
    who = program.administrator or lead_of(program)
    return AccessDenied(f"Only {who.code if who else 'the CPUC'}'s administrators can change {program}.")


# --- statewide programs' lead PAs


def lead_of(program: Program) -> Organisation | None:
    """The PA that files a statewide program's claims and budgets; None for a program that is not statewide or has none recorded."""
    return program.lead if program.statewide else None


def filing_problem(program: Program, organisation: Organisation) -> str:
    """Why `organisation` may not file `program` (its claims or budget), or "" when it may: its own program, or a statewide program it
    leads. A statewide program is filed only by its lead PA, so a funding PA never files it a second time; one with no lead recorded is
    filed by nobody until the CPUC records one."""
    if program.statewide:
        if program.lead_id is None:
            return f"{program.prg_id} is a statewide program with no lead PA recorded; any PA records it on the program's page"
        if program.lead_id != organisation.pk:
            return f"{program.prg_id} is a statewide program led by {program.lead.code}; only its lead PA files it"
        return ""
    if program.administrator_id not in (None, organisation.pk):
        return f"{program.prg_id} is another program administrator's program"
    return ""


def can_set_lead(actor, program: Program, organisation: Organisation | None) -> bool:
    """Whether actor, acting for `organisation`, may record a statewide program's lead PA (Jake, 2026-11-06): any PA's administrators
    (org admins and programs.admin holders) while it has none, then only the current lead's. A CPUC system administrator may too."""
    if not program.statewide or actor is None:
        return program.statewide
    if access.is_system_admin(actor):
        return True
    if organisation is None or organisation.kind != Organisation.Kind.PA:
        return False
    if program.lead_id is not None and program.lead_id != organisation.pk:
        return False
    return access.is_org_admin(actor, organisation) or access.has_permission(actor, organisation, "programs.admin")


@transaction.atomic
def set_lead(actor, program: Program, lead: Organisation | None, reason: str, organisation: Organisation | None = None) -> Program:
    """Record (or clear) a statewide program's lead PA, with a reason, acting for `organisation` (`can_set_lead`): any PA while the
    program has no lead, then only its current lead, which may hand it to another PA."""
    if not program.statewide:
        raise AccessDenied(f"{program} is not a statewide program: its administrator files it.")
    if not can_set_lead(actor, program, organisation):
        if program.lead_id is not None:
            raise AccessDenied(f"Only {program.lead.code}, {program}'s lead PA, changes its lead.")
        raise AccessDenied("Only a program administrator's administrators record a statewide program's lead PA.")
    if lead is not None and lead.kind != Organisation.Kind.PA:
        raise AccessDenied(f"{lead.code} is not a program administrator.")
    if not reason.strip():
        raise AccessDenied("Give the reason for the change.")
    before = program.lead.code if program.lead_id else None
    after = lead.code if lead else None
    if before == after:
        return program
    program.lead = lead
    program.save(update_fields=["lead"])
    audit.record(
        "program.lead_set",
        obj=program,
        before={"lead": before},
        after={"lead": after},
        reason=reason,
        actor=actor,
        organisation=organisation if organisation is not None and organisation.kind == Organisation.Kind.PA else None,
    )
    return program


# --- statewide programs as the source of truth for the IDs allocated to them (Jake, 2026-11-06)
#
# A statewide program (SW_FS) is allocated to each funding PA under the PA's code in front of its ID (PGE_SW_FS, SCE_SW_FS ...). Those
# IDs follow the statewide program: from the day they are linked, their attributes are the statewide program's, written by the
# registry whenever the statewide program changes, and their own PA never changes them. An ID keeps its own span (the years its
# versions cover, which legacy filings and claims set) and its versions before that day; only the attributes follow. A statewide
# program's own record is filled the first time from its lead PA's ID the same way (CEDARS' statewide rows are mostly blank), and from
# then on its lead PA's administrators change it (`can_edit`).


def _codes(organisation: Organisation) -> set[str]:
    """Every code the organization has had (PGE; EBCE and AVA)."""
    return {organisation.code, *organisation.codes.values_list("code", flat=True)}


def statewide_part_of(program: Program) -> Program | None:
    """The statewide program whose allocated ID `program` is by its PrgID (its PA's code, an underscore and the statewide PrgID), or
    None. A portfolio-cost ID and an ID with anything after the statewide PrgID (PGE_SW_FS_PA, the PA's own costs) are not."""
    if program.statewide or program.portfolio_cost or program.administrator is None:
        return None
    for code in _codes(program.administrator):
        prefix = f"{code}_"
        if program.prg_id.startswith(prefix) and len(program.prg_id) > len(prefix):
            sw = Program.objects.filter(statewide=True, prg_id=program.prg_id[len(prefix) :]).first()
            if sw is not None:
                return sw
    return None


def allocated_ids(sw: Program):
    """Every program whose PrgID is a PA's code in front of the statewide program's (`statewide_part_of`), linked or not."""
    out = []
    for p in Program.objects.filter(statewide=False, portfolio_cost=False, prg_id__endswith=f"_{sw.prg_id}").select_related(
        "administrator", "follows"
    ):
        if p.administrator is not None and p.prg_id[: -len(sw.prg_id) - 1] in _codes(p.administrator):
            out.append(p)
    return sorted(out, key=lambda p: p.prg_id)


def lead_id(sw: Program) -> Program | None:
    """The statewide program's lead PA's own ID of it (PGE_SW_FS when PG&E leads SW_FS), or None."""
    lead = lead_of(sw)
    return next((p for p in allocated_ids(sw) if p.administrator_id == lead.pk), None) if lead else None


def family(program: Program) -> list[Program]:
    """The statewide program and every ID that follows it, for a statewide program or one of its IDs; else the program alone. Their
    pages show the same documents and evaluations."""
    sw = program.follows if program.follows_id else program
    if not sw.statewide:
        return [program]
    return [sw, *sw.subprograms.order_by("prg_id")]


def _values(v: ProgramVersion) -> dict:
    return {f: getattr(v, f) for f in VERSION_FIELDS}


def _followed_values(sw: Program, source: ProgramVersion) -> dict:
    """An ID's attributes when it follows `sw`: the statewide program's version, with the statewide program it delivers named."""
    return _values(source) | {"statewide_program": f"{sw.prg_id} - {source.name}"}


def _source_on(sources: list[ProgramVersion], day: date) -> ProgramVersion:
    """The source version for `day`: the one in force then, else the last one before it, else the first one (sources oldest first)."""
    earlier = [s for s in sources if s.valid_from <= day]
    in_force = [s for s in earlier if s.valid_to is None or s.valid_to >= day]
    return (in_force or earlier or sources)[-1 if (in_force or earlier) else 0]


def _span(versions, day: date) -> list[list]:
    """The dates `versions` cover from `day` on, as [start, end] runs (end None = open-ended); touching or overlapping ones are one."""
    runs: list[list] = []
    for v in sorted(versions, key=lambda v: v.valid_from):
        if v.valid_to is not None and v.valid_to < day:
            continue
        a, b = max(v.valid_from, day), v.valid_to
        if runs and (runs[-1][1] is None or runs[-1][1] + timedelta(days=1) >= a):
            if runs[-1][1] is not None and (b is None or b > runs[-1][1]):
                runs[-1][1] = b
        else:
            runs.append([a, b])
    return runs


def _merged(pieces) -> list[tuple]:
    """(start, end, values) pieces, oldest first, with touching pieces of the same values made one."""
    out: list[list] = []
    for a, b, vals in pieces:
        if out and out[-1][2] == vals and out[-1][1] is not None and out[-1][1] + timedelta(days=1) == a:
            out[-1][1] = b
        else:
            out.append([a, b, vals])
    return [tuple(p) for p in out]


def _pieces(span: list[list], sources: list[ProgramVersion], values) -> list[tuple]:
    """The versions for the dates `span` covers (gaps kept), cut where a source version starts, each piece with `values(the source
    version for its start)` (`_source_on`)."""
    out = []
    for start, end in span:
        cuts = sorted({s.valid_from for s in sources if start < s.valid_from and (end is None or s.valid_from <= end)})
        starts = [start, *cuts]
        for i, a in enumerate(starts):
            b = starts[i + 1] - timedelta(days=1) if i + 1 < len(starts) else end
            out.append((a, b, values(_source_on(sources, a))))
    return _merged(out)


def _on_or_after(program: Program, day: date) -> ProgramVersion | None:
    """The program's version in force on `day`, else its first one after it."""
    return version_on(program, day) or program.versions.filter(valid_from__gt=day).order_by("valid_from").first()


def _replace_from(actor, target: Program, day: date, pieces: list[tuple], *, reason: str, created_by) -> bool:
    """Give `target` the versions `pieces` from `day` on: the version in force on `day` ends the day before and the ones after it give
    way (each kept in the audit event), so its record from that day is the source's. False when it already is."""
    versions = list(target.versions.select_for_update().order_by("valid_from"))
    affected = [v for v in versions if v.valid_to is None or v.valid_to >= day]
    if _merged([(max(v.valid_from, day), v.valid_to, _values(v)) for v in affected]) == list(pieces):
        return False
    replaced = [v for v in affected if v.valid_from >= day]
    if ProgramChangeRequest.objects.filter(version__in=replaced).exists():
        raise AccessDenied(f"{target} has a version from {day:%Y-%m-%d} on that a change request started; it cannot be replaced.")
    fields = ["valid_from", "valid_to", *VERSION_FIELDS]
    before = [audit.snapshot(v, fields) for v in affected]
    for v in affected:
        if v.valid_from < day:
            ProgramVersion.objects.filter(pk=v.pk).update(valid_to=day - timedelta(days=1))
    ProgramVersion.objects.filter(pk__in=[v.pk for v in replaced]).delete()
    new = [
        ProgramVersion.objects.create(program=target, valid_from=a, valid_to=b, created_by=created_by, reason=reason, **vals)
        for a, b, vals in pieces
    ]
    after = [audit.snapshot(v, fields) for v in new]
    audit.record(
        "program.changed",
        obj=target,
        before={**(before[0] if before else {}), "versions": before},
        after={**(after[0] if after else {}), "versions": after},
        reason=reason,
        actor=actor,
    )
    return True


def _follow_from(actor, sub: Program, sw: Program, day: date, *, reason: str, created_by) -> bool:
    """Bring `sub`'s attributes from `day` on (never before it started following) into line with the statewide program's."""
    day = max(day, sub.follows_from or day)
    sources = list(sw.versions.order_by("valid_from"))
    if not sources:
        return False
    pieces = _pieces(_span(sub.versions.all(), day), sources, lambda s: _followed_values(sw, s))
    return _replace_from(actor, sub, day, pieces, reason=f"follows {sw.prg_id}: {reason}", created_by=created_by)


@dataclass
class StatewideSync:
    """What `follow_statewide` did (or, without apply, would do) to one statewide program."""

    sw: Program
    skipped: str = ""  # why nothing was done
    source: Program | None = None  # the lead PA's ID its record was filled from
    filled: dict = None  # {attribute: (statewide program's value, lead PA's value)} on the day, for the statewide program
    filled_from: date | None = None
    linked: list = None  # [(ID, the day it follows from, {attribute: (its value, the statewide program's)})]
    already: list = None  # IDs that followed it already


class _DryRun(Exception):
    pass


def _diff(before: ProgramVersion | None, after: dict | None) -> dict:
    if after is None:
        return {}
    return {f: (getattr(before, f) if before else None, v) for f, v in after.items() if before is None or getattr(before, f) != v}


def follow_statewide(
    actor, sw: Program, *, effective: date | None = None, reason: str, apply: bool = False, fill: bool = True
) -> StatewideSync:
    """Make the statewide program the source of truth for its IDs (Jake, 2026-11-06). The first time, its record is filled from its lead
    PA's own ID of it (`lead_id`; fill=False leaves it as it is): from `effective` (today by default) its attributes are that ID's; then
    every PA's ID of it (`allocated_ids`) follows it from that day, and from then on only the statewide program changes. Each keeps its
    own span: only attributes change. Nothing is done to a statewide program with no lead PA recorded, or (the first time) none of whose
    IDs is its lead's. Without apply, everything is done and rolled back, so the report is exactly what apply would do. A CPUC system
    administrator, or a system job."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator links statewide programs' IDs.")
    if not reason.strip():
        raise AccessDenied("Give the reason.")
    effective = effective or timezone.localdate()
    out = StatewideSync(sw, filled={}, linked=[], already=[])
    if not sw.statewide:
        out.skipped = "not a statewide program"
        return out
    if lead_of(sw) is None:
        out.skipped = "no lead PA recorded: record one on its page, then run this again"
        return out
    ids = allocated_ids(sw)
    out.already = [p.prg_id for p in ids if p.follows_id == sw.pk]
    todo = [p for p in ids if p.follows_id is None]
    if not todo:
        if not out.already:
            out.skipped = "no PA's ID of it in the registry"
        return out
    out.source = lead_id(sw)
    if not out.already and out.source is None and fill:
        out.skipped = f"no ID of it is its lead PA's ({lead_of(sw).code}_{sw.prg_id}) to fill it from"
        return out
    try:
        with transaction.atomic():
            if fill and not out.already and out.source.follows_id is None:
                lead_versions = list(out.source.versions.order_by("valid_from"))
                own_versions = list(sw.versions.order_by("valid_from"))
                own = own_versions[-1].statewide_program if own_versions else ""
                # the dates either covers: a statewide record that ends before its lead's ID's filed years reaches them too
                span = _span(own_versions + lead_versions, effective)
                pieces = _pieces(span, lead_versions, lambda s: _values(s) | {"statewide_program": own}) if lead_versions else []
                if pieces and _replace_from(
                    actor, sw, effective, pieces, reason=f"filled from {out.source.prg_id}, its lead PA's ID: {reason}", created_by=actor
                ):
                    was = next((v for v in own_versions if v.valid_to is None or v.valid_to >= effective), None)
                    out.filled = _diff(was, pieces[0][2])
                    out.filled_from = pieces[0][0]
            for p in todo:
                p.follows, p.follows_from = sw, effective
                p.save(update_fields=["follows", "follows_from"])
                audit.record(
                    "program.follows_statewide",
                    obj=p,
                    before={"follows": None},
                    after={"follows": sw.prg_id, "from": effective.isoformat()},
                    reason=reason,
                    actor=actor,
                )
                mine = _on_or_after(p, effective)
                _follow_from(actor, p, sw, effective, reason=reason, created_by=actor)
                out.linked.append((p.prg_id, effective, _diff(mine, _values(_on_or_after(p, effective))) if mine else {}))
            if not apply:
                raise _DryRun
    except _DryRun:
        for p in todo:
            p.follows, p.follows_from = None, None
    return out


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
    _clean(fields)
    p = Program(prg_id=prg_id.strip(), administrator=administrator, statewide=statewide, portfolio_cost=portfolio_cost)
    sw = statewide_part_of(p)
    if sw is not None and sw.subprograms.exists() and sw.versions.exists():  # a new ID of a followed statewide program: from the start
        p.follows, p.follows_from = sw, valid_from
        fields = _followed_values(sw, _source_on(list(sw.versions.order_by("valid_from")), valid_from))
    p.save()
    v = ProgramVersion.objects.create(program=p, valid_from=valid_from, created_by=actor, reason=reason, **fields)
    audit.record(
        "program.created",
        obj=p,
        after={
            "prg_id": p.prg_id,
            "administrator": administrator.code if administrator else None,
            "follows": p.follows.prg_id if p.follows_id else None,
            "version": audit.snapshot(v, ["valid_from", *VERSION_FIELDS]),
        },
        reason=reason,
        actor=actor,
    )
    if p.follows_id:  # the statewide program's later versions too
        _follow_from(actor, p, sw, valid_from, reason=reason or "a new ID of it", created_by=actor)
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
        raise _cannot_edit(program)
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
    """The new version, and the same change to every ID that follows the program (a statewide program's IDs, `follow_statewide`)."""
    if not reason.strip():
        raise AccessDenied("Give a reason for the change.")
    latest = _latest(program, effective, lock=True)
    subs = list(program.subprograms.order_by("prg_id")) if program.statewide else []
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
    for sub in subs:
        _follow_from(actor, sub, program, effective, reason=reason, created_by=created_by)
    return new


@transaction.atomic
def add_history(
    actor, *, prg_id: str, administrator: Organisation | None, statewide: bool, years: dict[int, dict], reason: str
) -> tuple[Program, int]:
    """Program years from before the registry (legacy CEDARS' records): for each year with no version yet, a version over that calendar
    year with the given attributes; consecutive years with the same attributes share one version. Years a version already covers are
    left alone (the registry's own history wins), and the program is created when it is new. A system job, or a CPUC system
    administrator. Returns the program and the number of versions added."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator adds program history.")
    if not reason.strip():
        raise AccessDenied("Give the reason (the source of the history).")
    program = Program.objects.filter(prg_id=prg_id.strip()).first()
    if program is None:
        if administrator is None and not statewide:
            raise AccessDenied("A program needs an administrator unless it is statewide.")
        program = Program.objects.create(prg_id=prg_id.strip(), administrator=administrator, statewide=statewide)
        audit.record(
            "program.created",
            obj=program,
            after={"prg_id": program.prg_id, "administrator": administrator.code if administrator else None},
            reason=reason,
            actor=actor,
        )
    existing = list(program.versions.select_for_update().order_by("-valid_from"))
    added, last = [], None
    for year in sorted(years):
        if _for_year(existing, year) is not None:
            last = None
            continue
        fields = _clean(dict(years[year]))
        end = date(year, 12, 31)
        if last is not None and last.valid_to == date(year - 1, 12, 31) and all(getattr(last, f) == val for f, val in fields.items()):
            last.valid_to = end
            last.save(update_fields=["valid_to"])
            continue
        last = ProgramVersion.objects.create(
            program=program, valid_from=date(year, 1, 1), valid_to=end, created_by=actor, reason=reason, **fields
        )
        added.append(last)
    if added:
        audit.record(
            "program.history_added",
            obj=program,
            after={"versions": [audit.snapshot(v, ["valid_from", "valid_to", *VERSION_FIELDS]) for v in added]},
            reason=reason,
            actor=actor,
        )
    return program, len(added)


# --- locked attributes and change requests

APPROVALS = "programs"  # programs.approve in the CPUC organization decides requests (submissions' approvers and delegates)


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
        raise _cannot_edit(program)
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
        organisation=program.administrator or lead_of(program),
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
        organisation=req.organisation,
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


def ensure_cycle(first_year: int, years: int = 4, *, last_year: int | None = None) -> FilingCycle:
    """The cycle of these program years, made when missing. With last_year, exactly first_year to last_year (any number of years).
    Without it, a cycle that starts in first_year when there is one (the longest, when several do), else a new one of `years` years."""
    if last_year is None:
        found = FilingCycle.objects.filter(first_year=first_year).order_by("-last_year").first()
        if found is not None:
            return found
        last_year = first_year + years - 1
    if last_year < first_year:
        raise ValueError(f"A cycle ends in or after the year it starts: {first_year} to {last_year}.")
    cycle, _ = FilingCycle.objects.get_or_create(first_year=first_year, last_year=last_year)
    return cycle


def cycle_from_slug(slug: str) -> FilingCycle | None:
    """The cycle a page address names: "2028-2031", or a bare first year ("2028", as addresses had it before cycles could share a
    first year: the longest cycle that starts then). None when there is no such cycle."""
    first, _, last = str(slug).partition("-")
    if not first.isdigit() or (last and not last.isdigit()):
        return None
    cycles = FilingCycle.objects.filter(first_year=int(first))
    if last:
        cycles = cycles.filter(last_year=int(last))
    return cycles.order_by("-last_year").first()


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


# --- statewide allocation versions


@dataclass
class AllocationIssue:
    year: int
    program_id: str
    message: str
    blocking: bool = True


# Who manages the factors (Jake, 2026-11-02): the PAs that fund statewide programs, who agree them among themselves. It is a permission
# of its own, allocation.manage, that an organization's administrator grants to its members, and only in an organization the CPUC has
# marked as managing statewide allocations (Organisation.manages_allocation: PG&E, SCE, SoCalGas and SDG&E). A holder loads drafts and
# confirms or discards them alone; the other managing organizations are told. A CPUC system administrator can do all of it. Everyone
# signed in can read every version.


def can_manage_allocation(actor, organisation) -> bool:
    """Whether actor, acting for organisation, may load, confirm and discard statewide allocation versions."""
    if actor is None or access.is_system_admin(actor):
        return True
    if organisation is None or not organisation.manages_allocation:
        return False
    return access.has_permission(actor, organisation, perms.ALLOCATION)


def _require_allocation_manager(actor, organisation) -> None:
    if not can_manage_allocation(actor, organisation):
        raise AccessDenied(
            "Changing statewide allocation factors needs the permission to manage them, held in an organization that manages statewide "
            "allocations, or a CPUC system administrator."
        )


def _acting_for(actor, organisation):
    """The organization a change is recorded under: the managing organization the actor acted for, or none for the CPUC."""
    if actor is None or organisation is None or not organisation.manages_allocation:
        return None
    return organisation


def _tell_managers(version: AllocationVersion, organisation, title: str, body: str) -> None:
    """Tell the other managing organizations' holders of a change to the factors their budgets and claims are allocated with."""
    from cedars.services.notifications import service as notifications

    for org in Organisation.objects.filter(manages_allocation=True, active=True).exclude(pk=getattr(organisation, "pk", None)):
        key = f"allocation-{version.pk}-{version.status}-{org.code}"
        for user in access.holders(org, perms.ALLOCATION):
            notifications.notify(user, title, body, f"/allocations/{version.pk}/", key=key)


def _dec(value: str, what: str) -> Decimal:
    try:
        return Decimal(str(value).strip() or "0")
    except InvalidOperation as e:
        raise ValueError(f"{what}: {value!r} is not a number") from e


MAX_FACTOR_PLACES = 4
MAX_LISTED = 10  # problem cells named in a refused factor file's message
TOO_MANY_PLACES = f"has more than {MAX_FACTOR_PLACES} decimal places; {MAX_FACTOR_PLACES} is the most a factor file may give"


def _factor(value, what: str, problems: list[str], *, required: bool = False) -> Decimal:
    """A factor or PercentElectric from a factor file. A value with more than 4 decimal places is refused, not rounded (Jake,
    2026-11-06); trailing zeros do not count, so 0.44400 is 0.444. A blank factor is 0 (a PA that does not fund the program); a blank
    required value is refused. Each refusal is added to problems (and 0 returned) so that the whole file is reported at once."""
    text = str(value if value is not None else "").strip()
    if not text:
        if required:
            problems.append(f"{what} is blank; it is required")
        return Decimal(0)
    try:
        d = Decimal(text)
    except InvalidOperation:
        d = None
    if d is None or not d.is_finite():
        problems.append(f"{what}: {value!r} is not a number")
        return Decimal(0)
    if d != 0 and -d.normalize().as_tuple().exponent > MAX_FACTOR_PLACES:
        problems.append(f"{what}: {text} {TOO_MANY_PLACES}")
        return Decimal(0)
    return d.quantize(FACTOR_STEP)


def fuel_total_problem(fuel: str, factors: dict[str, Decimal]) -> str:
    """Why one fuel's factors are refused, or "": they must add up to exactly 1.0000 (Jake, 2026-11-06)."""
    total = sum(factors.values(), Decimal(0))
    if total == 1:
        return ""
    return f"the {fuel} factors add up to {total:.4f}; they must add up to exactly 1.0000"


def single_fuel_problem(percent_electric: Decimal, electric: dict[str, Decimal], gas: dict[str, Decimal]) -> str:
    """Why a program with one fuel only is refused, or "": a 100% electric program's gas factors must equal its electric factors, and
    a 0% electric program's electric factors its gas factors (Jake, 2026-11-06), so its savings in the other fuel (e.g. a lighting
    measure's gas interactive effects) are still allocated, although its weighted factors do not use them."""
    if percent_electric == 1 and gas != electric:
        return "PercentElectric is 1, so the gas factors must equal the electric factors (leave them blank and they are copied)"
    if percent_electric == 0 and electric != gas:
        return "PercentElectric is 0, so the electric factors must equal the gas factors (leave them blank and they are copied)"
    return ""


def weighted_factors(percent_electric: Decimal, electric: dict[str, Decimal], gas: dict[str, Decimal]) -> dict[str, Decimal]:
    """Each funding PA's weighted factor, PercentElectric x electric + (1 - PercentElectric) x gas, rounded to 4 decimal places so that
    the PAs' weighted factors add up to exactly 1.0000 (Jake, 2026-11-06). The inputs have at most 4 decimal places (`_factor`).

    Each is rounded half up first. When the rounded values do not add up to 1.0000, they are moved one 0.0001 step at a time, and the
    PA moved first is the one whose unrounded value was closest to rounding the other way: closest to rounding up when the total is
    short, closest to rounding down when it is over (the largest-remainder method). A tie goes to the larger factor (the smaller change
    relative to its size), then to the PAs' order. A value is never moved below 0, and a PA whose unrounded factor is 0 is never moved up.
    The total is forced to 1.0000 only when the unrounded factors add up to 1 within SUM_TOLERANCE, as they always do once each fuel's
    factors add up to 1.0000 (`fuel_total_problem`); otherwise (all 0, or a version loaded before that rule) the rounded values are
    kept and `check_allocation` reports the totals."""
    pas = list(electric)
    order = {pa: i for i, pa in enumerate(pas)}
    raw = {pa: percent_electric * electric[pa] + (1 - percent_electric) * gas.get(pa, Decimal(0)) for pa in pas}
    out = {pa: v.quantize(FACTOR_STEP, rounding=ROUND_HALF_UP) for pa, v in raw.items()}
    if abs(sum(raw.values()) - 1) > SUM_TOLERANCE:
        return out
    steps = int((1 - sum(out.values())) / FACTOR_STEP)
    while steps:
        if steps > 0:
            # short of 1: the value that came closest to rounding up (the largest raw - rounded) goes up a step
            pool = [pa for pa in pas if raw[pa] > 0]
            pa = max(pool, key=lambda p: (raw[p] - out[p], raw[p], -order[p]))
            out[pa] += FACTOR_STEP
            steps -= 1
        else:
            # over 1: the value that came closest to rounding down (the smallest raw - rounded) goes down a step
            pool = [pa for pa in pas if out[pa] >= FACTOR_STEP]
            pa = min(pool, key=lambda p: (raw[p] - out[p], -raw[p], order[p]))
            out[pa] -= FACTOR_STEP
            steps += 1
    return out


# The funding PAs of a factor file, in the order ReCET's layout lists them.
ALLOCATION_PAS = ("PGE", "SDGE", "SCE", "SCG")

FACTOR_FILE_COLUMNS = ["Year", "ProgramID", "ProgramName", "PercentElectric"] + [
    f"{pa}{k}Factor" for k in ("Gas", "Electric") for pa in ALLOCATION_PAS
]


def allocation_template_csv() -> str:
    """The factor file template: the columns a new version is loaded from, with no rows. The weighted factors are not in it: they are
    computed from PercentElectric and the electric and gas factors when the file is loaded."""
    return ",".join(FACTOR_FILE_COLUMNS) + "\n"


@transaction.atomic
def import_allocation_csv(
    actor,
    content: bytes,
    *,
    label: str,
    purpose: str,
    filing_cycle: FilingCycle | None = None,
    source_name: str = "",
    notes: str = "",
    organisation=None,
) -> AllocationVersion:
    """A draft version from a factor file in the template's layout (`allocation_template_csv`): Year, ProgramID, ProgramName,
    PercentElectric (required), then <PA>GasFactor and <PA>ElectricFactor for PGE, SDGE, SCE, SCG. A value with more than 4 decimal
    places (trailing zeros aside), a fuel whose factors do not add up to exactly 1.0000 (`fuel_total_problem`), a one-fuel program
    whose two fuels' factors differ (`single_fuel_problem`; the unused fuel's left blank or all 0 are copied from the other, counted in
    the version's `fuel_copied`) or a PercentElectric outside 0 to 1 refuses the file, with every such problem listed (up to
    MAX_LISTED). The weighted factors are computed (`weighted_factors`); a file in CEDARS' export layout (ReCET's statewide_factors_*.csv)
    is read too, and its <PA>WeightedFactor columns are ignored. The version's `weighted_replaced` says on how many program-years the
    file's own weighted factors differed from the computed ones. organisation: the organization the actor acts for (one that manages
    statewide allocations, unless the actor is a CPUC system administrator)."""
    _require_allocation_manager(actor, organisation)
    if AllocationVersion.objects.filter(label=label).exists():
        raise ValueError(f"a version named {label!r} already exists; give this one another name")
    text = content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    needed = {"Year", "ProgramID", "PercentElectric"} | {f"{pa}{k}Factor" for pa in FUNDING_PAS for k in ("Gas", "Electric")}
    missing = needed - set(rows[0].keys() if rows else [])
    if missing:
        raise ValueError(f"the factor file is missing columns: {', '.join(sorted(missing))}")
    problems: list[str] = []
    parsed = []
    replaced = copied = 0
    for i, r in enumerate(rows, start=2):
        where = f"line {i}"
        pe = _factor(r["PercentElectric"], f"{where}: PercentElectric", problems, required=True)
        electric = {pa: _factor(r[f"{pa}ElectricFactor"], f"{where}: {pa}ElectricFactor", problems) for pa in FUNDING_PAS}
        gas = {pa: _factor(r[f"{pa}GasFactor"], f"{where}: {pa}GasFactor", problems) for pa in FUNDING_PAS}
        if not (0 <= pe <= 1):
            problems.append(f"{where}: PercentElectric {pe.normalize():f} is outside 0 to 1")
        # one fuel only: the unused fuel's factors, left blank (or all 0), take the other fuel's (Jake, 2026-11-06)
        if pe == 1 and not any(gas.values()):
            gas, copied = dict(electric), copied + 1
        elif pe == 0 and not any(electric.values()):
            electric, copied = dict(gas), copied + 1
        for problem in (single_fuel_problem(pe, electric, gas), fuel_total_problem("electric", electric), fuel_total_problem("gas", gas)):
            if problem:
                problems.append(f"{where}: {problem}")
        weighted = weighted_factors(pe, electric, gas)
        filed = {pa: r.get(f"{pa}WeightedFactor") for pa in FUNDING_PAS}
        if any(str(v or "").strip() for v in filed.values()):
            try:
                if any(_dec(filed[pa] or "0", where) != weighted[pa] for pa in FUNDING_PAS):
                    replaced += 1
            except ValueError:
                replaced += 1
        parsed.append((r, pe, electric, gas, weighted))
    if problems:
        listed = "; ".join(problems[:MAX_LISTED])
        more = f"; and {len(problems) - MAX_LISTED} more" if len(problems) > MAX_LISTED else ""
        raise ValueError(f"{len(problems)} value{'s' if len(problems) != 1 else ''} to fix: {listed}{more}")
    version = AllocationVersion.objects.create(
        label=label,
        purpose=purpose,
        filing_cycle=filing_cycle,
        notes=notes,
        created_by=actor,
        created_organisation=_acting_for(actor, organisation),
        source_name=source_name,
        source_sha256=hashlib.sha256(content).hexdigest(),
    )
    for r, pe, electric, gas, weighted in parsed:
        entry = AllocationEntry.objects.create(
            version=version,
            year=int(r["Year"]),
            program_id=r["ProgramID"].strip(),
            program_name=(r.get("ProgramName") or "").strip(),
            percent_electric=pe,
        )
        AllocationShare.objects.bulk_create(
            AllocationShare(entry=entry, funding_pa=pa, electric=electric[pa], gas=gas[pa], weighted=weighted[pa]) for pa in FUNDING_PAS
        )
    version.weighted_replaced, version.fuel_copied = replaced, copied
    audit.record(
        "allocation_version.imported",
        obj=version,
        after={
            "label": label,
            "purpose": purpose,
            "entries": len(rows),
            "source": source_name,
            "sha256": version.source_sha256,
            "weighted_replaced": replaced,
            "fuel_copied": copied,
        },
        actor=actor,
        organisation=_acting_for(actor, organisation),
    )
    return version


def check_allocation(version: AllocationVersion) -> list[AllocationIssue]:
    """Each fuel's factors must add up to exactly 1.0000 across the funding PAs (`fuel_total_problem`) and a one-fuel program's two
    fuels' factors must be equal (`single_fuel_problem`); a file breaking either is refused on upload, so this finds them only in
    versions loaded before 2026-11-06 (CEDARS' own table leaves the unused fuel's factors at 0); the
    weighted factors must add up to exactly 1.0000 (or all be 0), and each must be PercentElectric x electric + (1 - PercentElectric) x
    gas to within the steps that made them add up."""
    issues = []
    for e in version.entries.prefetch_related("shares"):
        shares = list(e.shares.all())
        electric = {s.funding_pa: s.electric for s in shares}
        gas = {s.funding_pa: s.gas for s in shares}
        for problem in (
            fuel_total_problem("electric", electric),
            fuel_total_problem("gas", gas),
            single_fuel_problem(e.percent_electric, electric, gas),
        ):
            if problem:
                issues.append(
                    AllocationIssue(
                        e.year, e.program_id, problem[0].upper() + problem[1:].replace(" (leave them blank and they are copied)", "")
                    )
                )
        total = sum(s.weighted for s in shares)
        if total != 0 and total != 1:
            issues.append(AllocationIssue(e.year, e.program_id, f"The weighted factors add up to {total:.4f}, not 1.0000"))
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
def confirm_allocation(actor, version: AllocationVersion, organisation=None) -> AllocationVersion:
    _require_allocation_manager(actor, organisation)
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
    acting = _acting_for(actor, organisation)
    version.status, version.confirmed_by, version.confirmed_at = AllocationVersion.Status.CONFIRMED, actor, timezone.now()
    version.confirmed_organisation = acting
    version.save()
    audit.record("allocation_version.confirmed", obj=version, actor=actor, organisation=acting)
    who = acting.code if acting is not None else "The CPUC"
    _tell_managers(
        version,
        acting,
        f"Statewide allocation version confirmed: {version.label}",
        f"{who} confirmed {version.label} ({version.get_purpose_display().lower()}). Runs that name no version now use it for its years.",
    )
    return version


@transaction.atomic
def discard_draft(actor, version: AllocationVersion, reason: str = "", organisation=None) -> None:
    _require_allocation_manager(actor, organisation)
    version = AllocationVersion.objects.select_for_update().get(pk=version.pk)
    if version.status != AllocationVersion.Status.DRAFT:
        raise AccessDenied("A confirmed allocation version is kept for good.")
    acting = _acting_for(actor, organisation)
    audit.record("allocation_version.discarded", obj=version, reason=reason, actor=actor, organisation=acting)
    version.delete()


def allocation_history(version: AllocationVersion):
    """Every audit event about the version, oldest first: who loaded and confirmed it, for which organization."""
    return audit.changes(version).select_related("actor", "organisation")


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
    cols += [f"{pa}GasFactor" for pa in ALLOCATION_PAS] + [f"{pa}ElectricFactor" for pa in ALLOCATION_PAS]
    cols += [f"{pa}WeightedFactor" for pa in ALLOCATION_PAS] + ["Summary"]
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


# --- the operating budget

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


_reported_spend_sources: list = []


def register_reported_spend(source) -> None:
    """The Monthly reports module's spending: source(organisation, year) returns one dict per program, {prg_id, ytd, through}
    (year-to-date expenditure in Decimal dollars from the PA's latest published report of the year, and that report's month as
    "YYYY-MM"). Dashboards read it through `reported_spend`, so they never import Monthly reports."""
    _reported_spend_sources[:] = [source]


def reported_spend(organisation: Organisation, year: int) -> list[dict]:
    """A PA's monthly-reported year-to-date spending by program; [] when Monthly reports is not installed or nothing is published."""
    if not _reported_spend_sources:
        return []
    return _reported_spend_sources[0](organisation, year)


_evaluation_sources: list = []


def register_evaluations(source) -> None:
    """The Documents & EM&V module's study register: source(program) returns one dict per evaluation study covering the program in
    whole or in part, {study_id, title, url, first_year, last_year, whole, status, lead, deliverable: {title, url, stage} or None}, newest
    first. The program page reads it through `evaluations`, so Programs never imports Documents."""
    _evaluation_sources[:] = [source]


def evaluations(program: Program) -> list[dict]:
    """Every evaluation study that covers the program, with the years it covers; [] when the study register is not installed. For a
    statewide program or an ID that follows one, the studies of the statewide program and all its IDs (`family`), each once."""
    if not _evaluation_sources:
        return []
    members = family(program)
    if len(members) == 1:
        return _evaluation_sources[0](program)
    out, seen = [], set()
    for member in members:
        for e in _evaluation_sources[0](member):
            if e.get("study_id") not in seen:
                seen.add(e.get("study_id"))
                out.append(e)
    return out


def cycle_for_year(year: int) -> FilingCycle | None:
    """The filing cycle a program year belongs to: of the cycles that cover it, the one that starts latest, and of those that start
    the same year, the one whose budget filing was due last (a later filing for the same years supersedes an earlier one)."""
    return (
        FilingCycle.objects.filter(first_year__lte=year, last_year__gte=year)
        .annotate(last_due=Max("deadlines__due_at"))
        .order_by("-first_year", F("last_due").desc(nulls_last=True), "-last_year")
        .first()
    )


# --- what each module holds about one program

PROGRAM_SECTIONS = ("budgets", "fund_shifts", "claims", "monthly", "documents")
_program_record_sources: dict[str, object] = {}


def register_program_records(section: str, source) -> None:
    """A module's records about one program, for the program page: source(program, user, year) returns the rows `user` may read
    (an anonymous user reads published records only), each a dict with at least {title, url, organisation, status, published, when};
    year (or None for every year) narrows them to one program year. The shapes of the other keys are the section's own:

    budgets      one row per version of each budget filing that has the program: cycle, years {year: Decimal budget}, total, standing
    fund_shifts  one row per fund shift with lines on the program: lines [{pa, year, amount}], net
    claims       one row per claims confirmation (year to date) that has the program: year, quarter label, records, expenditures,
                 net_kwh, net_therm, tsb, standing
    monthly      one row per monthly report that has the program: year, month, ytd (spend), committed, standing
    documents    one row per document that names the program: collection, type, years

    The program page reads them through `program_records`, so Programs never imports the filing modules."""
    if section not in PROGRAM_SECTIONS:
        raise ValueError(f"Unknown program section: {section}")
    _program_record_sources[section] = source


def program_records(program: Program, user, year: int | None = None) -> dict[str, list[dict] | None]:
    """{section: rows} for every section; None for a section whose module is not installed."""
    out: dict[str, list[dict] | None] = {}
    for section in PROGRAM_SECTIONS:
        source = _program_record_sources.get(section)
        if source is None:
            out[section] = None
        elif section in SHARED_SECTIONS:
            out[section] = _shared_rows(source, program, user, year)
        else:
            out[section] = list(source(program, user, year))
    return out


SHARED_SECTIONS = ("documents",)  # shown alike on a statewide program and every ID that follows it (Jake, 2026-11-06)


def _shared_rows(source, program: Program, user, year: int | None) -> list[dict]:
    """A section's rows for the statewide program and every ID that follows it, each record once, newest first."""
    members = family(program)
    if len(members) == 1:
        return list(source(program, user, year))
    rows, seen = [], set()
    for member in members:
        for r in source(member, user, year):
            if r["url"] not in seen:
                seen.add(r["url"])
                rows.append(r)
    rows.sort(key=lambda r: r["when"], reverse=True)
    return rows


_document_filing: list = []


def register_document_filing(source) -> None:
    """The document library's "add a document" link for a program page: source(program, user, organisation) returns (url, allowed),
    the address of its new-document form with the program filled in, and whether the user, acting for the organization, may file
    there (a signed-out user: False). The program page reads it through `add_document`, so Programs never imports Documents."""
    _document_filing[:] = [source]


def add_document(program: Program, user, organisation) -> tuple[str, bool] | None:
    """(url, allowed) for adding a document about the program (an ID that follows a statewide program: about the statewide program,
    whose documents every one of its IDs shows), or None when the document library is not installed."""
    if not _document_filing:
        return None
    return _document_filing[0](program.follows if program.follows_id else program, user, organisation)
