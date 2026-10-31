"""Fund shifts and the operating budget (BR-14.4, BR-04.7).

A fund shift moves dollars between a PA's programs within a filing cycle, across its program years if need be. It is not part of any
budget filing and has no cost-effectiveness run. Each PA's part of it must add up to zero: an unbalanced shift is rejected when
submitted and stays a draft.

Money moved into or out of a statewide program changes every funding PA's portion of it, by its weighted allocation factor for that
program and year (the standing filing's pinned allocation version, else the most recent confirmed one). The submitting PA balances its
own portion with its own lines. Each other funding PA (a co-funder) is notified, chooses where its portion comes from or goes among its
own programs, and acknowledges; the shift takes effect when the last one has (Jake, 2026-10-30).

    draft (lines edited) -> submitted -> waiting for co-funders -> in effect
                                       \\-> in effect (no co-funders)
    draft or waiting -> withdrawn

The approved budget (the standing filing, BR-04.2) never changes. A PA's operating budget is its approved budget plus its part of the
shifts that took effect after that filing was confirmed: a later filing is taken to carry the earlier shifts in its own numbers, and the
pages say so wherever it matters (Jake, 2026-10-30)."""

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
    """The organization's readers, CPUC staff, and once submitted, the co-funders asked to acknowledge it."""
    if service._org_reader(user, shift.organisation) or service._cpuc_reader(user):
        return True
    if shift.status == FundShift.Status.DRAFT:
        return False
    codes = set(shift.cofunders)
    return any(o.code in codes and service._org_reader(user, o) for o in access.organisations_of(user))


def readable(user, organisation=None):
    """The shifts the user may read, newest first: CPUC staff see every PA's; others their organizations' and those they co-fund."""
    qs = FundShift.objects.select_related("organisation", "cycle")
    if service._cpuc_reader(user):
        return qs
    orgs = [o for o in access.organisations_of(user) if service._org_reader(user, o)]
    asked = Q(shares__has_any_keys=[o.code for o in orgs]) & ~Q(status=FundShift.Status.DRAFT)
    return qs.filter(Q(organisation__in=orgs) | asked) if orgs else qs.none()


