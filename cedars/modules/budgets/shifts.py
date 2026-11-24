"""Fund shifts and the operating budget.

A fund shift moves dollars between a PA's programs within a filing cycle, across its program years if need be. It is not part of any
budget filing and has no cost-effectiveness run. Each PA's part of it must add up to zero: an unbalanced shift is rejected when
submitted and stays a draft.

A statewide program's budget changes only as a whole, on the statewide program's own PrgID (SW_...), and only its lead PA makes the
change (as only the lead files its budget and claims). A PA's own ID of a statewide program (its code in front of the statewide
PrgID) is its portion of that program and is never named on a line, by the shift's PA or by a funder covering its portion
(`registry.statewide_parts`). The change is entered as the change to the whole program; it changes every funding PA's portion of it,
by its weighted allocation factor for that program and year (the standing filing's pinned allocation version, else the most recent
confirmed one; the cents left by rounding go to the shift's own PA). That portion is what the funder covers: a larger portion is paid
for by moving the same amount out of its own programs, a smaller one gives the amount back to them. The lead PA covers its own
portion with its own lines before it submits. Each other funding PA with a net amount to cover (a co-funder) is notified, chooses the
programs it comes from or goes to, and confirms; the shift takes effect when the last one has. Money moved between two statewide
programs with different allocation rates leaves each funder a net amount, which it covers the same way; a funder whose portions cancel
out is informed and has nothing to cover.

The pages follow those steps (`steps`): describe the shift, the statewide program changes, cover the PA's own portion (or, with no
statewide change, the amounts between its own programs), review and submit, the other funders cover theirs, in effect.

No fund shift may leave a program-year's operating budget below zero: a line or a portion that would is rejected, at submission for
every PA's lines and portions and again when a co-funder covers its portion. The lines of shifts still waiting for co-funders count as
spent.

    draft (lines edited) -> submitted -> waiting for co-funders -> in effect
                                       \\-> in effect (no co-funders)
    draft or waiting -> withdrawn

The approved budget (the standing filing) never changes. A PA's operating budget is its approved budget plus its part of the
shifts that took effect after that filing was confirmed: a later filing is taken to carry the earlier shifts in its own numbers, and the
pages say so wherever it matters."""

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
from cedars.services.accounts import permissions as perms
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
        raise AccessDenied(
            f"Making {organisation.code}'s fund shifts needs the {perms.label('budgets.submit')} permission in {organisation.code}."
        )


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


STATEWIDE, OWN = "statewide", "own"  # the two kinds of line, each entered on its own page


def statewide_problem(program: Program, organisation) -> str:
    """Why `organisation` may not change the statewide program's budget in a fund shift, or "" when it may: only the program's lead PA
    does (as only the lead files its budget and claims), and one with no lead recorded is changed by nobody until a lead is."""
    if program.lead_id is None:
        return (
            f"{program.prg_id} has no lead PA recorded, so nobody can change its budget yet; any PA records the lead on the program's page."
        )
    if program.lead_id != organisation.pk:
        lead = program.lead.code
        return (
            f"{program.prg_id} is led by {lead}: only a statewide program's lead PA changes its budget. {lead} makes the shift, "
            f"and {organisation.code} is then asked to cover its portion."
        )
    return ""


def _part_problem(prg: str, sw: Program, organisation) -> str:
    """Why a PA's own ID of a statewide program cannot be on a line, and where the change is made instead."""
    out = f"{prg} is {organisation.code}'s part of the statewide program {sw.prg_id}, whose budget changes only as a whole. "
    if sw.lead_id == organisation.pk:
        return out + f"Enter the change on {sw.prg_id}: every funding PA's portion follows by its allocation factor."
    if sw.lead_id is not None:
        lead = sw.lead.code
        return out + f"Its lead PA, {lead}, changes it with a fund shift on {sw.prg_id}; {organisation.code} then covers its portion."
    return out + f"{sw.prg_id} has no lead PA recorded yet; once it has, its lead changes it with a fund shift on {sw.prg_id}."


