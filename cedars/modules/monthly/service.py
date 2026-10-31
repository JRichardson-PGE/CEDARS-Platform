"""Monthly reports (BR-14). A CPUC system administrator sets up a program year: twelve deadlines of the "monthly" module, one per month
(the submissions service's calendar), and what the year's BCA inputs are priced on (YearSetup). Each PA's report for a month is a
submission with versions.

    upload (.zip or .xlsx: Commitments, Measure, ProgramCost) -> spend, commitments and checks -> validated
    -> priced (Calculations, through the submissions service: estimated savings and TSB) -> confirm -> published

A report is year to date: its BCA inputs are the year's measures and program costs so far, so its estimates show how programs are
trending toward their goals (BR-14.5). The estimates are labelled unvalidated everywhere and never enter a claim. The BCA inputs get
BCA-level validation only, the engine's own input check when they are priced, not the claims rules (D2). Before the due date a PA can
un-confirm, change and re-confirm (BR-14.3); after it a correction is a new version. Each program's spend is read from the BCA inputs
(ProgramCost and the measures' rebates and incentives, as Budgets counts a budget; Jake, 2026-10-30), the month's spend being the change
in the year to date since the previous month's report, and compared with its operating budget, the approved budget plus fund shifts in
effect, read from Budgets through the registry service (BR-04.7, BR-14.6). Commitments are their own table, with CEDARS' committed columns.
"""

import json
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry import service as registry
from cedars.services.registry.models import AllocationVersion, Program, ReportingPeriod
from cedars.services.rules import service as rules
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, Submission, SubmissionVersion

from . import upload as up
from .models import COMMITMENT_FIELDS, INCENTIVE_FIELDS, PROGRAM_COST_FIELDS, SPEND_FIELDS, MonthlyResult, ReportLine, YearSetup

MODULE = "monthly"
KIND = "Monthly report"
RESULT_KEYS = {"_pa", "_prg", "_prgname", "_sector", "_segment", "_year", "_n"}
YTD_TOLERANCE = Decimal("1")  # dollars: year-to-date spend may move by rounding
BCA_NOTE = (
    "The BCA inputs (Measure and ProgramCost) get BCA-level validation only: the calculation engine's own input check, run when the "
    "report is priced, not the claims rules (D2). Their savings and TSB are unvalidated estimates."
)


# --- program years (the CPUC sets them up)


def due_at(day: date) -> datetime:
    """A due date as the end of that day."""
    return timezone.make_aware(datetime.combine(day, time(23, 59, 59)))


def _month_after(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def due_date(year: int, month: int, due_day: int) -> date:
    """The day a month's report is due: due_day of the following month (its last day when the month is shorter)."""
    y, m = _month_after(year, month)
    last = (date(*_month_after(y, m), 1) - timedelta(days=1)).day
    return date(y, m, min(due_day, last))


def _setup_fields(s: YearSetup) -> dict:
    return {"acc": s.acc_version, "allocation_version": str(s.allocation_version or "most recent"), "due_day": s.due_day}


def _check_allocation(version: AllocationVersion | None) -> None:
    if version is not None and version.status != AllocationVersion.Status.CONFIRMED:
        raise AccessDenied(f"{version} is a draft; reports are priced only with a confirmed allocation version.")


@transaction.atomic
def set_up_year(actor, *, year: int, acc_version: str, due_day: int, allocation_version=None, notes: str = "") -> YearSetup:
    """A program year's monthly reports: the year's set-up and one deadline per month, each opening when its month ends and due on
    due_day of the next month."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator sets up monthly reports.")
    if not 1 <= due_day <= 31:
        raise AccessDenied("The due day is a day of the month, 1 to 31.")
    if YearSetup.objects.filter(year=year).exists():
        raise AccessDenied(f"{year}'s monthly reports are already set up.")
    _check_allocation(allocation_version)
    registry.ensure_periods(year)
    setup = YearSetup.objects.create(
        year=year, acc_version=acc_version, allocation_version=allocation_version, due_day=due_day, notes=notes
    )
    for p in ReportingPeriod.objects.filter(kind=ReportingPeriod.Kind.MONTH, year=year).order_by("number"):
        opens = timezone.make_aware(datetime.combine(date(*_month_after(year, p.number), 1), time()))
        submissions.add_deadline(
            actor, module=MODULE, kind=KIND, period=p, due_at=due_at(due_date(year, p.number, due_day)), opens_at=opens
        )
    audit.record("monthly_reports.set_up", obj=setup, after=_setup_fields(setup), actor=actor)
    return setup


@transaction.atomic
def change_setup(actor, setup: YearSetup, *, acc_version: str, allocation_version=None, reason: str) -> YearSetup:
    """Change what the year's reports are priced on. Versions already priced keep what they were priced with. A month's due date is
    moved on the filing calendar."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator changes how monthly reports are priced.")
    if not reason.strip():
        raise AccessDenied("Give the reason for the change.")
    _check_allocation(allocation_version)
    before = _setup_fields(setup)
    setup.acc_version, setup.allocation_version = acc_version, allocation_version
    setup.save()
    audit.record("monthly_reports.setup_changed", obj=setup, before=before, after=_setup_fields(setup), reason=reason, actor=actor)
    return setup


