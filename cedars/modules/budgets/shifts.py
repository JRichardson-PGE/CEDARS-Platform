"""Fund shifts and the operating budget (BR-14.4, BR-04.7).

A fund shift moves dollars between a PA's programs within a filing cycle, across its program years if need be. It is not part of any
budget filing and has no cost-effectiveness run. Its lines must add up to zero: an unbalanced shift is rejected when submitted and stays
a draft. A shift that touches a statewide program waits until every other PA funding that program in that year (its allocation factors)
has acknowledged it; one with none takes effect when submitted.

    draft (lines edited) -> submitted -> waiting for co-funders -> in effect
                                       \\-> in effect (no co-funders)
    draft or waiting -> withdrawn

The approved budget (the standing filing, BR-04.2) never changes. The operating budget is the approved budget plus the shifts that took
effect after that filing was confirmed: a later filing is taken to carry the earlier shifts in its own numbers."""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications
from cedars.services.registry import service as registry
from cedars.services.registry.models import AllocationShare, FilingCycle, Program

from . import service
from .models import FundShift, FundShiftAcknowledgement, FundShiftLine

MODULE = service.MODULE


class ShiftInvalid(ValueError):
    """The shift's lines or totals do not pass its checks; `problems` says which."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --- who sees what


def can_submit(user, organisation) -> bool:
    return organisation is not None and access.has_permission(user, organisation, f"{MODULE}.submit")


def can_read(user, shift: FundShift) -> bool:
    """The organisation's readers, CPUC staff, and once submitted, the co-funders asked to acknowledge it."""
    if service._org_reader(user, shift.organisation) or service._cpuc_reader(user):
        return True
    if shift.status == FundShift.Status.DRAFT:
        return False
    codes = set(shift.cofunders or {})
    return any(o.code in codes and service._org_reader(user, o) for o in access.organisations_of(user))


def readable(user, organisation=None):
    """The shifts the user may read, newest first: CPUC staff see every PA's; others their organisations' and those they co-fund."""
    qs = FundShift.objects.select_related("organisation", "cycle")
    if service._cpuc_reader(user):
        return qs
    orgs = [o for o in access.organisations_of(user) if service._org_reader(user, o)]
    asked = Q(cofunders__has_any_keys=[o.code for o in orgs]) & ~Q(status=FundShift.Status.DRAFT)
    return qs.filter(Q(organisation__in=orgs) | asked) if orgs else qs.none()


def waiting_for(organisation):
    """Shifts waiting for the organisation's acknowledgement."""
    if organisation is None:
        return []
    return list(
        FundShift.objects.filter(status=FundShift.Status.WAITING, cofunders__has_key=organisation.code)
        .exclude(acknowledgements__organisation=organisation)
        .select_related("organisation", "cycle")
    )


def cycles():
    """The filing cycles a shift can be made in: those with a budget filing."""
    return FilingCycle.objects.filter(deadlines__module=MODULE).distinct().order_by("-first_year")


# --- making a shift


def _fields(shift: FundShift) -> dict:
    return {
        "status": shift.status,
        "title": shift.title,
        "cycle": str(shift.cycle),
        "lines": [{"prg_id": ln.prg_id, "year": ln.year, "amount": str(ln.amount)} for ln in shift.lines.all()],
    }


def _require_submit(actor, organisation) -> None:
    if not can_submit(actor, organisation):
        raise AccessDenied(f"Making {organisation.code}'s fund shifts needs Budgets: Submit in {organisation.code}.")


def _require_draft(shift: FundShift) -> None:
    if shift.status != FundShift.Status.DRAFT:
        raise AccessDenied(f"This fund shift is {shift.get_status_display().lower()}; only a draft can be changed.")


@transaction.atomic
def create(actor, organisation, cycle: FilingCycle, *, title: str, reason: str) -> FundShift:
    _require_submit(actor, organisation)
    if not title.strip() or not reason.strip():
        raise ShiftInvalid(["A fund shift needs a title and a reason."])
    shift = FundShift.objects.create(organisation=organisation, cycle=cycle, title=title.strip(), reason=reason.strip(), created_by=actor)
    audit.record("fund_shift.created", obj=shift, after=_fields(shift), reason=shift.reason, actor=actor, organisation=organisation)
    return shift


@dataclass
class LineIn:
    """One line as entered: PrgID, year and amount as text."""

    prg_id: str
    year: str
    amount: str

    def blank(self) -> bool:
        return not (self.prg_id.strip() or self.year.strip() or self.amount.strip())