def _parse(shift: FundShift, entered: list[LineIn], organisation, *, step: str | None) -> tuple[list[tuple[str, int, Decimal]], list[str]]:
    """The lines to keep and what is wrong with them. `step` says which lines these are: STATEWIDE, the changes to statewide programs
    (each a statewide program the PA leads); OWN, the PA's own programs (never a statewide program, nor the PA's own ID of one);
    None, both together (the shift's PA only). Every PrgID must be in the registry, the year in the shift's cycle, the amount a
    non-zero dollar figure, each program-year once."""
    problems, out, seen = [], [], set()
    rows = [(n, ln) for n, ln in enumerate(entered, start=1) if not ln.blank()]
    programs = {p.prg_id: p for p in Program.objects.filter(prg_id__in={ln.prg_id.strip() for _, ln in rows}).select_related("lead")}
    mine = organisation == shift.organisation
    choices = parts = None

    def near(prg) -> str:  # the PA's programs closest to a PrgID it cannot use (core/pickers.py)
        nonlocal choices
        if choices is None:
            choices = program_choices(shift, organisation, step)
        found = pickers.near_matches(prg, choices)
        return f" Did you mean {', '.join(c.value + (f' ({c.label})' if c.label else '') for c in found)}?" if found and prg else ""

    for n, ln in rows:
        prg = ln.prg_id.strip()
        p = programs.get(prg)
        if p is not None and not p.statewide and parts is None:
            parts = registry.statewide_parts(organisation)
        if p is None:
            problems.append(f"Line {n}: PrgID {prg or '(blank)'} is not in the program registry.{near(prg)}")
        elif p.statewide and step == OWN:
            pa = organisation.code
            where = "enter its change under Statewide program changes" if mine else f"cover {pa}'s portion from its own programs"
            problems.append(f"Line {n}: {prg} is a statewide program; {where}.")
        elif p.statewide:
            why = statewide_problem(p, organisation)
            if why:
                problems.append(f"Line {n}: {why}")
        elif prg in parts:
            problems.append(f"Line {n}: {_part_problem(prg, parts[prg], organisation)}")
        elif p.administrator_id != organisation.pk:
            problems.append(f"Line {n}: {prg} is not one of {organisation.code}'s programs.{near(prg)}")
        elif step == STATEWIDE:
            problems.append(
                f"Line {n}: {prg} is not a statewide program. {organisation.code}'s own programs go in the next step, "
                f"Cover {organisation.code}'s portion."
            )
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


def _replace_lines(shift: FundShift, pa: str, lines, step: str | None = None) -> None:
    """Replace the PA's lines: all of them, or only its statewide lines or only its own programs' (`step`)."""
    old = shift.lines.filter(pa=pa)
    if step is not None:
        sw = statewide_ids(old.values_list("prg_id", flat=True))
        old = old.filter(prg_id__in=sw) if step == STATEWIDE else old.exclude(prg_id__in=sw)
    old.delete()
    FundShiftLine.objects.bulk_create(FundShiftLine(shift=shift, pa=pa, prg_id=p, year=y, amount=a) for p, y, a in lines)


@transaction.atomic
def set_lines(actor, shift: FundShift, entered: list[LineIn], step: str | None = None) -> FundShift:
    """Replace the draft's lines: its statewide program changes (step STATEWIDE), its PA's own programs (OWN), or all of them at once
    (None). Nothing is saved when any line has a problem (ShiftInvalid lists them)."""
    shift = FundShift.objects.select_for_update().get(pk=shift.pk)
    _require_submit(actor, shift.organisation)
    _require_draft(shift)
    lines, problems = _parse(shift, entered, shift.organisation, step=step)
    if problems:
        raise ShiftInvalid(problems)
    before = _fields(shift)
    _replace_lines(shift, shift.organisation.code, lines, step)
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
    own_lines: list = field(default_factory=list)  # its lines in its own programs
    statewide_lines: list = field(default_factory=list)  # the shift's PA: the statewide program changes it entered

    @property
    def placed(self) -> Decimal:
        """What its own programs' lines add up to: minus its portion once the portion is covered."""
        return self.moved_in + self.moved_out

    @property
    def net(self) -> Decimal:
        """What is left to cover: its portion plus its own programs' lines. Zero when its part balances."""
        return self.moved_in + self.moved_out + self.portion

    @property
    def covered(self) -> Decimal:
        """How much of its portion its own programs' lines cover so far, as a positive figure whichever way the money moves."""
        return -self.placed if self.portion >= 0 else self.placed

    @property
    def left(self) -> Decimal:
        """How much of its portion is still to cover (negative: its lines move more than the portion), the same way round."""
        return self.net if self.portion >= 0 else -self.net

    @property
    def words(self) -> str:
        return cover_words(self.pa, self.portion)


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
            part.statewide_lines.append(ln)
            continue
        part.own_lines.append(ln)
        if ln.amount > 0:
            part.moved_in += ln.amount
        else:
            part.moved_out += ln.amount
    return list(out.values())