def waiting_for(organisation):
    """Shifts waiting for the organization's acknowledgment."""
    if organisation is None:
        return []
    return list(
        FundShift.objects.filter(status=FundShift.Status.WAITING, shares__has_key=organisation.code)
        .exclude(organisation=organisation)
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
        "lines": [{"pa": ln.pa, "prg_id": ln.prg_id, "year": ln.year, "amount": str(ln.amount)} for ln in shift.lines.all()],
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


def _parse(shift: FundShift, entered: list[LineIn], organisation, *, statewide: bool) -> tuple[list[tuple[str, int, Decimal]], list[str]]:
    """The lines to keep and what is wrong with them. A PrgID must be in the registry and be the PA's own (or, when `statewide`, a
    statewide program); the year in the shift's cycle; the amount a non-zero dollar figure; each program-year once."""
    problems, out, seen = [], [], set()
    rows = [(n, ln) for n, ln in enumerate(entered, start=1) if not ln.blank()]
    programs = {p.prg_id: p for p in Program.objects.filter(prg_id__in={ln.prg_id.strip() for _, ln in rows})}
    for n, ln in rows:
        prg = ln.prg_id.strip()
        p = programs.get(prg)
        if p is None:
            problems.append(f"Line {n}: PrgID {prg or '(blank)'} is not in the program registry.")
        elif p.statewide and not statewide:
            problems.append(f"Line {n}: {prg} is a statewide program; place {organisation.code}'s portion in its own programs.")
        elif not p.statewide and p.administrator_id != organisation.pk:
            problems.append(f"Line {n}: {prg} is not one of {organisation.code}'s programs.")
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


def _replace_lines(shift: FundShift, pa: str, lines) -> None:
    shift.lines.filter(pa=pa).delete()
    FundShiftLine.objects.bulk_create(FundShiftLine(shift=shift, pa=pa, prg_id=p, year=y, amount=a) for p, y, a in lines)


@transaction.atomic
def set_lines(actor, shift: FundShift, entered: list[LineIn]) -> FundShift:
    """Replace the draft's lines. Nothing is saved when any line has a problem (ShiftInvalid lists them)."""
    shift = FundShift.objects.select_for_update().get(pk=shift.pk)
    _require_submit(actor, shift.organisation)
    _require_draft(shift)
    lines, problems = _parse(shift, entered, shift.organisation, statewide=True)
    if problems:
        raise ShiftInvalid(problems)
    before = _fields(shift)
    _replace_lines(shift, shift.organisation.code, lines)
    audit.record("fund_shift.lines_set", obj=shift, before=before, after=_fields(shift), actor=actor, organisation=shift.organisation)
    return shift


def statewide_ids(prg_ids) -> set[str]:
    return set(Program.objects.filter(statewide=True, prg_id__in=set(prg_ids)).values_list("prg_id", flat=True))


@dataclass
class Part:
    """One PA's part of a shift: its own lines (statewide lines aside) and its portion of the statewide lines."""

    pa: str
    moved_in: Decimal = Decimal(0)
    moved_out: Decimal = Decimal(0)
    portion: Decimal = Decimal(0)
    lines: list = field(default_factory=list)

    @property
    def net(self) -> Decimal:
        return self.moved_in + self.moved_out + self.portion


def compute_shares(shift: FundShift) -> tuple[dict[str, dict[str, Decimal]], list[str]]:
    """{funding PA: {'PrgID year': its portion}} of the shift's statewide lines, by weighted allocation factor (rounded to cents),
    and the statewide lines that have no factors to split them by."""
    shares: dict[str, dict[str, Decimal]] = defaultdict(dict)
    problems = []
    own = [ln for ln in shift.lines.all() if ln.pa == shift.organisation.code]
    sw = statewide_ids(ln.prg_id for ln in own)
    for ln in own:
        if ln.prg_id not in sw:
            continue
        version = _allocation_for(shift, ln.year)
        rows = (
            list(
                AllocationShare.objects.filter(entry__version=version, entry__year=ln.year, entry__program_id=ln.prg_id).exclude(weighted=0)
            )
            if version
            else []
        )
        if not rows:
            where = f"in {version.label}" if version else "in any confirmed allocation version"
            problems.append(f"{ln.prg_id} {ln.year} has no allocation factors {where}, so its funding PAs' portions are unknown.")
            continue
        for r in rows:
            shares[r.funding_pa][f"{ln.prg_id} {ln.year}"] = (ln.amount * r.weighted).quantize(Decimal("0.01"))
    return {k: dict(v) for k, v in sorted(shares.items())}, problems


def frozen_shares(shift: FundShift) -> dict[str, dict[str, Decimal]]:
    """The shares recorded at submission, or for a draft the ones it would have now."""
    if shift.status == FundShift.Status.DRAFT:
        return compute_shares(shift)[0]
    return {pa: {k: Decimal(v) for k, v in items.items()} for pa, items in (shift.shares or {}).items()}


def parts(shift: FundShift) -> list[Part]:
    """Each PA's part: the shift's PA first, then each co-funder."""
    shares = frozen_shares(shift)
    lines = list(shift.lines.all())
    sw = statewide_ids(ln.prg_id for ln in lines)
    out = {shift.organisation.code: Part(shift.organisation.code)}
    for code in sorted(shares):
        out.setdefault(code, Part(code))
    for code, items in shares.items():
        out[code].portion = sum(items.values(), Decimal(0))
    for ln in lines:
        part = out.setdefault(ln.pa, Part(ln.pa))
        part.lines.append(ln)
        if ln.prg_id in sw:
            continue
        if ln.amount > 0:
            part.moved_in += ln.amount
        else:
            part.moved_out += ln.amount
    return list(out.values())


def _unbalanced(part: Part, organisation_code: str) -> str:
    portion = f", with its {service_money(part.portion)} portion of the statewide lines" if part.portion else ""
    return (
        f"{part.pa}'s part does not balance: {service_money(part.moved_in)} into its programs and {service_money(-part.moved_out)} out"
        f"{portion}, {service_money(part.net)} net. "
        + ("Its lines must add up to zero." if part.pa == organisation_code else "Its lines must place exactly its portion.")
    )


def problems(shift: FundShift) -> list[str]:
    """Why the shift cannot be submitted: fewer than two lines, statewide lines with no factors, or the shift's PA's part not adding up
    to zero with its portion of the statewide lines (BR-14.4)."""
    own = [ln for ln in shift.lines.all() if ln.pa == shift.organisation.code]
    out = []
    if len(own) < 2:
        out.append("A fund shift moves money from at least one program-year to another: it needs two lines or more.")
    _, missing = compute_shares(shift)
    out += missing
    mine = parts(shift)[0]
    if mine.net != 0:
        out.append(_unbalanced(mine, shift.organisation.code))
    return out


def warnings(shift: FundShift, organisation=None) -> list[str]:
    """The PA's lines that move more out of a program-year than its operating budget holds (not blocking: the approved budget may be
    filed later, or the PA may mean it)."""
    organisation = organisation or shift.organisation
    ob = operating_budget(organisation, shift.cycle)
    have = {(r.prg_id, r.year): r.operating for r in ob.rows}
    out = []
    for ln in shift.lines.filter(pa=organisation.code):
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
    shift.shares = {pa: {k: str(v) for k, v in items.items()} for pa, items in compute_shares(shift)[0].items()}
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
        after={**_fields(shift), "shares": shift.shares},
        actor=actor,
        organisation=shift.organisation,
    )
    if shift.status == FundShift.Status.WAITING:
        for code in shift.cofunders:
            org = access.by_code(code)
            items = shift.shares[code]
            portion = sum((Decimal(v) for v in items.values()), Decimal(0))
            if org is not None:
                _notify(
                    org,
                    f"{MODULE}.confirm",
                    f"{shift.organisation.code} fund shift waiting for {code}",
                    f"{shift.title}. It changes {code}'s portion of {', '.join(items)} by {service_money(portion)}. Choose where that "
                    f"comes from or goes among {code}'s programs and acknowledge it; it takes effect once every co-funder has.",
                    shift,
                )
    return shift


