"""Fund shifts and the operating budget.

A fund shift moves dollars between a PA's programs within a filing cycle, across its program years if need be. It is not part of any
budget filing and has no cost-effectiveness run. Each PA's part of it must add up to zero: an unbalanced shift is rejected when
submitted and stays a draft.

Money moved into or out of a statewide program is entered as the change to the whole program; it changes every funding PA's portion
of it, by its weighted allocation factor for that program and year (the standing filing's pinned allocation version, else the most
recent confirmed one; the cents left by rounding go to the shift's own PA). The submitting PA balances its own portion with its own
lines. Each other funding PA with a net gap (a co-funder) is notified, chooses where its portion comes from or goes among its own
programs, and acknowledges; the shift takes effect when the last one has (Jake, 2026-10-30 and 2026-11-02). Money moved between two
statewide programs with different allocation rates leaves each funder a net gap, which it covers the same way; a funder whose
portions cancel out is informed and has nothing to acknowledge.

No fund shift may leave a program-year's operating budget below zero (Jake, 2026-11-02): a line or a portion that would is rejected,
at submission for every PA's lines and portions and again when a co-funder places its lines. The lines of shifts still waiting for
co-funders count as spent.

    draft (lines edited) -> submitted -> waiting for co-funders -> in effect
                                       \\-> in effect (no co-funders)
    draft or waiting -> withdrawn

The approved budget (the standing filing) never changes. A PA's operating budget is its approved budget plus its part of the
shifts that took effect after that filing was confirmed: a later filing is taken to carry the earlier shifts in its own numbers, and the
pages say so wherever it matters (Jake, 2026-10-30)."""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from cedars.core import pickers
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
    codes = set(shift.funders)
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
    qs = (
        FundShift.objects.filter(status=FundShift.Status.WAITING, shares__has_key=organisation.code)
        .exclude(organisation=organisation)
        .exclude(acknowledgements__organisation=organisation)
        .select_related("organisation", "cycle")
    )
    return [s for s in qs if organisation.code in s.cofunders]


def cycles():
    """The filing cycles a shift can be made in: those with a budget filing."""
    return FilingCycle.objects.filter(deadlines__module=MODULE).distinct().order_by("-first_year")


def current_cycle(among=None, today=None) -> FilingCycle | None:
    """The cycle a new shift opens on: the one holding this year, else the nearest one after it, else the newest (UI review 3: it opened
    on the newest, years ahead)."""
    year = (today or timezone.localdate()).year
    options = list(among if among is not None else cycles())
    holding = [c for c in options if c.first_year <= year <= c.last_year]
    later = sorted((c for c in options if c.first_year > year), key=lambda c: c.first_year)
    return (holding or later or options or [None])[0]


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
    choices = None

    def near(prg) -> str:  # the PA's programs closest to a PrgID it cannot use (core/pickers.py)
        nonlocal choices
        if choices is None:
            choices = program_choices(shift, organisation if not statewide else None)
        found = pickers.near_matches(prg, choices)
        return f" Did you mean {', '.join(c.value + (f' ({c.label})' if c.label else '') for c in found)}?" if found and prg else ""

    for n, ln in rows:
        prg = ln.prg_id.strip()
        p = programs.get(prg)
        if p is None:
            problems.append(f"Line {n}: PrgID {prg or '(blank)'} is not in the program registry.{near(prg)}")
        elif p.statewide and not statewide:
            problems.append(f"Line {n}: {prg} is a statewide program; place {organisation.code}'s portion in its own programs.")
        elif not p.statewide and p.administrator_id != organisation.pk:
            problems.append(f"Line {n}: {prg} is not one of {organisation.code}'s programs.{near(prg)}")
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
class Portion:
    """One funder's portion of one statewide line: the program-year, the whole line's amount, the funder's factor and its portion."""

    item: str
    amount: Decimal
    factor: Decimal
    portion: Decimal


