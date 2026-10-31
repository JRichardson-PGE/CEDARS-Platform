"""Energy efficiency goals (the portfolio dashboard's goal lines, BR-07.3). The CPUC adopts goals by decision about every two years; each
decision is loaded as a goal set (a CSV of its tables), checked, and confirmed by a second CPUC system administrator. The newest confirmed
set covering a year replaces the earlier ones for that year; earlier sets stay for the record.

Goal file (one row per IOU and period, as the decision's tables print them; a blank or "-" is a value the decision does not set):
  Utility, Kind (cycle | annual), FirstYear, LastYear, ProgramTSB, ProgramGWh, ProgramMW, ProgramMMTherms, CSGWh, CSMW, CSMMTherms
Counting rule file (optional; which PAs' claims count toward an IOU territory's goals):
  PA, Territory, Share, AppliesTo (all | programs | cs), Source"""

import csv
import hashlib
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import Goal, GoalCountingRule, GoalSet

UTILITIES = ("PGE", "SCE", "SDGE", "SCG")
GOAL_COLUMNS = ["Utility", "Kind", "FirstYear", "LastYear"]
METRICS = [
    ("ProgramTSB", "program_tsb"),
    ("ProgramGWh", "program_gwh"),
    ("ProgramMW", "program_mw"),
    ("ProgramMMTherms", "program_mmtherms"),
    ("CSGWh", "cs_gwh"),
    ("CSMW", "cs_mw"),
    ("CSMMTherms", "cs_mmtherms"),
]
RULE_COLUMNS = ["PA", "Territory", "Share", "AppliesTo", "Source"]


@dataclass
class GoalIssue:
    where: str
    message: str
    blocking: bool = True


def _require_admin(actor):
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator manages energy efficiency goals.")


def _number(value, where: str) -> Decimal | None:
    text = str(value or "").replace("$", "").replace(",", "").strip()
    if text in ("", "-", "–", "—"):
        return None
    try:
        return Decimal(text)
    except InvalidOperation as e:
        raise ValueError(f"{where}: {value!r} is not a number") from e


