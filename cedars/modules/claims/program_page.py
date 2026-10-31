"""What the program page shows from Claims (BR-07.6), through registry.register_program_records: the program's year to date in each
claims confirmation (Q1 to Q4 and the annual claim) of every claim year. The standing published version of each confirmation is public,
as its figures are on the dashboards; a newer version still being worked on is shown only to those who may read the PA's claims."""

from collections import defaultdict
from decimal import Decimal

from django.urls import reverse

from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from . import service
from .models import ClaimProgramLine, ClaimResult

PUBLISHED = SubmissionVersion.Status.PUBLISHED
RESULTS = {"net_kwh": "FirstYearNetKWh", "net_therm": "FirstYearNetThm", "tsb": "TotalSystemBenefit"}


def confirmations(program, user, year: int | None = None) -> list[dict]:
    """One row per confirmation that has the program, from its standing published version (and, for the PA's readers and CPUC staff,
    from a newer version not yet published): claims, expenditures (program costs plus rebates and incentives), and the priced first-year
    net savings and TSB summed over the credited PAs. Newest year first, its confirmations in order."""
    lines = ClaimProgramLine.objects.filter(prg_id=program.prg_id).select_related(
        "version__submission__deadline__period", "version__submission__organisation"
    )
    if year:
        lines = lines.filter(version__submission__deadline__period__year=year)
    lines = list(lines)
    numbers = submissions.standing_numbers({ln.version.submission_id for ln in lines})
    signed_in = user is not None and user.is_authenticated
    readable: dict[int, bool] = {}
    kept: dict[int, ClaimProgramLine] = {}
    for ln in lines:
        v = ln.version
        standing, latest = numbers.get(v.submission_id, (None, v.number))
        if v.status == PUBLISHED and v.number == standing:
            kept[v.pk] = ln
        elif v.status != PUBLISHED and v.number == latest and signed_in:
            if v.submission_id not in readable:
                readable[v.submission_id] = service.can_read_submission(user, v.submission)
            if readable[v.submission_id]:
                kept[v.pk] = ln
    runs = {pk: (ln.version.calculation or {}).get("run", "") for pk, ln in kept.items()}
    sums: dict[int, dict] = defaultdict(lambda: defaultdict(float))
    for r in ClaimResult.objects.filter(version_id__in=list(kept), prg_id=program.prg_id):
        if r.run == runs.get(r.version_id):
            for key, column in RESULTS.items():
                sums[r.version_id][key] += float(r.values.get(column) or 0)
    out = []
    for pk, ln in kept.items():
        v = ln.version
        d = v.submission.deadline
        priced = pk in sums
        costs = sum((Decimal(str(x)) for x in (ln.program_costs or {}).values()), Decimal(0))
        out.append(
            {
                "title": service.label(d),
                "url": reverse("claims:version", args=[pk]),
                "organisation": v.submission.organisation.code,
                "version": v.number,
                "status": ("Published (imported from CEDARS)" if v.imported else "Published")
                if v.status == PUBLISHED
                else v.get_status_display(),
                "published": v.status == PUBLISHED,
                "standing": v.status == PUBLISHED,
                "when": v.published_at or v.created_at,
                "year": d.period.year,
                "quarter": d.kind,
                "order": service.number(d),
                "records": ln.claims,
                "expenditures": costs + ln.rebates_and_incentives,
                "outside_totals": bool((v.calculation or {}).get("outside_totals")),
                **{key: (sums[pk][key] if priced else None) for key in RESULTS},
            }
        )
    out.sort(key=lambda r: (-r["year"], r["order"], r["organisation"], r["published"]))
    return out