def months(year: int):
    """The year's twelve report deadlines, January first."""
    return (
        Deadline.objects.filter(module=MODULE, period__kind=ReportingPeriod.Kind.MONTH, period__year=year)
        .select_related("period")
        .order_by("period__number")
    )


def setup_for(deadline: Deadline) -> YearSetup | None:
    return YearSetup.objects.select_related("allocation_version").filter(year=deadline.period.year).first()


def label(deadline: Deadline) -> str:
    return f"{deadline.period.starts:%B %Y} report"


# --- who sees what


def _org_reader(user, organisation) -> bool:
    return any(access.has_permission(user, organisation, f"{MODULE}.{a}") for a in ("read", "submit", "confirm"))


def _cpuc_reader(user) -> bool:
    org = access.cpuc()
    return access.is_system_admin(user) or (
        org is not None and any(access.has_permission(user, org, f"{MODULE}.{a}") for a in ("read", "approve"))
    )


def can_read_submission(user, submission: Submission) -> bool:
    return _org_reader(user, submission.organisation) or _cpuc_reader(user)


def can_read(user, v: SubmissionVersion) -> bool:
    """A published report is open to every signed-in user, as CEDARS' monthly reports are; the rest only to the organisation and CPUC
    staff."""
    if not user.is_authenticated:
        return False
    return v.status == SubmissionVersion.Status.PUBLISHED or can_read_submission(user, v.submission)


def standing(organisation, deadline: Deadline) -> SubmissionVersion | None:
    """The report that stands for a PA's month: the published version, the latest when it has been corrected."""
    return (
        SubmissionVersion.objects.filter(
            submission__organisation=organisation, submission__deadline=deadline, status=SubmissionVersion.Status.PUBLISHED
        )
        .order_by("-number")
        .first()
    )


def previous_month(deadline: Deadline) -> Deadline | None:
    """The report deadline for the month before, in the same program year (none for January: a year's figures start again)."""
    p = deadline.period
    if p.number == 1:
        return None
    return Deadline.objects.filter(module=MODULE, period__kind=p.kind, period__year=p.year, period__number=p.number - 1).first()


# --- the upload and its checks


def _prefix(v: SubmissionVersion) -> str:
    return f"monthly/{v.submission_id}/v{v.number}/"


def _finding(row, record, table, fieldname, rule, severity, message, fix, value="") -> rules.Finding:
    return rules.Finding(row, record, table, fieldname, rule, severity, message, fix, value)