@dataclass
class Part:
    """One PA's part of a shift: its own lines (statewide lines aside) and its portions of the statewide lines, whose sum is the net
    gap it covers from its own programs."""

    pa: str
    moved_in: Decimal = Decimal(0)
    moved_out: Decimal = Decimal(0)
    portion: Decimal = Decimal(0)
    portions: list[Portion] = field(default_factory=list)
    lines: list = field(default_factory=list)

    @property
    def net(self) -> Decimal:
        return self.moved_in + self.moved_out + self.portion


@dataclass
class Split:
    """How the statewide lines split: {funding PA: {'PrgID year': portion}}, the factors used the same way, and the statewide lines
    that have no factors to split them by."""

    shares: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    factors: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def amounts(self) -> dict[str, Decimal]:
        """{'PrgID year': the whole statewide line} for every split line."""
        return {k: sum((items[k] for items in self.shares.values() if k in items), Decimal(0)) for k in self._items()}

    def _items(self):
        return sorted({k for items in self.shares.values() for k in items}, key=_key)


CENT = Decimal("0.01")


def compute_shares(shift: FundShift) -> Split:
    """Each funding PA's portion of the shift's statewide lines, by weighted allocation factor, rounded to cents. The portions of a
    line always add up to the line: the cents rounding leaves over go to the shift's own PA, or to the largest funder when the shift's
    PA does not fund the program."""
    out = Split()
    shares: dict[str, dict[str, Decimal]] = defaultdict(dict)
    factors: dict[str, dict[str, Decimal]] = defaultdict(dict)
    own = [ln for ln in shift.lines.all() if ln.pa == shift.organisation.code]
    sw = statewide_ids(ln.prg_id for ln in own)
    for ln in own:
        if ln.prg_id not in sw:
            continue
        version = allocation_for(shift.organisation, shift.cycle, ln.year)
        rows = (
            list(
                AllocationShare.objects.filter(entry__version=version, entry__year=ln.year, entry__program_id=ln.prg_id)
                .exclude(weighted=0)
                .order_by("-weighted", "funding_pa")
            )
            if version
            else []
        )
        if not rows:
            where = f"in {version.label}" if version else "in any confirmed allocation version"
            out.problems.append(f"{ln.prg_id} {ln.year} has no allocation factors {where}, so its funding PAs' portions are unknown.")
            continue
        item = f"{ln.prg_id} {ln.year}"
        for r in rows:
            shares[r.funding_pa][item] = (ln.amount * r.weighted).quantize(CENT)
            factors[r.funding_pa][item] = r.weighted
        left = ln.amount - sum((shares[r.funding_pa][item] for r in rows), Decimal(0))
        if left:
            mine = shift.organisation.code
            to = mine if item in shares.get(mine, {}) else rows[0].funding_pa
            shares[to][item] += left
    out.shares = {k: dict(v) for k, v in sorted(shares.items())}
    out.factors = {k: dict(v) for k, v in sorted(factors.items())}
    return out


def frozen(shift: FundShift) -> Split:
    """The split recorded at submission, or for a draft the one it would have now."""
    if shift.status == FundShift.Status.DRAFT:
        return compute_shares(shift)
    dec = lambda table: {pa: {k: Decimal(v) for k, v in items.items()} for pa, items in (table or {}).items()}  # noqa: E731
    return Split(dec(shift.shares), dec(shift.factors))


def frozen_shares(shift: FundShift) -> dict[str, dict[str, Decimal]]:
    """The shares recorded at submission, or for a draft the ones it would have now."""
    return frozen(shift).shares