def _parse(shift: FundShift, entered: list[LineIn]) -> tuple[list[tuple[str, int, Decimal]], list[str]]:
    """The lines to keep and what is wrong with them. A PrgID must be in the registry and be the PA's own or a statewide program; the
    year in the shift's cycle; the amount a non-zero dollar figure; each program-year once."""
    problems, out, seen = [], [], set()
    rows = [(n, ln) for n, ln in enumerate(entered, start=1) if not ln.blank()]
    programs = {p.prg_id: p for p in Program.objects.filter(prg_id__in={ln.prg_id.strip() for _, ln in rows})}
    for n, ln in rows:
        prg = ln.prg_id.strip()
        p = programs.get(prg)
        if p is None:
            problems.append(f"Line {n}: PrgID {prg or '(blank)'} is not in the program registry.")
        elif not p.statewide and p.administrator_id != shift.organisation_id:
            problems.append(f"Line {n}: {prg} is another program administrator's program.")
        year = int(ln.year) if ln.year.strip().isdigit() else None
        if year is None or not shift.cycle.covers(year):
            problems.append(f"Line {n}: the year must be one of {shift.cycle.first_year} to {shift.cycle.last_year}.")
        try:
            amount = Decimal(ln.amount.replace(",", "").replace("$", "").strip()).quantize(Decimal("0.01"))
        except InvalidOperation:
            amount = None
            problems.append(f"Line {n}: the amount must be a dollar figure, negative to move money out of the program.")
        if amount == 0:
            problems.append(f"Line {n}: the amount cannot be zero.")
        if p is not None and year is not None:
            if (prg, year) in seen:
                problems.append(f"Line {n}: {prg} {year} is on more than one line; put its whole amount on one.")
            seen.add((prg, year))
        if p is not None and year is not None and amount:
            out.append((prg, year, amount))
    return out, problems


@transaction.atomic
def set_lines(actor, shift: FundShift, entered: list[LineIn]) -> FundShift:
    """Replace the draft's lines. Nothing is saved when any line has a problem (ShiftInvalid lists them)."""
    shift = FundShift.objects.select_for_update().get(pk=shift.pk)
    _require_submit(actor, shift.organisation)
    _require_draft(shift)
    lines, problems = _parse(shift, entered)
    if problems:
        raise ShiftInvalid(problems)
    before = _fields(shift)
    shift.lines.all().delete()
    FundShiftLine.objects.bulk_create(FundShiftLine(shift=shift, prg_id=p, year=y, amount=a) for p, y, a in lines)
    audit.record("fund_shift.lines_set", obj=shift, before=before, after=_fields(shift), actor=actor, organisation=shift.organisation)
    return shift


@dataclass
class Totals:
    moved_in: Decimal = Decimal(0)
    moved_out: Decimal = Decimal(0)

    @property
    def net(self) -> Decimal:
        return self.moved_in + self.moved_out


def totals(shift: FundShift) -> Totals:
    t = Totals()
    for ln in shift.lines.all():
        if ln.amount > 0:
            t.moved_in += ln.amount
        else:
            t.moved_out += ln.amount
    return t


def problems(shift: FundShift) -> list[str]:
    """Why the shift cannot be submitted: fewer than two lines, or lines that do not add up to zero (BR-14.4)."""
    lines = list(shift.lines.all())
    out = []
    if len(lines) < 2:
        out.append("A fund shift moves money from at least one program-year to another: it needs two lines or more.")
    t = totals(shift)
    if t.net != 0:
        out.append(
            f"The shift does not balance: {service_money(t.moved_in)} in and {service_money(-t.moved_out)} out, "
            f"{service_money(t.net)} net. The lines must add up to zero."
        )
    return out


def warnings(shift: FundShift) -> list[str]:
    """Lines that move more out of a program-year than its operating budget holds (not blocking: the approved budget may be filed later
    or the PA may mean it)."""
    ob = operating_budget(shift.organisation, shift.cycle)
    have = {(r.prg_id, r.year): r.operating for r in ob.rows}
    out = []
    for ln in shift.lines.all():
        if ln.amount < 0 and have.get((ln.prg_id, ln.year), Decimal(0)) + ln.amount < 0:
            out.append(
                f"{ln.prg_id} {ln.year}: moves {service_money(-ln.amount)} out of an operating budget of "
                f"{service_money(have.get((ln.prg_id, ln.year), Decimal(0)))}."
            )
    return out


def service_money(x: Decimal) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def _allocation_for(shift: FundShift, year: int):
    """The allocation factors a statewide line is read against: the standing filing's pinned version, else the most recent confirmed
    one for the year (as a run naming none)."""
    v = service.standing(shift.organisation, shift.cycle)
    setup = getattr(v.submission.deadline, "budget_setup", None) if v else None
    pinned = setup.allocation_version if setup else None
    if pinned is not None and pinned.entries.filter(year=year).exists():
        return pinned
    return registry.most_recent_allocation(year)