def cover_words(code: str, portion: Decimal) -> str:
    """What a funding PA does about its portion of a shift's statewide changes, in words."""
    if portion > 0:
        return f"{code} covers {service_money(portion)} from its own programs"
    if portion < 0:
        return f"{code} gets {service_money(-portion)} back, to put into its own programs"
    return f"{code} has nothing to cover"


def balance_problem(pa: str, portion: Decimal, moved_in: Decimal, moved_out: Decimal) -> str:
    """Why a PA's part does not balance, or "": its own programs' lines must add up to minus its portion of the statewide changes
    (to zero with no portion)."""
    placed = moved_in + moved_out
    left = placed + portion
    if left == 0:
        return ""
    if portion == 0:
        return (
            f"{pa}'s lines do not balance: {service_money(moved_in)} into its programs and {service_money(-moved_out)} out, "
            f"{service_money(left)} net. They must add up to zero."
        )
    if portion > 0:
        what = f"{pa} has {service_money(portion)} to cover from its own programs"
        gap = f"{service_money(left)} still to cover" if left > 0 else f"{service_money(-left)} more than its portion"
    else:
        what = f"{pa} gets {service_money(-portion)} back to put into its own programs"
        gap = f"{service_money(-left)} still to place" if left < 0 else f"{service_money(left)} more than it gets back"
    return f"{what}: its lines there must add up to {service_money(-portion)}, and they add up to {service_money(placed)} ({gap})."


def problems(shift: FundShift) -> list[str]:
    """Why the shift cannot be submitted: no statewide change and fewer than two lines, a statewide program its PA does not lead or
    with no factors, a line on a PA's own ID of a statewide program, the shift's PA's own lines not covering its portion of the
    statewide changes exactly (not adding up to zero when there is none), or a line or portion that would leave a program-year's
    operating budget below zero, for the shift's PA or for any other funder."""
    mine, *others = parts(shift)
    out = []
    if not mine.statewide_lines and len(mine.own_lines) < 2:
        out.append("A fund shift moves money from at least one program-year to another: it needs two lines or more.")
    led = Program.objects.filter(prg_id__in={ln.prg_id for ln in mine.statewide_lines}).select_related("lead")
    out += [why for p in sorted(led, key=lambda p: p.prg_id) if (why := statewide_problem(p, shift.organisation))]
    if mine.own_lines:
        own_parts = registry.statewide_parts(shift.organisation)
        out += [_part_problem(ln.prg_id, own_parts[ln.prg_id], shift.organisation) for ln in mine.own_lines if ln.prg_id in own_parts]
    out += compute_shares(shift).problems
    unbalanced = balance_problem(mine.pa, mine.portion, mine.moved_in, mine.moved_out)
    if unbalanced:
        out.append(unbalanced)
    sw_filed = statewide_filed(shift.cycle)
    for part in [mine, *others]:
        org = shift.organisation if part.pa == shift.organisation.code else access.by_code(part.pa)
        if org is not None:
            out += shortfalls(org, shift, _changes(part), sw_filed)
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