def derive(tables: dict[str, bytes], organisation, period: ReportingPeriod, previous: SubmissionVersion | None):
    """The version's lines from its tables, the module's checks, the number of records and notes. Each program's spend is read from the
    year-to-date BCA inputs (Jake, 2026-10-30): its ProgramCost row's ten cost fields and OnBillFinancing, plus its measures' rebates and
    incentives (NumUnits x the four unit fields), as Budgets counts a budget; YTDExpenditures is the costs plus the rebates and
    incentives. Commitments are their own table.

    Checks (rule IDs MON-*): Commitments' PA is the filing organisation and its YearMonth the report's month; PrgIDs in the program
    registry and the PA's own or statewide; the spend and commitment fields are numbers (CEDARS has the commitments NotEmpty); one row per
    program in Commitments and in ProgramCost; the BCA inputs are this year's up to the report's quarter (ProgramCost PrgYear, Measure
    ClaimYearQuarter). Warnings: a program's year-to-date spend below the previous month's standing report's; measures whose program has
    no ProgramCost row (priced with no program costs)."""
    year, month = period.year, period.number
    quarter = (month - 1) // 3 + 1
    found: list[rules.Finding] = []
    notes: list[str] = []
    parsed = {t: up.rows(tables[t]) for t in up.TABLES}
    prg_ids = {up.get(r, "PrgID") for _, rs in parsed.values() for r in rs}
    programs = {p.prg_id: p for p in Program.objects.filter(prg_id__in=prg_ids)}

    def program_ok(table, i, rid, prg) -> bool:
        p = programs.get(prg)
        if p is None:
            found.append(
                _finding(
                    i,
                    rid,
                    table,
                    "PrgID",
                    "MON-PRG",
                    "reject",
                    f"PrgID {prg or '(blank)'} is not in the program registry",
                    "Use the PrgID of a program in the Programs catalog, or ask for the program to be added first.",
                    prg,
                )
            )
            return False
        if not p.statewide and p.administrator_id not in (None, organisation.pk):
            found.append(
                _finding(
                    i,
                    rid,
                    table,
                    "PrgID",
                    "MON-ORG",
                    "reject",
                    f"{prg} is another program administrator's program",
                    f"Report only {organisation.code}'s programs and the statewide programs it leads.",
                    prg,
                )
            )
            return False
        return True

    def numbers(table, i, rid, row, fields, blank_ok) -> dict | None:
        values, ok = {}, True
        for f in fields:
            n = up.number(up.get(row, f), blank_ok=blank_ok)
            if n is None:
                found.append(
                    _finding(
                        i,
                        rid,
                        table,
                        f,
                        "MON-NUM",
                        "reject",
                        f"{f} should be a number",
                        f"Enter a plain number in {f}" + (" (0 when there is nothing to report)." if not blank_ok else "."),
                        up.get(row, f),
                    )
                )
                ok = False
            else:
                values[f] = n
        return values if ok else None

    lines: list[dict] = []
    header, rows = parsed["Commitments"]
    retired = [c for c in header if c in up.RETIRED]
    if retired:
        notes.append(f"Commitments: {', '.join(retired)} {'is' if len(retired) == 1 else 'are'} not read ({up.RETIRED[retired[0]]}).")
    seen = set()
    for i, r in enumerate(rows, start=2):
        prg = up.get(r, "PrgID")
        ok = program_ok("Commitments", i, prg, prg)
        pa = up.get(r, "PA")
        if pa.upper() != organisation.code.upper():
            found.append(
                _finding(
                    i,
                    prg,
                    "Commitments",
                    "PA",
                    "MON-PA",
                    "reject",
                    f"PA {pa or '(blank)'} is not {organisation.code}",
                    f"Put {organisation.code} in PA: a report holds only the filing PA's rows.",
                    pa,
                )
            )
            ok = False
        ym = up.year_month(up.get(r, "YearMonth"))
        if ym != (year, month):
            raw = up.get(r, "YearMonth")
            found.append(
                _finding(
                    i,
                    prg,
                    "Commitments",
                    "YearMonth",
                    "MON-MONTH",
                    "reject",
                    f"YearMonth {raw or '(blank)'} is not this report's month",
                    f"Use {year}{month:02d}.",
                    raw,
                )
            )
            ok = False
        values = numbers("Commitments", i, prg, r, [f for f, _ in COMMITMENT_FIELDS], blank_ok=False)
        ok = ok and values is not None
        if ok and prg in seen:
            found.append(
                _finding(
                    i, prg, "Commitments", "PrgID", "MON-DUP", "reject", f"{prg} has two Commitments rows", "Keep one row per program.", prg
                )
            )
            ok = False
        if ok:
            seen.add(prg)
            lines.append(
                {
                    "table": ReportLine.Table.COMMITMENTS,
                    "prg_id": prg,
                    "pa": pa,
                    "values": {k: str(n) for k, n in values.items()},
                    "comments": up.get(r, "Comments")[:255],
                }
            )

    spend: dict[str, dict] = {}

    def spend_line(prg: str, pa: str = "") -> dict:
        s = spend.setdefault(prg, {"pa": pa, "values": defaultdict(Decimal), "measures": 0})
        s["pa"] = s["pa"] or pa
        return s

    _, costs = parsed["ProgramCost"]
    cost_programs = set()
    for i, r in enumerate(costs, start=2):
        prg, raw = up.get(r, "PrgID"), up.get(r, "PrgYear")
        ok = program_ok("ProgramCost", i, prg, prg)
        if raw.strip() != str(year):
            found.append(
                _finding(
                    i,
                    prg,
                    "ProgramCost",
                    "PrgYear",
                    "MON-YEAR",
                    "reject",
                    f"PrgYear {raw or '(blank)'} is not {year}",
                    f"A {year} report's program costs are {year}'s, year to date.",
                    raw,
                )
            )
            ok = False
        values = numbers("ProgramCost", i, prg, r, [f for f, _ in PROGRAM_COST_FIELDS] + ["OnBillFinancing"], blank_ok=True)
        ok = ok and values is not None
        if ok and prg in cost_programs:
            found.append(
                _finding(
                    i,
                    prg,
                    "ProgramCost",
                    "PrgID",
                    "MON-DUP",
                    "reject",
                    f"{prg} has two ProgramCost rows",
                    "Keep one row per program: its costs for the year to date.",
                    prg,
                )
            )
            ok = False
        if ok:
            cost_programs.add(prg)
            s = spend_line(prg, up.get(r, "PA"))
            for k, n in values.items():
                s["values"][k] += n
    _, measures = parsed["Measure"]
    without_costs: dict[str, int] = defaultdict(int)
    for i, r in enumerate(measures, start=2):
        rid, prg, raw = up.get(r, "CEInputID"), up.get(r, "PrgID"), up.get(r, "ClaimYearQuarter")
        ok = program_ok("Measure", i, rid, prg)
        yq = up.year_quarter(raw)
        if yq is None or yq[0] != year or yq[1] > quarter:
            found.append(
                _finding(
                    i,
                    rid,
                    "Measure",
                    "ClaimYearQuarter",
                    "MON-YEAR",
                    "reject",
                    f"ClaimYearQuarter {raw or '(blank)'} is not in {year} up to this report's quarter",
                    f"Use {year}Q1 to {year}Q{quarter}: the report's BCA inputs are the year to date.",
                    raw,
                )
            )
            ok = False
        values = numbers("Measure", i, rid, r, ["NumUnits"] + INCENTIVE_FIELDS, blank_ok=True)
        if not (ok and values is not None):
            continue
        if prg not in cost_programs:
            without_costs[prg] += 1
        s = spend_line(prg)
        s["values"]["RebatesAndIncentives"] += values["NumUnits"] * sum(values[f] for f in INCENTIVE_FIELDS)
        s["measures"] += 1
    for prg, n in sorted(without_costs.items()):
        found.append(
            _finding(
                0,
                prg,
                "Measure",
                "PrgID",
                "MON-NOCOST",
                "warning",
                f"{n} measure{'s' if n > 1 else ''} of {prg} {'have' if n > 1 else 'has'} no ProgramCost row: priced with no program costs",
                f"Add {prg}'s {year} ProgramCost row if it has program costs.",
                prg,
            )
        )
    for prg, s in sorted(spend.items()):
        v = s["values"]
        v["YTDExpenditures"] = sum((v[f] for f, _ in PROGRAM_COST_FIELDS), Decimal(0)) + v["RebatesAndIncentives"]
        values = {f: str(v[f]) for f in SPEND_FIELDS}
        lines.append({"table": ReportLine.Table.SPEND, "prg_id": prg, "pa": s["pa"] or organisation.code, "values": values, "comments": ""})
    found += _ytd_checks(lines, previous, month)
    if month > 1 and previous is None:
        notes.append("The previous month's report is not confirmed, so the year-to-date spend was not checked against it.")
    notes.append(BCA_NOTE)
    records = sum(len(rs) for _, rs in parsed.values())
    return lines, found, records, notes