def parts(shift: FundShift) -> list[Part]:
    """Each PA's part: the shift's PA first, then each other funder."""
    split = frozen(shift)
    amounts = split.amounts
    lines = list(shift.lines.all())
    sw = statewide_ids(ln.prg_id for ln in lines)
    out = {shift.organisation.code: Part(shift.organisation.code)}
    for code in sorted(split.shares):
        out.setdefault(code, Part(code))
    for code, items in split.shares.items():
        out[code].portion = sum(items.values(), Decimal(0))
        factors = split.factors.get(code, {})
        out[code].portions = [
            Portion(k, amounts[k], factors.get(k, Decimal(0)), v) for k, v in sorted(items.items(), key=lambda kv: _key(kv[0]))
        ]
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
    """Why the shift cannot be submitted: fewer than two lines, statewide lines with no factors, the shift's PA's part not adding up
    to zero with its portion of the statewide lines, or a line or portion that would leave a program-year's operating
    budget below zero, for the shift's PA or for any other funder."""
    own = [ln for ln in shift.lines.all() if ln.pa == shift.organisation.code]
    out = []
    if len(own) < 2:
        out.append("A fund shift moves money from at least one program-year to another: it needs two lines or more.")
    out += compute_shares(shift).problems
    mine = parts(shift)[0]
    if mine.net != 0:
        out.append(_unbalanced(mine, shift.organisation.code))
    for part in parts(shift):
        org = shift.organisation if part.pa == shift.organisation.code else access.by_code(part.pa)
        if org is not None:
            out += shortfalls(org, shift, _changes(part))
    return out


def _changes(part: Part, lines=None) -> dict[tuple[str, int], Decimal]:
    """{(PrgID, year): the change} a PA's part makes to its operating budget: its own lines and its portions of the statewide lines."""
    out: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    own = [ln for ln in part.lines if ln.pa == part.pa] if lines is None else []
    sw = statewide_ids(ln.prg_id for ln in own)
    for ln in own:
        if ln.prg_id not in sw:  # a statewide line's effect on the PA is its portion
            out[(ln.prg_id, ln.year)] += ln.amount
    for p, y, a in lines or []:
        out[(p, y)] += a
    for p in part.portions:
        out[_key(p.item)] += p.portion
    return dict(out)


