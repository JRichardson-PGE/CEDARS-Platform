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
from django.db.models import Q
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied

from .models import AllocationEntry, AllocationShare, AllocationVersion, FilingCycle, Program, ProgramVersion, ReportingPeriod

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


def catalog(year: int | None = None):
    """(program, version) for every program not flagged as a portfolio cost, closed programs included (BR-13.5)."""
    out = []
    for p in Program.objects.filter(portfolio_cost=False).select_related("administrator"):
        v = version_for_year(p, year) if year else (current_version(p) or p.versions.order_by("-valid_from").first())
        out.append((p, v))
    return out


def _can_edit(actor, program: Program) -> bool:
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
        access.is_system_admin(actor) or (administrator and _can_edit(actor, Program(administrator=administrator)))
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
    start, because versions already in force are history."""
    if not _can_edit(actor, program):
        raise AccessDenied(
            f"Only {program.administrator.code if program.administrator else 'the CPUC'}'s administrators can change {program}."
        )
    if not reason.strip():
        raise AccessDenied("Give a reason for the change.")
    _clean(changes)
    latest = program.versions.select_for_update().order_by("-valid_from").first()
    if latest is None:
        raise ValueError(f"{program} has no versions")
    if effective <= latest.valid_from:
        raise AccessDenied(f"A change must start after {latest.valid_from:%Y-%m-%d}, when the latest version started.")
    if latest.valid_to is not None and effective > latest.valid_to + timedelta(days=1):
        raise AccessDenied(f"{program}'s versions end on {latest.valid_to:%Y-%m-%d}; a change cannot leave a gap.")
    before = audit.snapshot(latest, ["valid_from", "valid_to", *VERSION_FIELDS])
    values = {f: getattr(latest, f) for f in VERSION_FIELDS} | changes
    ProgramVersion.objects.filter(pk=latest.pk).update(valid_to=effective - timedelta(days=1))
    new = ProgramVersion.objects.create(
        program=program, valid_from=effective, valid_to=latest.valid_to, created_by=actor, reason=reason, **values
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