def _previous_spend(previous: SubmissionVersion | None) -> dict[str, Decimal]:
    """{PrgID: YTDExpenditures} of the previous month's standing report."""
    if previous is None:
        return {}
    ensure_lines(previous)
    return {ln.prg_id: Decimal(ln.values.get("YTDExpenditures", "0")) for ln in previous.monthly_lines.filter(table=ReportLine.Table.SPEND)}


def _ytd_checks(lines: list[dict], previous: SubmissionVersion | None, month: int) -> list[rules.Finding]:
    """MON-YTD: the BCA inputs are the year to date, so a program's spend should not fall below what the previous month's standing report
    had (a program it had with spend should still be there)."""
    if month == 1 or previous is None:
        return []
    before = _previous_spend(previous)
    now = {ln["prg_id"]: Decimal(ln["values"]["YTDExpenditures"]) for ln in lines if ln["table"] == ReportLine.Table.SPEND}
    out = []
    for prg in sorted(set(before) | set(now)):
        prior, got = before.get(prg, Decimal(0)), now.get(prg)
        if got is None and prior > YTD_TOLERANCE:
            out.append(
                _finding(
                    0,
                    prg,
                    "ProgramCost",
                    "PrgID",
                    "MON-YTD",
                    "warning",
                    f"{prg} had {prior:,.2f} spent year to date last month and is not in this report's BCA inputs",
                    f"Keep {prg}'s ProgramCost row and measures: the BCA inputs are the year to date.",
                    prg,
                )
            )
        elif got is not None and prior - got > YTD_TOLERANCE:
            out.append(
                _finding(
                    0,
                    prg,
                    "ProgramCost",
                    "PrgID",
                    "MON-YTD",
                    "warning",
                    f"{prg}'s spend year to date, {got:,.2f}, is below last month's {prior:,.2f}",
                    f"Check {prg}'s ProgramCost row and measures: the BCA inputs are the year to date, so spend should not fall.",
                    str(got),
                )
            )
    return out


def _summary(found: list, records: int, notes: list[str], key: str) -> dict:
    rejected = {(p.table, p.row, p.record_id) for p in found if p.severity == "reject"}
    warned = {(p.table, p.row, p.record_id) for p in found if p.severity != "reject"} - rejected
    by_rule = defaultdict(lambda: [0, "", ""])
    for p in found:
        by_rule[p.rule_id][0] += 1
        by_rule[p.rule_id][1:] = [p.severity, p.message]
    top = sorted(by_rule.items(), key=lambda kv: (kv[1][1] != "reject", -kv[1][0]))
    return {
        "records": records,
        "rejected": len(rejected),
        "warnings": len(warned),
        "notes": notes,
        "rule_set": "Monthly report checks (MON-*)",
        "findings_key": key,
        "findings": len(found),
        "by_rule": [{"rule_id": k, "count": c, "severity": s, "example": m} for k, (c, s, m) in top[:20]],
    }


def report_file(v: SubmissionVersion) -> dict | None:
    return next((f for f in v.files or [] if f.get("key", "").endswith("report.zip")), None)


def job_file(v: SubmissionVersion) -> dict | None:
    return next((f for f in v.files or [] if f.get("key", "").endswith("job.zip")), None)


