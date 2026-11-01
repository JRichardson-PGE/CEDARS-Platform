"""What the program page shows from Monthly reports (BR-07.6), through registry.register_program_records: the program's year-to-date
spend, paid incentives and commitments in each monthly report. The standing published version of each report is public, as the
dashboards show its spending; a newer version still being worked on is shown only to those who may read the PA's reports."""

from decimal import Decimal, InvalidOperation

from django.urls import reverse

from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from . import service
from .models import ReportLine

PUBLISHED = SubmissionVersion.Status.PUBLISHED


def _dollars(values: dict, key: str) -> Decimal | None:
    try:
        return Decimal(str(values[key]))
    except (KeyError, InvalidOperation):
        return None


def reports(program, user, year: int | None = None) -> list[dict]:
    """One row per monthly report that has the program in its spend or its commitments: year-to-date spend (program costs plus rebates
    and incentives), paid incentives (those rebates and incentives, part of spend) and funds committed (incentives to be paid in the
    future, not spend). Newest month first."""
    lines = ReportLine.objects.filter(prg_id=program.prg_id).select_related(
        "version__submission__deadline__period", "version__submission__organisation"
    )
    if year:
        lines = lines.filter(version__submission__deadline__period__year=year)
    lines = list(lines)
    numbers = submissions.standing_numbers({ln.version.submission_id for ln in lines})
    signed_in = user is not None and user.is_authenticated
    readable: dict[int, bool] = {}
    rows: dict[int, dict] = {}
    for ln in lines:
        v = ln.version
        standing, latest = numbers.get(v.submission_id, (None, v.number))
        if v.status == PUBLISHED:
            if v.number != standing:
                continue
        elif v.number != latest or not signed_in:
            continue
        else:
            if v.submission_id not in readable:
                readable[v.submission_id] = service.can_read_submission(user, v.submission)
            if not readable[v.submission_id]:
                continue
        period = v.submission.deadline.period
        row = rows.setdefault(
            v.pk,
            {
                "title": service.label(v.submission.deadline),
                "url": reverse("monthly:version", args=[v.pk]),
                "organisation": v.submission.organisation.code,
                "version": v.number,
                "status": v.get_status_display(),
                "published": v.status == PUBLISHED,
                "standing": v.status == PUBLISHED,
                "when": v.published_at or v.created_at,
                "year": period.year,
                "month": period.number,
                "ytd": None,
                "paid": None,
                "committed": None,
            },
        )
        if ln.table == ReportLine.Table.SPEND:
            row["ytd"] = _dollars(ln.values or {}, "YTDExpenditures")
            row["paid"] = _dollars(ln.values or {}, "RebatesAndIncentives")
        else:
            row["committed"] = _dollars(ln.values or {}, "YTDCommittedFunds")
    out = list(rows.values())
    out.sort(key=lambda r: (-r["year"], -r["month"], r["organisation"], r["published"]))
    return out