def _rows(content: bytes, needed: list[str], what: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    missing = set(needed) - set(rows[0].keys() if rows else [])
    if missing:
        raise ValueError(f"the {what} is missing columns: {', '.join(sorted(missing))}")
    return rows


@transaction.atomic
def import_goals(
    actor,
    content: bytes,
    *,
    decision: str,
    adopted,
    url: str = "",
    rules: bytes | None = None,
    source_name: str = "",
    notes: str = "",
) -> GoalSet:
    """A draft goal set from the decision's tables (and, optionally, its counting rules)."""
    _require_admin(actor)
    decision = decision.strip()
    if GoalSet.objects.filter(decision=decision).exists():
        raise AccessDenied(f"{decision} is loaded already; discard its draft first to load it again.")
    rows = _rows(content, GOAL_COLUMNS + [c for c, _ in METRICS], "goal file")
    s = GoalSet.objects.create(
        decision=decision,
        adopted=adopted,
        url=url,
        notes=notes,
        created_by=actor,
        source_name=source_name,
        source_sha256=hashlib.sha256(content).hexdigest(),
    )
    for i, r in enumerate(rows, start=2):
        where = f"line {i}"
        try:
            first, last = int(r["FirstYear"]), int(r["LastYear"] or r["FirstYear"])
        except ValueError as e:
            raise ValueError(f"{where}: FirstYear and LastYear must be years") from e
        Goal.objects.create(
            goal_set=s,
            utility=r["Utility"].strip().upper().replace("&", ""),
            kind=r["Kind"].strip().lower(),
            first_year=first,
            last_year=last,
            **{field: _number(r[col], f"{where} {col}") for col, field in METRICS},
        )
    for i, r in enumerate(_rows(rules, ["PA", "Territory", "Share"], "counting rule file") if rules else [], start=2):
        share = _number(r["Share"], f"rule line {i} Share")
        GoalCountingRule.objects.create(
            goal_set=s,
            pa=r["PA"].strip().upper(),
            territory=r["Territory"].strip().upper().replace("&", ""),
            share=share if share is not None else Decimal(0),
            applies_to=(r.get("AppliesTo") or "all").strip().lower() or "all",
            source=(r.get("Source") or "").strip(),
        )
    audit.record(
        "goal_set.imported",
        obj=s,
        after={
            "decision": decision,
            "goals": len(rows),
            "rules": s.counting_rules.count(),
            "source": source_name,
            "sha256": s.source_sha256,
        },
        actor=actor,
    )
    return s


def check(s: GoalSet) -> list[GoalIssue]:
    """Known utilities and kinds, a cycle of four years and a target of one, no negative values, and every row with at least one value."""
    issues = []
    for g in s.goals.all():
        where = f"{g.utility} {g}"
        if g.utility not in UTILITIES:
            issues.append(GoalIssue(where, f"{g.utility} is not an IOU territory ({', '.join(UTILITIES)})"))
        if g.kind not in Goal.Kind.values:
            issues.append(GoalIssue(where, f"kind {g.kind!r} is neither cycle nor annual"))
        elif g.kind == Goal.Kind.ANNUAL and g.first_year != g.last_year:
            issues.append(GoalIssue(where, "an annual target covers one year"))
        elif g.kind == Goal.Kind.CYCLE and g.last_year - g.first_year != 3:
            issues.append(GoalIssue(where, "a cycle goal covers four years", blocking=False))
        values = [getattr(g, f) for _, f in METRICS]
        if all(v is None for v in values):
            issues.append(GoalIssue(where, "no values"))
        if any(v is not None and v < 0 for v in values):
            issues.append(GoalIssue(where, "a negative value"))
    for r in s.counting_rules.all():
        where = f"rule {r.pa} toward {r.territory}"
        if r.territory not in UTILITIES:
            issues.append(GoalIssue(where, f"{r.territory} is not an IOU territory"))
        if not (0 <= r.share <= 1):
            issues.append(GoalIssue(where, f"share {r.share.normalize():f} is outside 0 to 1"))
        if r.applies_to not in GoalCountingRule.AppliesTo.values:
            issues.append(GoalIssue(where, f"applies to {r.applies_to!r}: all, programs or cs"))
    if not s.goals.exists():
        issues.append(GoalIssue("the set", "no goals"))
    return issues


@transaction.atomic
def confirm(actor, s: GoalSet) -> GoalSet:
    """Put a set in force. A second administrator confirms what the first loaded (a system load, with no loader, needs only one)."""
    _require_admin(actor)
    s = GoalSet.objects.select_for_update().get(pk=s.pk)
    if s.status != GoalSet.Status.DRAFT:
        raise AccessDenied("That goal set is already confirmed.")
    if actor is not None and s.created_by_id == actor.pk:
        raise AccessDenied("A different CPUC system administrator confirms the goals you loaded: check them against the decision.")
    blocking = [i for i in check(s) if i.blocking]
    if blocking:
        raise AccessDenied(f"{len(blocking)} problems to fix first, e.g. {blocking[0].where}: {blocking[0].message}.")
    s.status, s.confirmed_by, s.confirmed_at = GoalSet.Status.CONFIRMED, actor, timezone.now()
    s.save()
    audit.record("goal_set.confirmed", obj=s, actor=actor)
    return s


@transaction.atomic
def discard(actor, s: GoalSet, reason: str = "") -> None:
    _require_admin(actor)
    s = GoalSet.objects.select_for_update().get(pk=s.pk)
    if s.status != GoalSet.Status.DRAFT:
        raise AccessDenied("A confirmed goal set is kept for the record.")
    audit.record("goal_set.discarded", obj=s, reason=reason, actor=actor)
    s.delete()


# --- reading


def _confirmed():
    return GoalSet.objects.filter(status=GoalSet.Status.CONFIRMED).order_by("-confirmed_at", "-pk")


def goals(kind: str, year: int) -> dict[str, Goal]:
    """Each IOU's goal in force for a year: the annual target for that year, or the cycle goal whose cycle holds it, from the newest
    confirmed set that has one."""
    out: dict[str, Goal] = {}
    for s in _confirmed():
        for g in s.goals.filter(kind=kind, first_year__lte=year, last_year__gte=year):
            out.setdefault(g.utility, g)
    return out


def counting_rules() -> list[GoalCountingRule]:
    """The counting rules of the newest confirmed set that lists any."""
    for s in _confirmed():
        rules = list(s.counting_rules.all())
        if rules:
            return rules
    return []


def in_force_years(s: GoalSet) -> dict[str, list[int]]:
    """For a confirmed set: the years of each kind for which it is the set in force (none once newer sets cover them all)."""
    if s.status != GoalSet.Status.CONFIRMED:
        return {}
    newer = [n for n in _confirmed() if (n.confirmed_at, n.pk) > (s.confirmed_at, s.pk)]
    out: dict[str, list[int]] = {}
    for kind in Goal.Kind.values:
        years = {y for g in s.goals.filter(kind=kind) for y in range(g.first_year, g.last_year + 1)}
        for n in newer:
            years -= {y for g in n.goals.filter(kind=kind) for y in range(g.first_year, g.last_year + 1)}
        out[kind] = sorted(years)
    return out


def goals_csv(s: GoalSet) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(GOAL_COLUMNS + [c for c, _ in METRICS])
    for g in s.goals.all():
        w.writerow([g.utility, g.kind, g.first_year, g.last_year] + ["" if getattr(g, f) is None else getattr(g, f) for _, f in METRICS])
    return out.getvalue()