@transaction.atomic
def upload(actor, deadline: Deadline, organisation, data: bytes, filename: str) -> SubmissionVersion:
    """Take a PA's report for a month: start the submission (or use its open draft), keep the upload (an earlier upload of the version is
    deleted: superseded drafts are not kept), read its lines, run the module's checks and record the result: validated when nothing is
    rejected."""
    if deadline.module != MODULE or setup_for(deadline) is None:
        raise AccessDenied("This is not a monthly report that has been set up.")
    tables = up.read_upload(data, filename)
    v = submissions.start(actor, deadline, organisation)
    prev = previous_month(deadline)
    lines, found, records, notes = derive(tables, organisation, deadline.period, standing(organisation, prev) if prev else None)
    report, job = up.report_zip(tables), up.job_zip(tables)
    sha = up.sha256(report)
    store = storage.store("uploads")
    folder = f"{_prefix(v)}{sha[:12]}/"
    findings_key = folder + "findings.json"
    store.put_bytes(folder + "report.zip", report)
    store.put_bytes(folder + "job.zip", job)
    store.put_bytes(findings_key, json.dumps([f.__dict__ for f in found]).encode())
    # the superseded upload, once this one is recorded; a reopened version starts with its predecessor's files, which stay with it
    old = {f["key"].rsplit("/", 1)[0] + "/" for f in v.files or [] if f.get("key", "").startswith(_prefix(v))} - {folder}
    transaction.on_commit(lambda: [store.delete_prefix(o) for o in old])
    files = [
        {"name": "report.zip", "key": folder + "report.zip", "sha256": sha, "bytes": len(report), "uploaded_as": filename},
        {
            "name": "job.zip",
            "key": folder + "job.zip",
            "sha256": up.sha256(job),
            "bytes": len(job),
            "measures": len(up.rows(tables["Measure"])[1]),
        },
    ]
    v = submissions.attach_files(actor, v, files)
    _replace_lines(v, lines)
    return submissions.record_validation(actor, v, None, _summary(found, records, notes, findings_key))


def _replace_lines(v: SubmissionVersion, lines: list[dict]) -> None:
    ReportLine.objects.filter(version=v).delete()
    ReportLine.objects.bulk_create(ReportLine(version=v, **ln) for ln in lines)
    MonthlyResult.objects.filter(version=v).delete()


def ensure_lines(v: SubmissionVersion) -> None:
    """A reopened version (n+1) starts with its predecessor's files: read its lines from them."""
    f = report_file(v)
    if f is None or v.monthly_lines.exists():
        return
    tables = up.read_upload(storage.store("uploads").get_bytes(f["key"]), "report.zip")
    lines, _, _, _ = derive(tables, v.submission.organisation, v.submission.deadline.period, None)
    _replace_lines(v, lines)


def findings(v: SubmissionVersion) -> list[dict]:
    key = (v.findings or {}).get("findings_key")
    if not key or not storage.store("uploads").exists(key):
        return []
    return json.loads(storage.store("uploads").get_bytes(key))


def findings_csv(v: SubmissionVersion) -> str:
    return rules.findings_csv([rules.Finding(**f) for f in findings(v)])


# --- pricing (the estimates) and confirming


def price(actor, v: SubmissionVersion) -> str:
    """Price a validated report's year-to-date BCA inputs on the year's ACC for its estimates (BR-14.5); a report with statewide programs
    is allocated with the year's allocation version, or the most recent confirmed one. Returns the run's id. A report whose Measure
    table has no rows has nothing to estimate: it is recorded as priced with no run ("none"), so it can be confirmed."""
    v = SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation").get(pk=v.pk)
    f = job_file(v)
    if f is None:
        raise AccessDenied("Upload the report first.")
    setup = setup_for(v.submission.deadline)
    if not f.get("measures"):
        if not access.has_permission(actor, v.submission.organisation, f"{MODULE}.submit"):
            raise AccessDenied(f"You need {MODULE}.submit in {v.submission.organisation.code} to do that.")
        if v.status != SubmissionVersion.Status.VALIDATED:
            raise AccessDenied("Only a validated version is priced for confirmation.")
        submissions.record_calculation(
            actor,
            v,
            run="none",
            input_hash=f["sha256"],
            details={"job_sha256": f["sha256"], "acc": setup.acc_version, "nothing_priced": True},
        )
        return "none"
    store = storage.store("uploads")
    ensure_lines(v)  # every program of the BCA inputs has a spend line
    statewide = Program.objects.filter(prg_id__in=[ln.prg_id for ln in v.monthly_lines.all()], statewide=True).exists()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "job.zip"
        path.write_bytes(store.get_bytes(f["key"]))
        name = f"{v.submission.organisation.code} {label(v.submission.deadline)}, version {v.number} (estimate)"
        return submissions.price(
            actor,
            v,
            path,
            name=name,
            acc_version=setup.acc_version,
            allocation_version=setup.allocation_version,
            settings={"allocate_statewide": statewide},
        )


def priced(v: SubmissionVersion) -> bool:
    """A finished run (or nothing to price) on exactly the version's current BCA inputs."""
    f = job_file(v)
    return bool(v.calculation and f and v.calculation.get("job_sha256") == f["sha256"])