def can_acknowledge(user, shift: FundShift, organisation) -> bool:
    return (
        organisation is not None
        and shift.status == FundShift.Status.WAITING
        and organisation.code in shift.cofunders
        and access.has_permission(user, organisation, f"{MODULE}.confirm")
        and not shift.acknowledgements.filter(organisation=organisation).exists()
    )


def outstanding(shift: FundShift) -> list[str]:
    """The co-funders that have not acknowledged yet."""
    done = set(shift.acknowledgements.values_list("organisation__code", flat=True))
    return [c for c in shift.cofunders if c not in done]


def portion(shift: FundShift, code: str) -> Decimal:
    return sum((Decimal(v) for v in (shift.shares or {}).get(code, {}).values()), Decimal(0))


@transaction.atomic
def acknowledge(actor, shift: FundShift, organisation, entered: list[LineIn] = (), note: str = "") -> FundShift:
    """A co-funder places its portion of the statewide lines among its own programs (lines adding up to minus its portion: a larger
    portion is paid for by moving money out of its programs) and marks the shift complete for it. With the last one the shift takes
    effect and its PA is notified. Nothing is saved when a line has a problem or the lines do not place the portion exactly."""
    shift = FundShift.objects.select_for_update().get(pk=shift.pk)
    if not can_acknowledge(actor, shift, organisation):
        raise AccessDenied(f"{organisation.code if organisation else 'You'} cannot acknowledge this fund shift.")
    lines, found = _parse(shift, list(entered), organisation, statewide=False)
    owed = portion(shift, organisation.code)
    placed = sum((a for _, _, a in lines), Decimal(0))
    if not found and placed + owed != 0:
        found.append(
            f"{organisation.code}'s portion changes by {service_money(owed)}: its lines must add up to {service_money(-owed)} "
            f"(they add up to {service_money(placed)})."
        )
    if found:
        raise ShiftInvalid(found)
    _replace_lines(shift, organisation.code, lines)
    ack = FundShiftAcknowledgement.objects.create(shift=shift, organisation=organisation, acknowledged_by=actor, note=note.strip())
    audit.record(
        "fund_shift.acknowledged",
        obj=shift,
        after={
            "organisation": organisation.code,
            "note": ack.note,
            "portion": str(owed),
            "lines": [{"prg_id": p, "year": y, "amount": str(a)} for p, y, a in lines],
        },
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


def _key(item: str) -> tuple[str, int]:
    prg, year = item.rsplit(" ", 1)
    return prg, int(year)


def operating_budget(organisation, cycle: FilingCycle) -> OperatingBudget:
    """The PA's operating budget by program and year (BR-04.7): its standing filing's budget lines (approved), plus its part of the
    shifts in effect since that filing was confirmed: its own lines, and its portion of each statewide line (by allocation factor) on
    the statewide program's row. A shift in effect before the standing filing was confirmed is taken to be in that filing's numbers.
    Without a standing filing, every shift in effect counts against an approved budget of zero."""
    v = service.standing(organisation, cycle)
    rows: dict[tuple[str, int], OperatingRow] = {}
    if v is not None:
        service.ensure_lines(v)
        for ln in v.budget_lines.all():
            r = rows.setdefault((ln.prg_id, ln.year), OperatingRow(ln.prg_id, ln.year))
            r.approved += sum((Decimal(x) for x in (ln.program_costs or {}).values()), Decimal(0)) + ln.rebates_and_incentives
    effective = list(
        FundShift.objects.filter(Q(organisation=organisation) | Q(shares__has_key=organisation.code), cycle=cycle)
        .filter(status=FundShift.Status.EFFECTIVE)
        .order_by("effective_at")
        .select_related("organisation")
        .prefetch_related("lines")
    )
    since = v.confirmed_at if v is not None else None
    applied = [s for s in effective if since is None or s.effective_at >= since]
    earlier = [s for s in effective if s not in applied]
    sw = statewide_ids(ln.prg_id for s in applied for ln in s.lines.all())
    for s in applied:
        for ln in s.lines.all():
            if ln.pa == organisation.code and ln.prg_id not in sw:
                rows.setdefault((ln.prg_id, ln.year), OperatingRow(ln.prg_id, ln.year)).shifted += ln.amount
        for item, amount in (s.shares or {}).get(organisation.code, {}).items():
            rows.setdefault(_key(item), OperatingRow(*_key(item))).shifted += Decimal(amount)
    names = {}
    for p in Program.objects.filter(prg_id__in={k[0] for k in rows}).prefetch_related("versions"):
        versions = sorted(p.versions.all(), key=lambda x: x.valid_from, reverse=True)
        names[p.prg_id] = {y: pv.name for y in range(cycle.first_year, cycle.last_year + 1) if (pv := registry._for_year(versions, y))}
    for r in rows.values():
        r.name = names.get(r.prg_id, {}).get(r.year, "")
    return OperatingBudget(organisation, cycle, v, sorted(rows.values(), key=lambda r: (r.prg_id, r.year)), applied, earlier)


def operating_rows(organisation, cycle: FilingCycle) -> list[dict]:
    """The operating budget as plain rows, for the registry service's hook (registry.operating_budget): other modules read it there."""
    return [
        {"prg_id": r.prg_id, "year": r.year, "name": r.name, "approved": r.approved, "shifted": r.shifted, "operating": r.operating}
        for r in operating_budget(organisation, cycle).rows
    ]


def programs_for(shift: FundShift, organisation=None) -> list[tuple[str, str]]:
    """The programs the PA can name in the shift, for the entry page's suggestions: the shift's PA its own and the statewide ones, a
    co-funder its own."""
    organisation = organisation or shift.organisation
    where = Q(administrator=organisation) | (Q(statewide=True) if organisation == shift.organisation else Q(pk__in=[]))
    qs = Program.objects.filter(where, portfolio_cost=False).prefetch_related("versions")
    out = []
    for p in qs:
        versions = sorted(p.versions.all(), key=lambda x: x.valid_from, reverse=True)
        pv = registry._for_year(versions, shift.cycle.first_year) or (versions[0] if versions else None)
        out.append((p.prg_id, pv.name if pv else ""))
    return out


def can_read_operating(user, organisation) -> bool:
    return service._org_reader(user, organisation) or service._cpuc_reader(user)