def shortfalls(organisation, shift: FundShift, changes: dict[tuple[str, int], Decimal], sw_filed=None) -> list[str]:
    """The changes that would leave one of the PA's program-years below zero (Jake, 2026-11-02: no fund shift may). What a
    program-year holds is its operating budget (a statewide program: the PA's portion of it) less what the PA's lines and portions in
    other shifts still waiting for co-funders already take out."""
    have = held(organisation, shift, sw_filed)
    out = []
    for (prg, year), change in sorted(changes.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        holds = have.get((prg, year), Decimal(0))
        if change < 0 and holds + change < 0:
            out.append(
                f"{organisation.code} {prg} {year}: moves {service_money(-change)} out of an operating budget of {service_money(holds)}, "
                f"which would leave it at {service_money(holds + change)}. No fund shift may take a program's operating budget below zero."
            )
    return out


def held(organisation, shift: FundShift, sw_filed=None) -> dict[tuple[str, int], Decimal]:
    """What each of the PA's program-years holds for this shift: its operating budget (a statewide program: the PA's portion of it)
    less what the PA's lines and portions in other shifts still waiting for co-funders already take out. `sw_filed` is
    `statewide_filed(shift.cycle)` when the caller already has it (it is the same for every PA)."""
    have = {(r.prg_id, r.year): r.operating for r in operating_budget(organisation, shift.cycle, sw_filed).rows}
    for key, amount in pending_changes(organisation, shift).items():
        have[key] = have.get(key, Decimal(0)) + amount
    return have


def entry_budget(organisation, shift: FundShift) -> dict[str, dict]:
    """For the page where a PA enters lines in its own programs, by "PrgID year": what the program-year holds. core's tables read it
    to show the budget beside a line, and what is left of it, as the line is typed."""
    return {f"{prg} {year}": {"held": str(amount)} for (prg, year), amount in held(organisation, shift).items()}


def statewide_entry(shift: FundShift) -> dict[str, dict]:
    """For the statewide program changes page, by "PrgID year", each statewide program the shift's PA leads in each year of the
    cycle that has allocation factors: the whole program's operating budget now (every funder's portion added up) and its funders,
    largest first, each with its factor and what its portion holds. The page shows the budget beside a line and what each funding PA
    would cover as the change is typed, split the way `compute_shares` splits it (`rounding`: who takes the cents left over)."""
    org = shift.organisation
    led = list(Program.objects.filter(statewide=True, lead=org).values_list("prg_id", flat=True))
    if not led:
        return {}
    sw_filed = statewide_filed(shift.cycle)
    holds: dict[str, dict] = {}
    out: dict[str, dict] = {}
    for year in range(shift.cycle.first_year, shift.cycle.last_year + 1):
        version = allocation_for(org, shift.cycle, year)
        if version is None:
            continue
        shares = (
            AllocationShare.objects.filter(entry__version=version, entry__year=year, entry__program_id__in=led)
            .exclude(weighted=0)
            .order_by("entry__program_id", "-weighted", "funding_pa")
            .values_list("entry__program_id", "funding_pa", "weighted")
        )
        for prg, code, weighted in shares:
            if code not in holds:
                funder = org if code == org.code else access.by_code(code)
                holds[code] = held(funder, shift, sw_filed) if funder is not None else {}
            entry = out.setdefault(f"{prg} {year}", {"whole": Decimal(0), "funders": []})
            has = holds[code].get((prg, year), Decimal(0))
            entry["whole"] += has
            entry["funders"].append([code, str(weighted), str(has)])
    for entry in out.values():
        codes = [f[0] for f in entry["funders"]]
        entry["rounding"] = org.code if org.code in codes else codes[0]
        entry["whole"] = str(entry["whole"])
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
                f"{shift.organisation.code} fund shift waiting for {code}: {service_money(abs(shift.net_of(code)))} to cover",
                f"{shift.title}. {shift.organisation.code} changed the budget of {', '.join(items)}, which changes {code}'s portion "
                f"by {service_money(shift.net_of(code))}. {cover_words(code, shift.net_of(code))}: choose the programs and confirm. "
                "The shift takes effect once every funding PA with an amount to cover has.",
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
                f"{code} has nothing to cover or confirm.",
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
    """A co-funder covers its portion of the statewide changes from its own programs (lines adding up to minus its portion: a larger
    portion is paid for by moving money out of its programs) and confirms the shift for its part. With the last one the shift takes
    effect and its PA is notified. Nothing is saved when a line has a problem or the lines do not cover the portion exactly."""
    shift = FundShift.objects.select_for_update().get(pk=shift.pk)
    if not can_acknowledge(actor, shift, organisation):
        raise AccessDenied(f"{organisation.code if organisation else 'You'} cannot cover a portion of this fund shift.")
    lines, found = _parse(shift, list(entered), organisation, step=OWN)
    owed = portion(shift, organisation.code)
    if not found:
        moved_in = sum((a for _, _, a in lines if a > 0), Decimal(0))
        moved_out = sum((a for _, _, a in lines if a < 0), Decimal(0))
        unbalanced = balance_problem(organisation.code, owed, moved_in, moved_out)
        found += [unbalanced] if unbalanced else []
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
            shift.organisation,
            f"{MODULE}.submit",
            "Fund shift in effect",
            f"{shift.title}: every funding PA has covered its portion.",
            shift,
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


def operating_budget(organisation, cycle: FilingCycle, sw_filed=None) -> OperatingBudget:
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
    own_sw = statewide_ids(ln.prg_id for ln in own_lines)
    for ln in own_lines:
        if ln.prg_id in own_sw:
            continue
        rows.setdefault((ln.prg_id, ln.year), OperatingRow(ln.prg_id, ln.year)).approved += _line_budget(ln)
    for (prg, year), whole in (statewide_filed(cycle) if sw_filed is None else sw_filed).items():
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


def program_choices(shift: FundShift, organisation=None, step: str | None = None) -> list:
    """`programs_for` as the shared picker's choices (core/pickers.py Choice), statewide programs marked."""
    found = programs_for(shift, organisation, step)
    sw = statewide_ids(p for p, _ in found)
    return [pickers.Choice(p, name, "statewide" if p in sw else "") for p, name in found]


def programs_for(shift: FundShift, organisation=None, step: str | None = None) -> list[tuple[str, str]]:
    """The programs the PA can name in the shift, for the entry pages' suggestions: on the statewide program changes page (step
    STATEWIDE) the statewide programs the shift's PA leads; among a PA's own programs (OWN) its own, without its own IDs of statewide
    programs; with neither, both (the shift's PA) or its own (a co-funder)."""
    organisation = organisation or shift.organisation
    mine = organisation == shift.organisation
    where = Q(pk__in=[])
    if step != STATEWIDE:
        where |= Q(administrator=organisation, statewide=False)
    if mine and step != OWN:
        where |= Q(statewide=True, lead=organisation)
    skip = set(registry.statewide_parts(organisation)) if step != STATEWIDE else set()
    qs = Program.objects.filter(where, portfolio_cost=False).prefetch_related("versions")
    out = []
    for p in qs:
        if p.prg_id in skip:
            continue
        versions = sorted(p.versions.all(), key=lambda x: x.valid_from, reverse=True)
        pv = registry._for_year(versions, shift.cycle.first_year) or (versions[0] if versions else None)
        out.append((p.prg_id, pv.name if pv else ""))
    return out


def leads_statewide(organisation) -> bool:
    """Whether the PA leads a statewide program: only then can its fund shift change a statewide program's budget."""
    return organisation is not None and Program.objects.filter(statewide=True, lead=organisation).exists()


@dataclass
class Step:
    """One step of a fund shift, for the list of steps at the top of its pages."""

    key: str
    name: str
    done: bool = False
    current: bool = False
    url: str = ""


def steps(shift: FundShift, on: str = "") -> list[Step]:
    """The shift's steps in order, each done or not, one of them current: the page being shown (`on`: "statewide", "own" or
    "cover"), else the first step not done. A shift with no statewide change has no statewide step and nobody else to cover it;
    `on="statewide"` shows that step for a draft that has no statewide line yet. A withdrawn shift has no steps."""
    if shift.status == FundShift.Status.WITHDRAWN:
        return []
    pa = shift.organisation.code
    mine = parts(shift)[0]
    draft = shift.status == FundShift.Status.DRAFT
    statewide = bool(mine.statewide_lines) or on == STATEWIDE
    others = shift.cofunders if not draft else [p.pa for p in parts(shift)[1:] if p.portion != 0]
    out = [Step("describe", "Describe the shift", done=True)]
    if statewide:
        url = reverse("budgets:shift_statewide", args=[shift.pk]) if draft else ""
        out.append(Step(STATEWIDE, "Statewide program changes", done=bool(mine.statewide_lines), url=url))
    balanced = mine.net == 0 and (bool(mine.statewide_lines) or len(mine.own_lines) >= 2)
    name = f"Cover {pa}'s portion" if statewide else f"Amounts in {pa}'s programs"
    out.append(Step(OWN, name, done=balanced, url=reverse("budgets:shift_lines", args=[shift.pk]) if draft else ""))
    out.append(Step("review", "Review and submit", done=not draft, url=_link(shift) if draft else ""))
    if statewide and (others or not mine.statewide_lines):  # before a statewide change is entered, taken to have other funders
        out.append(Step("cover", "Other funders cover their portions", done=shift.status == FundShift.Status.EFFECTIVE))
    out.append(Step("effective", "In effect", done=shift.status == FundShift.Status.EFFECTIVE))
    key = on or next((st.key for st in out if not st.done), "")
    for st in out:
        st.current = st.key == key
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