def import_results(v: SubmissionVersion) -> bool:
    """Read the version's estimates into MonthlyResult rows (once per run): the run's group sums, summed by credited PA and program.
    False when the version has none."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    calc = v.calculation or {}
    key = (calc.get("results") or {}).get("group_sums")
    if not key:
        return False
    if MonthlyResult.objects.filter(version=v, run=calc["run"]).exists():
        return True
    table = pq.read_table(pa.BufferReader(storage.store("results").get_bytes(key)))
    sums: dict[tuple, dict] = {}
    for row in table.to_pylist():
        k = (str(row.get("_pa") or ""), str(row.get("_prg") or ""))
        s = sums.setdefault(k, {"name": "", "n": 0, "values": defaultdict(float)})
        s["name"] = s["name"] or str(row.get("_prgname") or "")
        s["n"] += int(row.get("_n") or 0)
        for c, val in row.items():
            if c not in RESULT_KEYS and isinstance(val, int | float) and val == val:  # NaN != NaN: a blank sum
                s["values"][c] += float(val)
    with transaction.atomic():
        MonthlyResult.objects.filter(version=v).delete()
        MonthlyResult.objects.bulk_create(
            MonthlyResult(
                version=v, run=calc["run"], pa=k[0], prg_id=k[1], program_name=s["name"], measures=s["n"], values=dict(s["values"])
            )
            for k, s in sums.items()
        )
    return True


def confirm(actor, v: SubmissionVersion) -> SubmissionVersion:
    """Confirm the priced report: the sign-off on exactly the figures and estimates reviewed; publication follows at once."""
    if not priced(v):
        raise AccessDenied("Price this version first: confirming needs its estimates on exactly this upload.")
    return submissions.confirm(actor, v, v.calculation["input_hash"])


def on_publish(v: SubmissionVersion) -> None:
    """Publication hook: the estimates the version was confirmed on are read now, so the published version holds them."""
    import_results(v)


def on_unpublish(v: SubmissionVersion) -> None:
    """Un-confirmed before the due date: nothing is kept apart from the version, which goes back to validated."""


def reported_spend(organisation, year: int) -> list[dict]:
    """The PA's latest published report of the year: each program's year-to-date expenditure (registry.reported_spend)."""
    versions = SubmissionVersion.objects.filter(
        submission__organisation=organisation,
        submission__deadline__module="monthly",
        submission__deadline__period__year=year,
        status=SubmissionVersion.Status.PUBLISHED,
    ).select_related("submission__deadline__period")
    standing = [v for v in versions if (c := submissions.current(v.submission)) is not None and c.pk == v.pk]
    if not standing:
        return []
    latest = max(standing, key=lambda v: v.submission.deadline.period.ends)
    period = latest.submission.deadline.period
    through = f"{period.year}-{period.number:02d}"
    return [
        {"prg_id": ln.prg_id, "ytd": Decimal(ln.values.get("YTDExpenditures", "0")), "through": through}
        for ln in latest.monthly_lines.filter(table=ReportLine.Table.SPEND)
    ]


# --- a report by program, against the operating budget


ESTIMATES = [
    ("TotalSystemBenefit", "Estimated TSB", "money"),
    ("FirstYearNetKWh", "Estimated first-year net kWh", "number"),
    ("FirstYearNetKW", "Estimated first-year net kW", "number"),
    ("FirstYearNetThm", "Estimated first-year net therms", "number"),
    ("TRC", "Estimated TRC", "ratio"),
    ("PAC", "Estimated PAC", "ratio"),
]
RATIOS = {"TRC": ("TRCBen", "TRCCost"), "PAC": ("PACBen", "PACCost")}


@dataclass
class Row:
    prg_id: str
    name: str = ""
    approved: Decimal | None = None
    operating: Decimal | None = None
    reported: bool = False  # in the BCA inputs: has spend
    values: dict = field(default_factory=lambda: defaultdict(Decimal))
    committed: bool = False
    measures: int = 0
    sums: dict = field(default_factory=lambda: defaultdict(float))
    estimated: bool = False

    def get(self, key: str) -> Decimal | None:
        return self.values.get(key) if (self.reported or self.committed) and key in self.values else None

    @property
    def spent_share(self) -> float | None:
        """Year-to-date spend as a share of the operating budget."""
        ytd = self.get("YTDExpenditures")
        return float(ytd / self.operating) if ytd is not None and self.operating else None

    def estimate(self, key: str):
        if not self.estimated:
            return None
        if key in RATIOS:
            ben, cost = (self.sums.get(c) for c in RATIOS[key])
            return ben / cost if ben is not None and cost else None
        return self.sums.get(key)

    def add(self, other: "Row") -> None:
        for attr in ("approved", "operating"):
            if getattr(other, attr) is not None:
                setattr(self, attr, (getattr(self, attr) or Decimal(0)) + getattr(other, attr))
        for k, val in other.values.items():
            self.values[k] += val
        for k, val in other.sums.items():
            self.sums[k] += val
        self.reported |= other.reported
        self.committed |= other.committed
        self.estimated |= other.estimated
        self.measures += other.measures


@dataclass
class Report:
    rows: list[Row]
    total: Row
    year: int
    budget: bool  # an operating budget was found for the PA's cycle
    estimated: bool
    month_known: bool  # the month's spend could be worked out: January, or the previous month's report is confirmed


def budget_rows(organisation, year: int) -> list[dict]:
    """The PA's operating budget rows for a program year, from Budgets through the registry service ([] without one)."""
    return [b for b in registry.operating_budget(organisation, registry.cycle_for_year(year)) if b["year"] == year]