def shortfalls(organisation, shift: FundShift, changes: dict[tuple[str, int], Decimal]) -> list[str]:
    """The changes that would leave one of the PA's program-years below zero (Jake, 2026-11-02: no fund shift may). What a
    program-year holds is its operating budget (a statewide program: the PA's portion of it) less what the PA's lines and portions in
    other shifts still waiting for co-funders already take out."""
    have = held(organisation, shift)
    out = []
    for (prg, year), change in sorted(changes.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        holds = have.get((prg, year), Decimal(0))
        if change < 0 and holds + change < 0:
            out.append(
                f"{organisation.code} {prg} {year}: moves {service_money(-change)} out of an operating budget of {service_money(holds)}, "
                f"which would leave it at {service_money(holds + change)}. No fund shift may take a program's operating budget below zero."
            )
    return out


def held(organisation, shift: FundShift) -> dict[tuple[str, int], Decimal]:
    """What each of the PA's program-years holds for this shift: its operating budget (a statewide program: the PA's portion of it)
    less what the PA's lines and portions in other shifts still waiting for co-funders already take out."""
    have = {(r.prg_id, r.year): r.operating for r in operating_budget(organisation, shift.cycle).rows}
    for key, amount in pending_changes(organisation, shift).items():
        have[key] = have.get(key, Decimal(0)) + amount
    return have


def entry_budget(organisation, shift: FundShift) -> dict[str, dict]:
    """For the amounts page, by "PrgID year": what the program-year holds and, for a statewide program, the PA's factor (its line's
    portion counts in the PA's net by it). core's tables read it to show the budget beside a line and the running net as it is typed."""
    out = {f"{prg} {year}": {"held": str(amount), "factor": None} for (prg, year), amount in held(organisation, shift).items()}
    if organisation == shift.organisation:  # the shift's own PA may name statewide programs: their lines count by its factor
        sw = statewide_ids(p for p, _ in programs_for(shift))
        for prg in sorted(sw):
            for year in range(shift.cycle.first_year, shift.cycle.last_year + 1):
                factor = _factor(organisation, shift.cycle, prg, year)
                entry = out.setdefault(f"{prg} {year}", {"held": "0", "factor": None})
                entry["factor"] = str(factor) if factor is not None else None
    return out


def paste_lines(text: str) -> list[LineIn]:
    """Lines pasted from a spreadsheet or typed one to a line: PrgID, year and amount, separated by tabs (as a spreadsheet copies them),
    else by commas or semicolons (an amount's own thousands commas are kept). A heading line (no year) and blank lines are skipped."""
    out = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t") if "\t" in raw else re.split(r"[;,]", raw, maxsplit=2) if ";" in raw or "," in raw else raw.split()
        parts = [x.strip() for x in parts]
        if len(parts) > 3:  # commas inside the amount
            parts = parts[:2] + [",".join(parts[2:])]
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        out.append(LineIn(parts[0], parts[1], parts[2]))
    return out


def pending_changes(organisation, shift: FundShift) -> dict[tuple[str, int], Decimal]:
    """The PA's lines and portions in the cycle's other shifts still waiting for co-funders: committed, though not in effect."""
    out: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    waiting = (
        FundShift.objects.filter(Q(organisation=organisation) | Q(shares__has_key=organisation.code), cycle=shift.cycle)
        .filter(status=FundShift.Status.WAITING)
        .exclude(pk=shift.pk)
        .prefetch_related("lines")
    )
    sw = statewide_ids(ln.prg_id for s in waiting for ln in s.lines.all())
    for s in waiting:
        for ln in s.lines.all():
            if ln.pa == organisation.code and ln.prg_id not in sw:
                out[(ln.prg_id, ln.year)] += ln.amount
        for item, amount in (s.shares or {}).get(organisation.code, {}).items():
            out[_key(item)] += Decimal(amount)
    return dict(out)


def service_money(x: Decimal) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def allocation_for(organisation, cycle: FilingCycle, year: int):
    """The allocation factors a PA's statewide portions are read against: its standing filing's pinned version, else the most recent
    confirmed one for the year (as a run naming none)."""
    v = service.standing_by_year(organisation, cycle).get(year) or service.standing(organisation, cycle)
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
    split = compute_shares(shift)
    shift.shares = {pa: {k: str(v) for k, v in items.items()} for pa, items in split.shares.items()}
    shift.factors = {pa: {k: str(v) for k, v in items.items()} for pa, items in split.factors.items()}
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
        after={**_fields(shift), "shares": shift.shares, "factors": shift.factors},
        actor=actor,
        organisation=shift.organisation,
    )
    if shift.status == FundShift.Status.EFFECTIVE:  # no co-funder: in effect at once (what subscribers hear)
        audit.record("fund_shift.effective", obj=shift, after=_fields(shift), actor=actor, organisation=shift.organisation)
    for code in shift.cofunders:
        org = access.by_code(code)
        items = shift.shares[code]
        if org is not None:
            _notify(
                org,
                f"{MODULE}.confirm",
                f"{shift.organisation.code} fund shift waiting for {code}",
                f"{shift.title}. It changes {code}'s portion of {', '.join(items)} by {service_money(shift.net_of(code))} net. Choose "
                f"where that comes from or goes among {code}'s programs and acknowledge it; it takes effect once every co-funder has.",
                shift,
            )
    for code in shift.informed:
        org = access.by_code(code)
        if org is not None:
            _notify(
                org,
                f"{MODULE}.confirm",
                f"{shift.organisation.code} fund shift: no change to {code}'s total",
                f"{shift.title}. It moves {code}'s portion between {', '.join(shift.shares[code])}, and the portions add up to zero, so "
                f"{code} has nothing to place or acknowledge.",
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
    if not found:
        mine = next(p for p in parts(shift) if p.pa == organisation.code)
        found = shortfalls(organisation, shift, _changes(mine, lines))
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
    statewide_total: Decimal | None = None  # a statewide program: the whole program's approved budget, as filed by its lead PA
    factor: Decimal | None = None  # a statewide program: the PA's weighted allocation factor, by which `approved` is its portion

    @property
    def operating(self) -> Decimal:
        return self.approved + self.shifted

    @property
    def statewide(self) -> bool:
        return self.statewide_total is not None


@dataclass
class OperatingBudget:
    organisation: Organisation
    cycle: FilingCycle
    standing: object  # SubmissionVersion | None
    rows: list[OperatingRow]
    shifts: list[FundShift]
    earlier: list[FundShift] = field(default_factory=list)
    standing_years: list[tuple] = field(default_factory=list)  # (version, its years) when the cycle's years stand on different filings

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


def _line_budget(ln) -> Decimal:
    return sum((Decimal(x) for x in (ln.program_costs or {}).values()), Decimal(0)) + ln.rebates_and_incentives


def statewide_filed(cycle: FilingCycle) -> dict[tuple[str, int], Decimal]:
    """{(statewide PrgID, year): the whole program's approved budget}: the statewide lines of every PA's standing filing in the cycle
    (a statewide program is filed whole, by the PA that leads it)."""
    out: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    orgs = Organisation.objects.filter(submissions__deadline__module=MODULE, submissions__deadline__cycle=cycle).distinct()
    for org in orgs:
        lines = standing_lines(org, cycle)
        sw = statewide_ids(ln.prg_id for ln in lines)
        for ln in lines:
            if ln.prg_id in sw:
                out[(ln.prg_id, ln.year)] += _line_budget(ln)
    return dict(out)


def standing_lines(organisation, cycle: FilingCycle, by_year: dict | None = None) -> list:
    """The PA's approved budget lines in the cycle: each year's lines from the filing that stands for that year
    (`service.standing_by_year`)."""
    by_year = service.standing_by_year(organisation, cycle) if by_year is None else by_year
    years_of: dict[int, list[int]] = defaultdict(list)
    versions = {}
    for y, v in by_year.items():
        years_of[v.pk].append(y)
        versions[v.pk] = v
    return [ln for pk, v in versions.items() for ln in v.budget_lines.filter(year__in=years_of[pk])]


def _factor(organisation, cycle: FilingCycle, prg_id: str, year: int) -> Decimal | None:
    """The PA's weighted allocation factor for a statewide program-year, from the version its portions are read against."""
    version = allocation_for(organisation, cycle, year)
    if version is None:
        return None
    share = AllocationShare.objects.filter(
        entry__version=version, entry__year=year, entry__program_id=prg_id, funding_pa=organisation.code
    ).first()
    return share.weighted if share is not None else Decimal(0)


def operating_budget(organisation, cycle: FilingCycle) -> OperatingBudget:
    """The PA's operating budget by program and year: its standing filing's budget lines (approved), plus its part of the
    shifts in effect since that filing was confirmed: its own lines, and its portion of each statewide line (by allocation factor) on
    the statewide program's row. A statewide program's row is the PA's portion throughout: its weighted factor of the whole program's
    approved budget (filed by the lead PA; the row keeps the whole as `statewide_total`), so a co-funder's row shows what it funds and
    the lead's does not show the other funders' money. A shift in effect before the standing filing was confirmed is taken to be in
    that filing's numbers. Without a standing filing, every shift in effect counts against an approved budget of zero."""
    by_year = service.standing_by_year(organisation, cycle)
    v = service.standing(organisation, cycle)
    rows: dict[tuple[str, int], OperatingRow] = {}
    own_lines = standing_lines(organisation, cycle, by_year)
    sw_filed = statewide_ids(ln.prg_id for ln in own_lines)
    for ln in own_lines:
        if ln.prg_id in sw_filed:
            continue
        rows.setdefault((ln.prg_id, ln.year), OperatingRow(ln.prg_id, ln.year)).approved += _line_budget(ln)
    for (prg, year), whole in statewide_filed(cycle).items():
        factor = _factor(organisation, cycle, prg, year)
        if not factor:
            continue
        r = rows.setdefault((prg, year), OperatingRow(prg, year))
        r.approved, r.statewide_total, r.factor = (whole * factor).quantize(CENT), whole, factor
    effective = list(
        FundShift.objects.filter(Q(organisation=organisation) | Q(shares__has_key=organisation.code), cycle=cycle)
        .filter(status=FundShift.Status.EFFECTIVE)
        .order_by("effective_at")
        .select_related("organisation")
        .prefetch_related("lines")
    )
    # a shift in effect before the filing that stands for a year was confirmed is taken to be in that filing's numbers for the year
    latest_since = v.confirmed_at if v is not None else None
    since = {y: sv.confirmed_at for y, sv in by_year.items()}

    def counts(s, year):
        start = since.get(year, latest_since)
        return start is None or s.effective_at >= start

    def shift_years(s):
        return {ln.year for ln in s.lines.all()} | {_key(i)[1] for i in (s.shares or {}).get(organisation.code, {})}

    applied = [s for s in effective if any(counts(s, y) for y in shift_years(s) or {None})]
    earlier = [s for s in effective if s not in applied]
    sw = statewide_ids(ln.prg_id for s in applied for ln in s.lines.all())
    for s in applied:
        for ln in s.lines.all():
            if ln.pa == organisation.code and ln.prg_id not in sw and counts(s, ln.year):
                rows.setdefault((ln.prg_id, ln.year), OperatingRow(ln.prg_id, ln.year)).shifted += ln.amount
        for item, amount in (s.shares or {}).get(organisation.code, {}).items():
            if not counts(s, _key(item)[1]):
                continue
            r = rows.setdefault(_key(item), OperatingRow(*_key(item)))
            r.shifted += Decimal(amount)
            if r.statewide_total is None:
                r.statewide_total = Decimal(0)
                r.factor = Decimal(s.factors.get(organisation.code, {}).get(item, 0)) or None
    names = {}
    for p in Program.objects.filter(prg_id__in={k[0] for k in rows}).prefetch_related("versions"):
        versions = sorted(p.versions.all(), key=lambda x: x.valid_from, reverse=True)
        names[p.prg_id] = {y: pv.name for y in range(cycle.first_year, cycle.last_year + 1) if (pv := registry._for_year(versions, y))}
    for r in rows.values():
        r.name = names.get(r.prg_id, {}).get(r.year, "")
    stands: dict[int, tuple] = {}
    for y, sv in sorted(by_year.items()):
        stands.setdefault(sv.pk, (sv, []))[1].append(y)
    return OperatingBudget(
        organisation, cycle, v, sorted(rows.values(), key=lambda r: (r.prg_id, r.year)), applied, earlier, list(stands.values())
    )


def operating_rows(organisation, cycle: FilingCycle) -> list[dict]:
    """The operating budget as plain rows, for the registry service's hook (registry.operating_budget): other modules read it there."""
    return [
        {
            "prg_id": r.prg_id,
            "year": r.year,
            "name": r.name,
            "approved": r.approved,
            "shifted": r.shifted,
            "operating": r.operating,
            "statewide_total": r.statewide_total,
            "factor": r.factor,
        }
        for r in operating_budget(organisation, cycle).rows
    ]


def program_choices(shift: FundShift, organisation=None) -> list:
    """`programs_for` as the shared picker's choices (core/pickers.py Choice), statewide programs marked."""
    sw = statewide_ids(p for p, _ in programs_for(shift, organisation))
    return [pickers.Choice(p, name, "statewide" if p in sw else "") for p, name in programs_for(shift, organisation)]


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


def can_read_every_operating(user) -> bool:
    """CPUC staff read every PA's operating budget (the regulator's view: the CPUC has none of its own)."""
    return service._cpuc_reader(user)


NOTICE_LINES = 10  # the shift's lines listed in a notification; the page has every one


def notice(event) -> notifications.Concern | None:
    """What a notification about a fund shift says (registered with the notification service for budgets.fundshift): the PAs whose
    portfolios it moves (its PA and its co-funders), its programs, and its lines."""
    shift = FundShift.objects.select_related("organisation").get(pk=event.object_id)
    lines = list(shift.lines.all())
    orgs = {shift.organisation_id} | {o.pk for o in Organisation.objects.filter(code__in=shift.cofunders)}
    listed = [f"{ln.pa} {ln.prg_id} {ln.year}: {'+' if ln.amount > 0 else ''}{service_money(ln.amount)}" for ln in lines[:NOTICE_LINES]]
    if len(lines) > NOTICE_LINES:
        listed.append(f"and {len(lines) - NOTICE_LINES} more lines.")
    return notifications.Concern(
        orgs,
        MODULE,
        _link(shift),
        {ln.prg_id for ln in lines},
        title=f"Fund shift in effect: {shift.organisation.code}, {shift.title}"[:300],
        body="\n".join([f"Reason: {shift.reason}", *listed]),
    )
