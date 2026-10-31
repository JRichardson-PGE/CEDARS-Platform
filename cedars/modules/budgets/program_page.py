"""What the program page shows from Budgets (BR-07.6), through registry.register_program_records: the program's lines in every
budget filing version and the fund shifts that move its money. Published filing versions are public, as their figures are on the
dashboards; a version not yet published is shown only to those who may read its filing. Fund shifts are shown only to those who may
read the shift (the PA, its co-funders once submitted, and CPUC staff)."""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Q
from django.urls import reverse

from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from . import service, shifts
from .models import BudgetLine, BudgetResult, FundShift, FundShiftLine

PUBLISHED = SubmissionVersion.Status.PUBLISHED


def _visible(user, v: SubmissionVersion) -> bool:
    return v.status == PUBLISHED or (user is not None and user.is_authenticated and service.can_read_submission(user, v.submission))


def _status(v: SubmissionVersion, standing: int | None) -> str:
    if v.status != PUBLISHED:
        return v.get_status_display()
    if standing is not None and standing != v.number:
        return f"Published, replaced by version {standing}"
    return "Published (imported from CEDARS)" if v.imported else "Published"


def filings(program, user, year: int | None = None) -> list[dict]:
    """One row per budget filing version that has the program: its filed budget by program year (program costs plus the measures'
    rebates and incentives, as the filing's own page counts it), the total, and the program's priced TSB and TRC in that version.
    Newest cycle and filing first, newest version first."""
    lines = BudgetLine.objects.filter(prg_id=program.prg_id).select_related(
        "version__submission__deadline__cycle", "version__submission__organisation"
    )
    if year:
        lines = lines.filter(year=year)
    by_version: dict[int, list[BudgetLine]] = defaultdict(list)
    versions: dict[int, SubmissionVersion] = {}
    for ln in lines:
        by_version[ln.version_id].append(ln)
        versions[ln.version_id] = ln.version
    shown = [v for v in versions.values() if _visible(user, v)]
    numbers = submissions.standing_numbers({v.submission_id for v in shown})
    results: dict[int, dict] = defaultdict(lambda: defaultdict(float))
    runs = {v.pk: (v.calculation or {}).get("run", "") for v in shown}
    q = BudgetResult.objects.filter(version_id__in=list(runs), prg_id=program.prg_id)
    if year:
        q = q.filter(Q(year=year) | Q(year=0))
    for r in q:
        if r.run == runs.get(r.version_id):
            for k in ("TotalSystemBenefit", "TRCBen", "TRCCost"):
                results[r.version_id][k] += float(r.values.get(k) or 0)
    out = []
    for v in shown:
        d = v.submission.deadline
        years: dict[int, Decimal] = {}
        for ln in by_version[v.pk]:
            costs = sum((Decimal(x) for x in (ln.program_costs or {}).values()), Decimal(0))
            years[ln.year] = years.get(ln.year, Decimal(0)) + costs + ln.rebates_and_incentives
        res = results.get(v.pk)
        trc = res["TRCBen"] / res["TRCCost"] if res and res.get("TRCCost") else None
        st = numbers.get(v.submission_id, (None, 0))[0]
        url = reverse("budgets:version", args=[v.pk])
        out.append(
            {
                "title": service.label(d),
                "url": f"{url}?years={year}" if year else url,
                "organisation": v.submission.organisation.code,
                "version": v.number,
                "status": _status(v, st),
                "published": v.status == PUBLISHED,
                "standing": st == v.number,
                "when": v.published_at or v.created_at,
                "cycle": d.cycle,
                "first_year": d.cycle.first_year,
                "due": d.due_at,
                "years": dict(sorted(years.items())),
                "total": sum(years.values(), Decimal(0)),
                "tsb": res["TotalSystemBenefit"] if res else None,
                "trc": trc,
            }
        )
    out.sort(key=lambda r: (-r["first_year"], -r["due"].timestamp(), r["organisation"], -r["version"]))
    return out


def fund_shifts(program, user, year: int | None = None) -> list[dict]:
    """One row per fund shift with lines on the program that the user may read (drafts only to the PA's own readers and CPUC staff):
    those lines and their net. Newest first."""
    if user is None or not user.is_authenticated:
        return []
    lines = FundShiftLine.objects.filter(prg_id=program.prg_id)
    if year:
        lines = lines.filter(year=year)
    ids = set(lines.values_list("shift_id", flat=True))
    if not ids:
        return []
    out = []
    for s in shifts.readable(user).filter(pk__in=ids).prefetch_related("lines"):
        mine = [ln for ln in s.lines.all() if ln.prg_id == program.prg_id and (not year or ln.year == year)]
        out.append(
            {
                "title": s.title,
                "url": reverse("budgets:shift", args=[s.pk]),
                "organisation": s.organisation.code,
                "status": s.get_status_display(),
                "published": False,
                "effective": s.status == FundShift.Status.EFFECTIVE,
                "when": s.effective_at or s.submitted_at or s.created_at,
                "cycle": s.cycle,
                "lines": [{"pa": ln.pa, "year": ln.year, "amount": ln.amount} for ln in sorted(mine, key=lambda x: (x.year, x.pa))],
                "net": sum((ln.amount for ln in mine), Decimal(0)),
            }
        )
    out.sort(key=lambda r: r["when"], reverse=True)
    return out