def report(v: SubmissionVersion, budget: list[dict] | None = None) -> Report:
    """The version by program: its spend (from the BCA inputs) and commitments, the operating budget for the program year (approved
    budget plus fund shifts in effect, BR-04.7) and the estimates. The month's spend (MonthExpenditures) is the change in the year-to-date
    spend since the previous month's standing report (January: the year to date). Programs in the operating budget but not in the
    report are listed as not reported."""
    ensure_lines(v)
    import_results(v)
    deadline = v.submission.deadline
    period = deadline.period
    prev = previous_month(deadline)
    previous = standing(v.submission.organisation, prev) if prev else None
    month_known = period.number == 1 or previous is not None
    prior = _previous_spend(previous)
    rows: dict[str, Row] = {}
    for ln in v.monthly_lines.all():
        r = rows.setdefault(ln.prg_id, Row(ln.prg_id))
        if ln.table == ReportLine.Table.SPEND:
            r.reported = True
        else:
            r.committed = True
        for k, val in ln.values.items():
            r.values[k] += Decimal(val)
        if ln.table == ReportLine.Table.SPEND and month_known:
            r.values["MonthExpenditures"] += Decimal(ln.values.get("YTDExpenditures", "0")) - prior.get(ln.prg_id, Decimal(0))
    for res in v.monthly_results.filter(run=(v.calculation or {}).get("run", "")):
        r = rows.setdefault(res.prg_id, Row(res.prg_id))
        r.name = r.name or res.program_name
        r.estimated = True
        r.measures += res.measures
        for k, val in res.values.items():
            r.sums[k] += val
    if budget is None:
        budget = budget_rows(v.submission.organisation, period.year)
    for b in budget:
        r = rows.setdefault(b["prg_id"], Row(b["prg_id"]))
        r.name = r.name or b.get("name", "")
        r.approved = (r.approved or Decimal(0)) + Decimal(b["approved"])
        r.operating = (r.operating or Decimal(0)) + Decimal(b["operating"])
    for p in Program.objects.filter(prg_id__in=[r.prg_id for r in rows.values() if not r.name]).prefetch_related("versions"):
        pv = registry._for_year(sorted(p.versions.all(), key=lambda x: x.valid_from, reverse=True), period.year)
        if pv:
            rows[p.prg_id].name = pv.name
    out = sorted(rows.values(), key=lambda r: r.prg_id)
    total = Row("Total")
    for r in out:
        total.add(r)
    return Report(out, total, period.year, bool(budget), any(r.estimated for r in out), month_known)


# --- the year in one view (BR-14.1)


@dataclass
class Month:
    deadline: Deadline
    version: SubmissionVersion | None
    totals: Row | None
    spend: Decimal | None = None  # the year-to-date spend less the month before's in this view (January: the year to date)

    @property
    def consistent(self) -> bool:
        """The reports are year to date, so the total spend should never fall from one month to the next."""
        return self.spend is None or self.spend >= -YTD_TOLERANCE


def year_view(organisation, year: int, *, published_only: bool = False) -> list[Month]:
    """Each month of the year with its standing report's totals (or, for the organisation's own readers, its latest version), the month's
    spend (the change in the year-to-date spend since the month before) and the accuracy check: the year-to-date spend should not fall
    from one month's report to the next."""
    out, before = [], Decimal(0)
    budget = budget_rows(organisation, year)
    for d in months(year):
        v = standing(organisation, d)
        if v is None and not published_only:
            sub = Submission.objects.filter(deadline=d, organisation=organisation).first()
            v = submissions.latest(sub) if sub else None
        if v is None:
            out.append(Month(d, None, None))
            before = None
            continue
        t = report(v, budget).total
        ytd = t.values.get("YTDExpenditures", Decimal(0))
        out.append(Month(d, v, t, None if before is None else ytd - before))
        before = ytd
    return out


# --- the 13th-month report (BR-14.2)


CLAIMED = [
    ("expenditures", "Expenditures", "money"),
    ("incentives", "Rebates and incentives", "money"),
    ("gross_kwh", "First-year gross kWh", "number"),
    ("gross_kw", "First-year gross kW", "number"),
    ("gross_therm", "First-year gross therms", "number"),
    ("net_kwh", "First-year net kWh", "number"),
    ("net_kw", "First-year net kW", "number"),
    ("net_therm", "First-year net therms", "number"),
    ("tsb", "TSB", "money"),
]
MONEY_CLAIMED = {"expenditures", "incentives"}