def cofunders(shift: FundShift) -> dict[str, list[str]]:
    """{funding PA code: ['PrgID year', ...]}: every other PA with a non-zero share of a statewide program-year the shift touches."""
    out: dict[str, list[str]] = defaultdict(list)
    statewide = set(Program.objects.filter(statewide=True, prg_id__in=shift.lines.values("prg_id")).values_list("prg_id", flat=True))
    for ln in shift.lines.all():
        if ln.prg_id not in statewide:
            continue
        version = _allocation_for(shift, ln.year)
        if version is None:
            continue
        shares = AllocationShare.objects.filter(entry__version=version, entry__year=ln.year, entry__program_id=ln.prg_id).exclude(
            weighted=0, electric=0, gas=0
        )
        for code in shares.values_list("funding_pa", flat=True):
            if code != shift.organisation.code:
                out[code].append(f"{ln.prg_id} {ln.year}")
    return {k: sorted(set(v)) for k, v in sorted(out.items())}


def _link(shift: FundShift) -> str:
    return reverse("budgets:shift", args=[shift.pk])


def _notify(organisation, permission: str, title: str, body: str, shift: FundShift) -> None:
    for user in access.holders(organisation, permission):
        notifications.notify(user, title, body, _link(shift), key=f"fund-shift-{shift.pk}-{organisation.code}-{shift.status}")


def submit(actor, shift: FundShift) -> FundShift:
    """Submit a draft. An unbalanced shift is rejected (it stays a draft and the rejection is audited); a balanced one takes effect, or
    waits for its co-funders, who are notified."""
    shift = FundShift.objects.get(pk=shift.pk)
    _require_submit(actor, shift.organisation)
    _require_draft(shift)
    found = problems(shift)
    if found:
        audit.record("fund_shift.rejected", obj=shift, after={"problems": found}, actor=actor, organisation=shift.organisation)
        raise ShiftInvalid(found)
    return _submit(actor, shift)


@transaction.atomic
def _submit(actor, shift: FundShift) -> FundShift:
    shift = FundShift.objects.select_for_update().get(pk=shift.pk)
    _require_draft(shift)
    if problems(shift):  # changed since the check
        raise ShiftInvalid(problems(shift))
    before = _fields(shift)
    shift.cofunders = cofunders(shift)
    shift.submitted_by, shift.submitted_at = actor, timezone.now()
    if shift.cofunders:
        shift.status = FundShift.Status.WAITING
    else:
        shift.status, shift.effective_at = FundShift.Status.EFFECTIVE, shift.submitted_at
    shift.save()
    audit.record(
        "fund_shift.submitted",
        obj=shift,
        before=before,
        after={**_fields(shift), "cofunders": shift.cofunders},
        actor=actor,
        organisation=shift.organisation,
    )
    if shift.status == FundShift.Status.WAITING:
        for code, items in shift.cofunders.items():
            org = Organisation.objects.filter(code=code).first()
            if org is not None:
                _notify(
                    org,
                    f"{MODULE}.confirm",
                    f"{shift.organisation.code} fund shift waiting for {code}",
                    f"{shift.title}. It moves {code}'s share of {', '.join(items)}; it takes effect once every co-funder acknowledges it.",
                    shift,
                )
    return shift


def can_acknowledge(user, shift: FundShift, organisation) -> bool:
    return (
        organisation is not None
        and shift.status == FundShift.Status.WAITING
        and organisation.code in (shift.cofunders or {})
        and access.has_permission(user, organisation, f"{MODULE}.confirm")
        and not shift.acknowledgements.filter(organisation=organisation).exists()
    )


def outstanding(shift: FundShift) -> list[str]:
    """The co-funders that have not acknowledged yet."""
    done = set(shift.acknowledgements.values_list("organisation__code", flat=True))
    return [c for c in (shift.cofunders or {}) if c not in done]


@transaction.atomic
def acknowledge(actor, shift: FundShift, organisation, note: str = "") -> FundShift:
    """A co-funder marks the shift complete for its share. With the last one the shift takes effect and its PA is notified."""
    shift = FundShift.objects.select_for_update().get(pk=shift.pk)
    if not can_acknowledge(actor, shift, organisation):
        raise AccessDenied(f"{organisation.code if organisation else 'You'} cannot acknowledge this fund shift.")
    ack = FundShiftAcknowledgement.objects.create(shift=shift, organisation=organisation, acknowledged_by=actor, note=note.strip())
    audit.record(
        "fund_shift.acknowledged",
        obj=shift,
        after={"organisation": organisation.code, "note": ack.note},
        actor=actor,
        organisation=organisation,
    )
    if not outstanding(shift):
        shift.status, shift.effective_at = FundShift.Status.EFFECTIVE, timezone.now()
        shift.save(update_fields=["status", "effective_at"])
        audit.record("fund_shift.effective", obj=shift, after=_fields(shift), actor=actor, organisation=organisation)
        _notify(
            shift.organisation, f"{MODULE}.submit", "Fund shift in effect", f"{shift.title}: every co-funder has acknowledged it.", shift
        )
    return shift