@dataclass
class ClosingRow:
    """One program of the 13th-month report: its claimed totals, its operating budget and December's report."""

    prg_id: str
    name: str = ""
    approved: Decimal | None = None
    operating: Decimal | None = None
    claimed: dict | None = None  # {CLAIMED key: total}, None when the program has no confirmed claims
    records: int = 0
    december_spend: Decimal | None = None  # December's report: spend, year to date
    december_tsb: float | None = None  # December's report: estimated TSB (unvalidated)

    def get(self, key: str):
        return None if self.claimed is None else self.claimed.get(key)

    @property
    def spent_share(self) -> float | None:
        spend = self.get("expenditures")
        return float(spend / self.operating) if spend is not None and self.operating else None

    @property
    def spend_difference(self) -> Decimal | None:
        """Claimed expenditures less December's year-to-date spend: what the claims add or take away."""
        if self.claimed is None and self.december_spend is None:
            return None
        return (self.get("expenditures") or Decimal(0)) - (self.december_spend or Decimal(0))

    @property
    def reconciles(self) -> bool:
        d = self.spend_difference
        return d is None or abs(d) <= YTD_TOLERANCE

    def add(self, other: "ClosingRow") -> None:
        for attr in ("approved", "operating", "december_spend"):
            if getattr(other, attr) is not None:
                setattr(self, attr, (getattr(self, attr) or Decimal(0)) + getattr(other, attr))
        if other.december_tsb is not None:
            self.december_tsb = (self.december_tsb or 0.0) + other.december_tsb
        if other.claimed is not None:
            self.claimed = self.claimed or {k: Decimal(0) if k in MONEY_CLAIMED else 0.0 for k, *_ in CLAIMED}
            for k, *_ in CLAIMED:
                self.claimed[k] += other.claimed[k]
        self.records += other.records


@dataclass
class Closing:
    """A PA's 13th-month report for a program year, generated from its confirmed claims (BR-14.2): final once the annual claim is
    confirmed, provisional (the confirmed quarters' year to date) before."""

    year: int
    rows: list[ClosingRow]
    total: ClosingRow
    quarters: list[int]
    final: bool
    december: SubmissionVersion | None  # December's standing monthly report, reconciled against the claims
    budget: bool

    @property
    def claimed(self) -> bool:
        return self.final or bool(self.quarters)

    @property
    def unreconciled(self) -> list[ClosingRow]:
        return [r for r in self.rows if not r.reconciles] if self.final and self.december else []


def closing(organisation, year: int) -> Closing:
    """The 13th-month report: each program's totals from the PA's confirmed claims for the year (submissions.claims_totals, which the
    Claims module answers), so it always matches the standing annual claim; the operating budget; and December's confirmed monthly
    report beside them, so the year's monthly spend reconciles to the claims."""
    totals = submissions.claims_totals(organisation, year)
    rows: dict[str, ClosingRow] = {}
    quarters, final = [], False
    for t in totals:
        r = rows.setdefault(t["prg_id"], ClosingRow(t["prg_id"]))
        claimed = {k: Decimal(str(t.get(k) or 0)) if k in MONEY_CLAIMED else float(t.get(k) or 0) for k, *_ in CLAIMED}
        r.add(ClosingRow(t["prg_id"], claimed=claimed, records=int(t.get("records") or 0)))
        quarters, final = sorted(set(quarters) | set(t.get("quarters") or [])), final or bool(t.get("final"))
    dec = Deadline.objects.filter(module=MODULE, period__kind=ReportingPeriod.Kind.MONTH, period__year=year, period__number=12).first()
    december = standing(organisation, dec) if dec else None
    if december is not None:
        for row in report(december).rows:
            if row.reported or row.estimated:
                r = rows.setdefault(row.prg_id, ClosingRow(row.prg_id))
                r.december_spend = row.get("YTDExpenditures")
                r.december_tsb = row.estimate("TotalSystemBenefit")
    budget = budget_rows(organisation, year)
    for b in budget:
        r = rows.setdefault(b["prg_id"], ClosingRow(b["prg_id"]))
        r.name = r.name or b.get("name", "")
        r.approved = (r.approved or Decimal(0)) + Decimal(b["approved"])
        r.operating = (r.operating or Decimal(0)) + Decimal(b["operating"])
    for p in Program.objects.filter(prg_id__in=[r.prg_id for r in rows.values() if not r.name]).prefetch_related("versions"):
        pv = registry._for_year(sorted(p.versions.all(), key=lambda x: x.valid_from, reverse=True), year)
        if pv:
            rows[p.prg_id].name = pv.name
    out = sorted(rows.values(), key=lambda r: r.prg_id)
    total = ClosingRow("Total")
    for r in out:
        total.add(r)
    return Closing(year, out, total, quarters, final, december, bool(budget))


def years():
    return YearSetup.objects.select_related("allocation_version").order_by("-year")


def published_files(v: SubmissionVersion) -> list[tuple[str, str, str]]:
    """(label, area, key) of what a confirmed report publishes: its upload, and the estimate run's job spec and receipt."""
    calc, out = v.calculation or {}, []
    f = report_file(v)
    if f:
        out.append(("The report (Commitments, Measure, ProgramCost)", "uploads", f["key"]))
    if calc.get("spec_key"):
        out.append(("Estimate run settings (job spec)", "results", calc["spec_key"]))
    if calc.get("receipt_key"):
        out.append(("Estimate run receipt (engine, reference data, outputs)", "results", calc["receipt_key"]))
    return out