@transaction.atomic
def withdraw(actor, shift: FundShift, reason: str) -> FundShift:
    """Withdraw a draft or a shift still waiting for co-funders. One in effect is undone by a new shift the other way."""
    shift = FundShift.objects.select_for_update().get(pk=shift.pk)
    _require_submit(actor, shift.organisation)
    if shift.status not in (FundShift.Status.DRAFT, FundShift.Status.WAITING):
        raise AccessDenied("A fund shift in effect is not withdrawn: make a new shift that moves the money back.")
    if not reason.strip():
        raise ShiftInvalid(["Say why the shift is withdrawn."])
    before = _fields(shift)
    shift.status, shift.withdrawn_reason = FundShift.Status.WITHDRAWN, reason.strip()
    shift.save(update_fields=["status", "withdrawn_reason"])
    audit.record(
        "fund_shift.withdrawn",
        obj=shift,
        before=before,
        after=_fields(shift),
        reason=shift.withdrawn_reason,
        actor=actor,
        organisation=shift.organisation,
    )
    return shift


# --- the operating budget


@dataclass
class OperatingRow:
    prg_id: str
    year: int
    name: str = ""
    approved: Decimal = Decimal(0)
    shifted: Decimal = Decimal(0)

    @property
    def operating(self) -> Decimal:
        return self.approved + self.shifted


@dataclass
class OperatingBudget:
    organisation: Organisation
    cycle: FilingCycle
    standing: object  # SubmissionVersion | None
    rows: list[OperatingRow]
    shifts: list[FundShift]
    earlier: list[FundShift] = field(default_factory=list)

    def total(self, years=None) -> OperatingRow:
        t = OperatingRow("Total", 0)
        for r in self.rows:
            if not years or r.year in years:
                t.approved += r.approved
                t.shifted += r.shifted
        return t

    @property
    def years(self) -> list[int]:
        return list(range(self.cycle.first_year, self.cycle.last_year + 1))


def operating_budget(organisation, cycle: FilingCycle) -> OperatingBudget:
    """The PA's operating budget by program and year (BR-04.7): its standing filing's budget lines (approved), plus the shifts in effect
    since that filing was confirmed. Without a standing filing, every shift in effect counts against an approved budget of zero."""
    v = service.standing(organisation, cycle)
    rows: dict[tuple[str, int], OperatingRow] = {}
    if v is not None:
        service.ensure_lines(v)
        for ln in v.budget_lines.all():
            r = rows.setdefault((ln.prg_id, ln.year), OperatingRow(ln.prg_id, ln.year))
            r.approved += sum((Decimal(x) for x in (ln.program_costs or {}).values()), Decimal(0)) + ln.rebates_and_incentives
    effective = list(
        FundShift.objects.filter(organisation=organisation, cycle=cycle, status=FundShift.Status.EFFECTIVE)
        .order_by("effective_at")
        .prefetch_related("lines")
    )
    since = v.confirmed_at if v is not None else None
    applied = [s for s in effective if since is None or s.effective_at >= since]
    earlier = [s for s in effective if s not in applied]
    for s in applied:
        for ln in s.lines.all():
            rows.setdefault((ln.prg_id, ln.year), OperatingRow(ln.prg_id, ln.year)).shifted += ln.amount
    names = {}
    for p in Program.objects.filter(prg_id__in={k[0] for k in rows}).prefetch_related("versions"):
        versions = sorted(p.versions.all(), key=lambda x: x.valid_from, reverse=True)
        names[p.prg_id] = {y: pv.name for y in range(cycle.first_year, cycle.last_year + 1) if (pv := registry._for_year(versions, y))}
    for r in rows.values():
        r.name = names.get(r.prg_id, {}).get(r.year, "")
    return OperatingBudget(organisation, cycle, v, sorted(rows.values(), key=lambda r: (r.prg_id, r.year)), applied, earlier)


def programs_for(shift: FundShift) -> list[tuple[str, str]]:
    """The programs a shift of this PA can name (its own and the statewide ones), for the entry page's suggestions."""
    qs = Program.objects.filter(Q(administrator=shift.organisation) | Q(statewide=True), portfolio_cost=False).prefetch_related("versions")
    out = []
    for p in qs:
        versions = sorted(p.versions.all(), key=lambda x: x.valid_from, reverse=True)
        pv = registry._for_year(versions, shift.cycle.first_year) or (versions[0] if versions else None)
        out.append((p.prg_id, pv.name if pv else ""))
    return out


def can_read_operating(user, organisation) -> bool:
    return service._org_reader(user, organisation) or service._cpuc_reader(user)
