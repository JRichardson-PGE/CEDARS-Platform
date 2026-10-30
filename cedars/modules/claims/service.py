"""Quarterly and annual claims (BR-05). A claim year has five confirmations, each a deadline of the "claims" module in the submissions
service's calendar: Q1 to Q4 on the year's quarters, then the final annual one on the year (due the following spring), as CEDARS has
them. Each PA's filing for one of them is a submission with versions.

    upload (.zip or .xlsx: the records new or changed since the last confirmed quarter)
    -> compiled year to date (the earlier quarters' confirmed records, this upload's over them by primary key) -> checks -> validated
    -> priced (Calculations, through the submissions service: the year to date, allocated) -> confirm
    -> published: the compiled non-PII inputs, run settings and results readable by every signed-in user (BR-05.9)

A quarter is compiled on the standing version of the quarter before it, so the quarters are filed in order: a quarter is confirmed only
on the earlier quarter it was checked against, and only the most recently confirmed quarter is reopened (CEDARS' own guard). Records an
upload changes from an earlier quarter are counted and listed, never silently replaced."""

import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

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

from . import spec, upload
from .models import FILED_TOTALS, PROGRAM_COST_FIELDS, ClaimProgramLine, ClaimResult, ClaimYear

MODULE = "claims"
RULE_SET = "claims"
# The five confirmations of a claim year: (kind, the period it is on: quarter number, or 0 for the year).
PERIODS = [("Q1 claim", 1), ("Q2 claim", 2), ("Q3 claim", 3), ("Q4 claim", 4), ("Annual (final) claim", 0)]
# Only these deadlines are the module's: another "claims" deadline (one added by hand on the filing calendar) is not a claim year's.
KINDS = [kind for kind, _ in PERIODS]
INCENTIVE_FIELDS = ["UnitIncentiveToOthers", "UnitDirectInstallLab", "UnitDirectInstallMat", "UnitEndUserRebate"]
RESULT_KEYS = {"_pa", "_prg", "_prgname", "_sector", "_segment", "_year", "_n"}
# Tables the CEDARS claims rule set (CET's Measure and ProgramCost specification and rules) checks; the module checks the rest.
RULE_TABLES = ("Measure", "ProgramCost")


# --- claim years (the CPUC sets them up)


def default_acc(year: int) -> str:
    """The ACC claims for a year use: that of the year's final budget filing (the engine's table), or "" without the engine."""
    try:
        from recet import versions
    except ImportError:  # pragma: no cover - depends on the environment
        return ""
    hit = [c.acc for c in versions.claims_acc() if c.first <= year <= c.last]
    return str(hit[0]) if hit else ""


def due_at(day) -> datetime:
    """A due date as the end of that day."""
    return timezone.make_aware(datetime.combine(day, time(23, 59, 59)))


def _check_allocation(version: AllocationVersion | None) -> None:
    if version is not None and version.status != AllocationVersion.Status.CONFIRMED:
        raise AccessDenied(f"{version} is a draft; claims are priced only with a confirmed allocation version.")


def _setup_fields(cy: ClaimYear) -> dict:
    return {"year": cy.year, "acc": cy.acc_version, "allocation_version": str(cy.allocation_version or "most recent")}


@transaction.atomic
def add_year(actor, *, year: int, acc_version: str, due: dict, opens: dict | None = None, allocation_version=None, notes="") -> ClaimYear:
    """A claim year with its five deadlines (`due` = {kind: datetime} for every kind in PERIODS; `opens` optionally the same), priced
    on an ACC and an allocation version (or the most recent)."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator sets up claim years.")
    if ClaimYear.objects.filter(year=year).exists():
        raise AccessDenied(f"{year} claims are already set up.")
    if not acc_version:
        raise AccessDenied("Choose the ACC the year's claims are priced on.")
    _check_allocation(allocation_version)
    missing = [k for k, _ in PERIODS if not due.get(k)]
    if missing:
        raise AccessDenied(f"Give a due date for each confirmation: {', '.join(missing)}.")
    registry.ensure_periods(year)
    cy = ClaimYear.objects.create(year=year, acc_version=acc_version, allocation_version=allocation_version, notes=notes)
    for kind, n in PERIODS:
        period = ReportingPeriod.objects.get(kind="quarter" if n else "year", year=year, number=n)
        submissions.add_deadline(actor, module=MODULE, kind=kind, period=period, due_at=due[kind], opens_at=(opens or {}).get(kind))
    audit.record("claim_year.set_up", obj=cy, after=_setup_fields(cy), actor=actor)
    return cy


@transaction.atomic
def change_setup(actor, cy: ClaimYear, *, acc_version: str, allocation_version=None, reason: str) -> ClaimYear:
    """Change how a year's claims are priced. Versions already priced keep what they were priced with; the next pricing uses the new
    setting."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator changes how claims are priced.")
    if not reason.strip():
        raise AccessDenied("Give the reason for the change.")
    _check_allocation(allocation_version)
    before = _setup_fields(cy)
    cy.acc_version, cy.allocation_version = acc_version, allocation_version
    cy.save()
    audit.record("claim_year.setup_changed", obj=cy, before=before, after=_setup_fields(cy), reason=reason, actor=actor)
    return cy


def periods(year: int) -> list[Deadline]:
    """The year's deadlines in filing order: Q1 to Q4, then the annual one."""
    ds = Deadline.objects.filter(module=MODULE, kind__in=KINDS, period__year=year).select_related("period")
    return sorted(ds, key=number)


def number(deadline: Deadline) -> int:
    """A deadline's place in its year: 1 to 4 for the quarters, 5 for the annual confirmation."""
    return deadline.period.number if deadline.period.kind == ReportingPeriod.Kind.QUARTER else 5


def last_quarter(deadline: Deadline) -> int:
    """The latest ClaimYearQuarter a deadline's records may carry."""
    return min(number(deadline), 4)


def claim_year(deadline: Deadline) -> ClaimYear:
    return ClaimYear.objects.select_related("allocation_version").get(year=deadline.period.year)


def label(deadline: Deadline) -> str:
    return f"{deadline.period.year} {deadline.kind}"


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
    """A published version is public to every signed-in user (BR-05.9); the rest only to the organisation and CPUC staff."""
    if not user.is_authenticated:
        return False
    return v.status == SubmissionVersion.Status.PUBLISHED or can_read_submission(user, v.submission)


def standing(organisation, deadline: Deadline) -> SubmissionVersion | None:
    """The published version of a PA's filing for one confirmation."""
    return (
        SubmissionVersion.objects.filter(
            submission__organisation=organisation, submission__deadline=deadline, status=SubmissionVersion.Status.PUBLISHED
        )
        .order_by("-number")
        .first()
    )


def _versions(organisation, year: int):
    return SubmissionVersion.objects.filter(
        submission__organisation=organisation,
        submission__deadline__module=MODULE,
        submission__deadline__kind__in=KINDS,
        submission__deadline__period__year=year,
    ).select_related("submission__deadline__period")


def base_for(organisation, deadline: Deadline) -> SubmissionVersion | None:
    """What a confirmation's upload goes over: the standing version of the latest earlier confirmation that has one."""
    earlier = [d for d in periods(deadline.period.year) if number(d) < number(deadline)]
    for d in reversed(earlier):
        v = standing(organisation, d)
        if v is not None:
            return v
    return None


def later_locked(organisation, deadline: Deadline) -> Deadline | None:
    """A later confirmation of the same year that the organisation has confirmed, if any."""
    later = [d for d in periods(deadline.period.year) if number(d) > number(deadline)]
    for d in later:
        sub = Submission.objects.filter(deadline=d, organisation=organisation).first()
        v = submissions.latest(sub) if sub else None
        if v is not None and v.locked:
            return d
    return None


def year_standing(organisation, year: int) -> tuple[SubmissionVersion | None, list[int], bool]:
    """The version whose compiled tables are the PA's year to date (the standing version of its latest confirmation), the quarters
    confirmed, and whether the final annual confirmation is."""
    latest, quarters, final = None, [], False
    for d in periods(year):
        v = standing(organisation, d)
        if v is None:
            continue
        latest = v
        if number(d) <= 4:
            quarters.append(number(d))
        else:
            final = True
    return latest, quarters, final


# --- the upload, compiled over the year to date


@dataclass
class Problem:
    """A module check's finding, in the rule library's format (rules.Finding)."""

    row: int
    record_id: str
    table: str
    field: str
    rule_id: str
    severity: str
    message: str
    fix: str
    value: str = ""


def _key(table: str, row: dict) -> tuple:
    """A row's primary key; CEDARS compares keys ignoring case and surrounding spaces."""
    return tuple(row.get(c, "").strip().lower() for c in spec.KEYS[table])


def _claim(row: dict) -> str:
    return row.get("ClaimID", "").strip().lower()


@dataclass
class Compiled:
    tables: dict[str, upload.Table]
    replaced: dict[str, int] = field(default_factory=dict)
    added: dict[str, int] = field(default_factory=dict)
    changed_claims: list[str] = field(default_factory=list)


def compile_tables(base: dict[str, upload.Table], own: dict[str, upload.Table]) -> Compiled:
    """The year to date: the base's rows with this upload's over them by primary key. For ContactClaim and InfrastructureCost an upload's
    rows for a claim replace all the claim's earlier rows. Columns are the template's, in its order, that either side has."""
    out = Compiled({})
    for name in spec.TABLES:
        b, o = base.get(name), own.get(name)
        if b is None and o is None:
            continue
        have = set((b.columns if b else []) + (o.columns if o else []))
        columns = [c for c in spec.columns(name) if c in have]
        base_rows = list(b.rows) if b else []
        own_rows = list(o.rows) if o else []
        if name in spec.BY_CLAIM:
            claims = {_claim(r) for r in own_rows}
            before = {_claim(r) for r in base_rows}
            kept = [r for r in base_rows if _claim(r) not in claims]
            out.replaced[name] = len(claims & before)
            out.added[name] = len(claims - before)
        else:
            keys = {_key(name, r) for r in own_rows}
            before = {_key(name, r) for r in base_rows}
            kept = [r for r in base_rows if _key(name, r) not in keys]
            out.replaced[name] = len(keys & before)
            out.added[name] = len(keys - before)
            if name == "Measure":
                out.changed_claims = sorted(k[0] for k in keys & before)
        out.tables[name] = upload.Table(name, columns, kept + own_rows)
    return out


def _prefix(v: SubmissionVersion) -> str:
    return f"claims/{v.submission_id}/v{v.number}/"


def own_file(v: SubmissionVersion) -> dict | None:
    return next((f for f in v.files or [] if f.get("key", "").endswith("claims.zip")), None)


def own_tables(v: SubmissionVersion) -> dict[str, upload.Table]:
    f = own_file(v)
    return upload.from_zip(storage.store("uploads").get_bytes(f["key"])) if f else {}


def compiled_tables(v: SubmissionVersion) -> dict[str, upload.Table]:
    key = (v.findings or {}).get("compiled_key")
    if not key or not storage.store("uploads").exists(key):
        return {}
    return upload.from_zip(storage.store("uploads").get_bytes(key))


@transaction.atomic
def upload_claims(actor, deadline: Deadline, organisation, data: bytes, filename: str) -> SubmissionVersion:
    """Take a PA's upload for one confirmation: start the submission (or use its open draft), keep the upload as this version's records
    (the version's earlier upload is deleted: superseded uploads are not kept, BR-05.9), then compile and check it (`check`)."""
    if deadline.module != MODULE or deadline.kind not in KINDS or deadline.period_id is None:
        raise AccessDenied("That is not a claims deadline.")
    claim_year(deadline)
    _require_in_order(organisation, deadline)
    tables = {name: t for name, t in upload.read(data, filename).items() if t.rows}
    if not tables:
        raise upload.UploadError("The file has no records: each table it holds has only its column names.")
    v = submissions.start(actor, deadline, organisation)
    content = upload.to_zip(tables)
    sha = hashlib.sha256(content).hexdigest()
    folder = f"{_prefix(v)}{sha[:12]}/"
    store = storage.store("uploads")
    store.put_bytes(folder + "claims.zip", content)
    old = {f["key"].rsplit("/", 1)[0] + "/" for f in v.files or [] if f.get("key")} - {folder}
    transaction.on_commit(lambda: [store.delete_prefix(o) for o in old])
    ignored = {t.name: t.ignored for t in tables.values() if t.ignored}
    entry = {"name": "claims.zip", "key": folder + "claims.zip", "sha256": sha, "bytes": len(content), "uploaded_as": filename}
    if ignored:
        entry["ignored_columns"] = ignored
    v = submissions.attach_files(actor, v, [entry])
    return check(actor, v)


def _require_in_order(organisation, deadline: Deadline) -> None:
    later = later_locked(organisation, deadline)
    if later is not None:
        raise AccessDenied(
            f"{organisation.code}'s {label(later)} is already confirmed, and it was compiled on this one. Put corrections to these "
            "records in the latest confirmation's next upload."
        )
    for d in periods(deadline.period.year):
        if number(d) >= number(deadline):
            break
        sub = Submission.objects.filter(deadline=d, organisation=organisation).first()
        v = submissions.latest(sub) if sub else None
        if v is not None and not v.locked:
            raise AccessDenied(f"Confirm the {label(d)} first: each confirmation is compiled on the one before it.")


@transaction.atomic
def check(actor, v: SubmissionVersion) -> SubmissionVersion:
    """Compile the version's records over the year to date (the standing version of the confirmation before it), run the module's
    checks and the claims rule set on its own records, write the compiled tables and the job to price, and record the result: validated
    when nothing is rejected. Run again ("Check again") when an earlier confirmation has changed since."""
    v = SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation").get(pk=v.pk)
    deadline, org = v.submission.deadline, v.submission.organisation
    year = deadline.period.year
    own = own_tables(v)
    if not own:
        raise AccessDenied("Upload the claims first.")
    base_v = base_for(org, deadline)
    base = compiled_tables(base_v) if base_v else {}
    comp = compile_tables(base, own)
    problems = derive_checks(own, comp.tables, org, year, last_quarter(deadline))
    found, notes, rule_set = _rule_findings(own, year)
    problems += found
    own_f = own_file(v)
    folder = own_f["key"].rsplit("/", 1)[0] + "/"
    store = storage.store("uploads")
    compiled = upload.to_zip(comp.tables)
    store.put_bytes(folder + "compiled.zip", compiled)
    job = job_zip(comp.tables)
    if job:
        store.put_bytes(folder + "job.zip", job)
    store.put_bytes(folder + "findings.json", json.dumps([p.__dict__ for p in problems]).encode())
    _replace_lines(v, lines(comp.tables, year))
    summary = _summary(problems, sum(len(t.rows) for t in own.values()), notes, rule_set, folder + "findings.json")
    summary.update(
        {
            "base": base_v.pk if base_v else None,
            "base_sha256": (base_v.findings or {}).get("compiled_sha256", "") if base_v else "",
            "base_label": f"{label(base_v.submission.deadline)} version {base_v.number}" if base_v else "",
            "compiled_key": folder + "compiled.zip",
            "compiled_sha256": hashlib.sha256(compiled).hexdigest(),
            "job_key": folder + "job.zip" if job else "",
            "job_sha256": hashlib.sha256(job).hexdigest() if job else "",
            "tables": {t: len(own[t].rows) for t in spec.TABLES if t in own},
            "compiled": {t: len(comp.tables[t].rows) for t in spec.TABLES if t in comp.tables},
            "replaced": {t: n for t, n in comp.replaced.items() if n},
            "added": {t: n for t, n in comp.added.items() if n},
            "changed_claims": comp.changed_claims[:200],
            "changed_claims_count": len(comp.changed_claims),
        }
    )
    return submissions.record_validation(actor, v, rules.in_force(RULE_SET), summary)


def job_zip(tables: dict[str, upload.Table]) -> bytes:
    """The job the engine prices: the compiled Measure (ClaimID is the engine's CEInputID) and ProgramCost tables. b"" when there are
    no measures yet."""
    m = tables.get("Measure")
    if m is None or not m.rows:
        return b""
    measure = upload.Table("Measure", ["CEInputID" if c == "ClaimID" else c for c in m.columns], [_as_ceinput(r) for r in m.rows])
    cost = tables.get("ProgramCost") or upload.Table("ProgramCost", spec.columns("ProgramCost"), [])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Measure.csv", upload.to_csv(measure.columns, measure.rows))
        z.writestr("ProgramCost.csv", upload.to_csv(cost.columns, cost.rows))
    return out.getvalue()


def _as_ceinput(row: dict) -> dict:
    out = dict(row)
    out["CEInputID"] = out.pop("ClaimID", "")
    return out


# --- the module's checks

YEAR_QUARTER = re.compile(r"^(\d{4})Q([1-4])$", re.IGNORECASE)
CA_ZIP = re.compile(r"^(\d{5})(-\d{4})?$")
TRUE = {"1", "true", "yes", "y", "t"}
FALSE = {"0", "false", "no", "n", "f"}
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M")


def _num(value: str) -> Decimal | None:
    """A cell as a number: blank is 0, anything else not a number is None."""
    s = (value or "").strip()
    if not s:
        return Decimal(0)
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _flag(value: str) -> bool | None:
    s = (value or "").strip().lower()
    return True if s in TRUE else False if s in FALSE else None


def _date(value: str) -> date | None:
    s = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _type_problem(f: spec.Field, value: str) -> str | None:
    """What is wrong with a non-blank value for its type, or None."""
    if f.type == "text255" and len(value) > 255:
        return "is longer than 255 characters"
    if f.type == "text6" and len(value) > 6:
        return "is longer than 6 characters"
    if f.type == "number" and _num(value) is None:
        return "should be a number"
    if f.type == "integer" and not re.fullmatch(r"-?\d+(\.0+)?", value):
        return "should be a whole number"
    if f.type == "bit" and _flag(value) is None:
        return "should be 0 or 1 (or True or False)"
    if f.type == "date" and _date(value) is None:
        return "should be a date (YYYY-MM-DD or MM/DD/YYYY)"
    return None


FIX = {
    "text255": "Shorten it to 255 characters or fewer.",
    "text6": "Shorten it to 6 characters or fewer.",
    "number": "Enter a plain number: no text, units, commas or currency symbols.",
    "integer": "Enter a whole number.",
    "bit": "Enter 0 or 1 (or True or False).",
    "date": "Write the date as YYYY-MM-DD (or MM/DD/YYYY).",
}


def derive_checks(own: dict[str, upload.Table], compiled: dict[str, upload.Table], organisation, year: int, last_q: int) -> list[Problem]:
    """The module's checks (rule IDs CLM-*), on the upload's own records, with references checked against the compiled year to date:
    the template's columns, required fields and types for the tables the claims rule set does not cover (it covers Measure and
    ProgramCost), primary keys, ID prefixes, the claim year and quarter, programs in the registry and the PA's own, the links between
    the tables, and CEDARS' own data rules for the Claim and Site tables that a record can be checked against on its own."""
    problems: list[Problem] = []
    code = organisation.code
    rule_set_on = rules.in_force(RULE_SET) is not None

    def add(table, i, rid, fld, rule, message, fix, value="", severity="reject"):
        problems.append(Problem(i, rid, table, fld, rule, severity, message, fix, value))

    def rid_of(table, row):
        return " / ".join(row.get(c, "") for c in spec.KEYS[table])

    # columns, required fields, types and keys
    for name, t in own.items():
        missing = [c for c in spec.required(name) if c not in t.columns]
        for c in missing:
            add(name, 1, "", c, "CLM-COL", f"{name} has no {c} column", f"Add the {c} column; every {name} record needs it.")
        if missing:
            continue
        checked = name not in RULE_TABLES or not rule_set_on
        seen: dict[tuple, int] = {}
        for i, r in enumerate(t.rows, start=2):
            rid = rid_of(name, r)
            if checked:
                for f in spec.fields()[name]:
                    value = r.get(f.name, "")
                    if not value:
                        if f.required:
                            add(name, i, rid, f.name, "CLM-REQ", f"{f.name} is blank", f"Fill in {f.name}.")
                        continue
                    why = _type_problem(f, value)
                    if why:
                        add(name, i, rid, f.name, "CLM-TYPE", f"{f.name} {why}", FIX[f.type], value)
                k = _key(name, r)
                if name not in spec.BY_CLAIM and k in seen:
                    add(name, i, rid, ", ".join(spec.KEYS[name]), "CLM-KEY", f"{name} has this key on line {seen[k]} too",
                        "Keep one record per key; to correct a record, change it in place.")  # fmt: skip
                seen.setdefault(k, i)

    # IDs and years
    for name in ("Claim", "Measure"):
        for i, r in enumerate(own.get(name, upload.Table(name, [], [])).rows, start=2):
            cid = r.get("ClaimID", "")
            if cid and not any(cid.upper().startswith(f"{p}-{year}") for p in (code.upper(), "SW")):
                add(name, i, cid, "ClaimID", "CLM-ID", f"ClaimID {cid} does not start with {code}-{year} (or SW-{year})",
                    f"Start each ClaimID with the PA code and claim year, e.g. {code}-{year}-0001.", cid)  # fmt: skip
    for i, r in enumerate(own.get("Site", upload.Table("Site", [], [])).rows, start=2):
        sid = r.get("SiteID", "")
        if sid and not any(sid.upper().startswith(p) for p in (code.upper(), "SW")):
            add(
                "Site",
                i,
                sid,
                "SiteID",
                "CLM-ID",
                f"SiteID {sid} does not start with {code} (or SW)",
                f"Start each SiteID with {code}.",
                sid,
            )
    for name in ("Measure", "ProgramCost"):
        for i, r in enumerate(own.get(name, upload.Table(name, [], [])).rows, start=2):
            raw = r.get("ClaimYearQuarter", "")
            m = YEAR_QUARTER.match(raw.strip())
            if m and (int(m.group(1)) != year or int(m.group(2)) > last_q):
                add(name, i, rid_of(name, r), "ClaimYearQuarter", "CLM-YEAR",
                    f"ClaimYearQuarter {raw} is not a quarter of {year} up to Q{last_q}",
                    f"Use a quarter from {year}Q1 to {year}Q{last_q}; later claims go in their own quarter's upload.", raw)  # fmt: skip
    for name in ("ProgramCost", "Participant"):
        for i, r in enumerate(own.get(name, upload.Table(name, [], [])).rows, start=2):
            raw = r.get("PrgYear", "")
            if raw and raw.split(".")[0] != str(year):
                add(name, i, rid_of(name, r), "PrgYear", "CLM-YEAR", f"PrgYear {raw} is not the claim year {year}",
                    f"Use {year}: these are {year} claims.", raw)  # fmt: skip

    # programs
    ids = {
        r.get("PrgID", "")
        for name in ("Measure", "ProgramCost", "Participant", "InfrastructureCost")
        if name in own
        for r in own[name].rows
    }
    programs = {p.prg_id: p for p in Program.objects.filter(prg_id__in=ids)}
    for name in ("Measure", "ProgramCost", "Participant", "InfrastructureCost"):
        for i, r in enumerate(own.get(name, upload.Table(name, [], [])).rows, start=2):
            prg = r.get("PrgID", "")
            if not prg:
                continue
            p = programs.get(prg)
            if p is None:
                add(name, i, rid_of(name, r), "PrgID", "CLM-PRG", f"PrgID {prg} is not in the program registry",
                    "Use the PrgID of a program in the Programs catalog, or ask for the program to be added first.", prg)  # fmt: skip
            elif not p.statewide and p.administrator_id not in (None, organisation.pk):
                add(name, i, rid_of(name, r), "PrgID", "CLM-ORG", f"{prg} is another program administrator's program",
                    f"Claim only {code}'s programs and the statewide programs it leads.", prg)  # fmt: skip

    # links between the tables (the compiled year to date)
    claims = {_claim(r) for r in compiled.get("Claim", upload.Table("Claim", [], [])).rows}
    measures = {_claim(r): r for r in compiled.get("Measure", upload.Table("Measure", [], [])).rows}
    sites = {r.get("SiteID", "").strip().lower() for r in compiled.get("Site", upload.Table("Site", [], [])).rows}
    costs = {(r.get("PrgID", "").strip().lower()) for r in compiled.get("ProgramCost", upload.Table("ProgramCost", [], [])).rows}
    for i, r in enumerate(own.get("Measure", upload.Table("Measure", [], [])).rows, start=2):
        if r.get("ClaimID") and _claim(r) not in claims:
            add("Measure", i, r["ClaimID"], "ClaimID", "CLM-REF", f"Measure {r['ClaimID']} has no Claim record",
                "Add its Claim record (this upload's or an earlier confirmed quarter's).", r["ClaimID"])  # fmt: skip
        prg = r.get("PrgID", "")
        if prg and prg in programs and prg.strip().lower() not in costs:
            add("Measure", i, r.get("ClaimID", ""), "PrgID", "CLM-REF", f"{prg} has no {year} ProgramCost record",
                f"Add {prg}'s {year} ProgramCost record (its year-to-date costs, which may be zero).", prg)  # fmt: skip
    for i, r in enumerate(own.get("Claim", upload.Table("Claim", [], [])).rows, start=2):
        cid = r.get("ClaimID", "")
        if cid and _claim(r) not in measures:
            add("Claim", i, cid, "ClaimID", "CLM-REF", f"Claim {cid} has no Measure record",
                "Add its Measure record: every claim is priced from it.", cid)  # fmt: skip
        site = r.get("SiteID", "")
        if site and site.strip().lower() not in sites:
            add("Claim", i, cid, "SiteID", "CLM-REF", f"Site {site} has no Site record", "Add the Site record for it.", site)
    for name in ("ContactClaim", "InfrastructureCost"):
        for i, r in enumerate(own.get(name, upload.Table(name, [], [])).rows, start=2):
            if r.get("ClaimID") and _claim(r) not in claims:
                add(name, i, r["ClaimID"], "ClaimID", "CLM-REF", f"{name} names claim {r['ClaimID']}, which has no Claim record",
                    "Use the ClaimID of a claim in this upload or an earlier confirmed quarter.", r["ClaimID"])  # fmt: skip
    for i, r in enumerate(own.get("Participant", upload.Table("Participant", [], [])).rows, start=2):
        site = r.get("SiteID", "")
        if site and site.strip().lower() not in sites:
            add("Participant", i, rid_of("Participant", r), "SiteID", "CLM-REF", f"Site {site} has no Site record",
                "Add the Site record, or leave SiteID blank for a count that covers all of the program's sites.", site)  # fmt: skip

    # CEDARS' data rules on the Claim and Site tables
    for i, r in enumerate(own.get("Claim", upload.Table("Claim", [], [])).rows, start=2):
        _claim_rules(add, i, r, measures.get(_claim(r), {}), organisation)
    for i, r in enumerate(own.get("Site", upload.Table("Site", [], [])).rows, start=2):
        sid = r.get("SiteID", "")
        if r.get("SiteState") and r["SiteState"].strip().upper() != "CA":
            add("Site", i, sid, "SiteState", "CLM-SITE", f"SiteState is {r['SiteState']}, not CA",
                "Enter CA: claims are for California sites.", r["SiteState"])  # fmt: skip
        z = r.get("SiteZipCode", "").strip()
        m = CA_ZIP.match(z)
        if z and not (m and 90001 <= int(m.group(1)) <= 96162):
            add("Site", i, sid, "SiteZipCode", "CLM-SITE", f"SiteZipCode {z} is not a California ZIP code",
                "Enter the site's five-digit California ZIP code (90001 to 96162).", z)  # fmt: skip
        if _flag(r.get("Residential_Flag", "")) is False and not r.get("NAICSCode"):
            add("Site", i, sid, "NAICSCode", "CLM-SITE", "NAICSCode is blank for a non-residential site", "Fill in the site's NAICS code.")
    return problems


def _claim_rules(add, i: int, r: dict, measure: dict, organisation) -> None:
    """The Claim rules of CEDARS' 2026 data rules that need only the claim and its measure."""
    cid = r.get("ClaimID", "")
    impact = measure.get("MeasImpactType", "")
    if _flag(r.get("NMEC_TU_Flag", "")) and not r.get("ParentClaimID"):
        add("Claim", i, cid, "ParentClaimID", "CLM-NMEC", "ParentClaimID is blank on a NMEC true-up",
            "Give the ClaimID of the claim this true-up adjusts.")  # fmt: skip
    if "NMEC" in impact.upper() and not r.get("NMEC_TU_Flag"):
        add("Claim", i, cid, "NMEC_TU_Flag", "CLM-NMEC", "NMEC_TU_Flag is blank on a NMEC claim",
            "Enter 1 if the claim is a true-up of an earlier NMEC claim, else 0.")  # fmt: skip
    if impact in ("Cust-SEM", "Cust-SEM-FuelSub"):
        for fld in ("ParticipantID", "SEM_Phase"):
            if not r.get(fld):
                add("Claim", i, cid, fld, "CLM-SEM", f"{fld} is blank on a SEM claim ({impact})",
                    f"Fill in {fld}: it links the SEM participant's claims across the engagement.")  # fmt: skip
    if r.get("SEM_Phase") and impact and impact not in ("Cust-SEM", "Cust-SEM-FuelSub"):
        add("Claim", i, cid, "SEM_Phase", "CLM-SEM", f"SEM_Phase is filled in, but the measure's MeasImpactType is {impact}",
            "Use MeasImpactType Cust-SEM or Cust-SEM-FuelSub for a SEM claim, or leave SEM_Phase blank.", r["SEM_Phase"])  # fmt: skip
    if r.get("ClaimType", "").strip().lower() == "deemed":
        for fld in ("MeasDetailID", "ProjectID", "MeasImpactCalcType"):
            if not r.get(fld):
                add("Claim", i, cid, fld, "CLM-DEEMED", f"{fld} is blank on a deemed claim", f"Fill in {fld}: deemed claims need it.")
    if impact.endswith("FuelSub") and not r.get("TotalCustomerInfrastructureCosts"):
        add("Claim", i, cid, "TotalCustomerInfrastructureCosts", "CLM-FUELSUB",
            "TotalCustomerInfrastructureCosts is blank on a fuel substitution claim",
            "Enter the customer's infrastructure costs (0 if there were none) and add its InfrastructureCost record.")  # fmt: skip
    pct = r.get("PartialPaymentPercent", "")
    n = _num(pct)
    if pct and n is not None and not (0 <= n <= 1):
        add("Claim", i, cid, "PartialPaymentPercent", "CLM-PAYMENT", f"PartialPaymentPercent {pct} is not between 0 and 1",
            "Enter the share paid as a fraction, e.g. 0.5 for half.", pct)  # fmt: skip
    if _flag(r.get("PartialPaymentFinal_Flag", "")) and not pct:
        add("Claim", i, cid, "PartialPaymentPercent", "CLM-PAYMENT", "PartialPaymentPercent is blank on a final partial payment",
            "Enter the share of the incentive paid.")  # fmt: skip
    if organisation.pa_type in ("CCA", "REN") and _flag(r.get("CCAREN_Flag", "")) is not True:
        add("Claim", i, cid, "CCAREN_Flag", "CLM-CCAREN", f"CCAREN_Flag is not 1 on a {organisation.pa_type}'s claim",
            "Enter 1: every claim a CCA or REN files has it.", r.get("CCAREN_Flag", ""))  # fmt: skip


def _rule_findings(own: dict[str, upload.Table], year: int) -> tuple[list, list[str], str]:
    """The claims rule set in force (CEDARS' own Measure and ProgramCost rules, run by the engine) on the upload's own records, with the
    claim year as the filing year. A table without the specification's columns is rejected on those columns before its rules run."""
    rs = rules.in_force(RULE_SET)
    if rs is None:
        return [], ["No claims rule set is published yet, so only the module's own checks ran."], ""
    out, notes = [], set()
    for table, id_col in (("Measure", "CEInputID"), ("ProgramCost", "PrgID")):
        t = own.get(table)
        if t is None or not t.rows:
            continue
        rows = [_as_ceinput(r) for r in t.rows] if table == "Measure" else t.rows
        cols = ["CEInputID" if c == "ClaimID" else c for c in t.columns]
        try:
            df = rules.read_table(upload.to_csv(cols, rows))
        except (rules.EngineMissing, ImportError) as e:  # the engine (and pandas with it) is not installed here
            raise AccessDenied(f"{e} The claims cannot be validated on this server.") from None
        have = {c.lower() for c in df.columns}
        names = sorted(set(rs.rules.filter(table=table, kind="spec").values_list("field", flat=True)))
        missing = [c for c in names if c.lower() not in have and _recet_name(c).lower() not in have]
        if missing:
            out += [
                Problem(1, "", table, c if c != "CEInputID" else "ClaimID", "CLM-COL", "reject", f"{table} has no {c} column",
                        f"Add the {c} column (it may be blank).")
                for c in missing
            ]  # fmt: skip
            notes.add(f"The {table} rules were not run: the file lacks {len(missing)} of the claims specification's columns.")
            continue
        try:
            found, n = rules.check(rs, table, df, filing_year=year, id_column=id_col)
        except (KeyError, ValueError) as e:  # the engine could not apply a rule: the version cannot be called valid
            out.append(Problem(1, "", table, "", "CLM-RULES", "reject", f"The {table} records could not be checked: {e}",
                               "Tell the CPUC's CEDARS support; the file may be fine."))  # fmt: skip
            continue
        out += found
        notes.update(n)
    return out, sorted(notes), f"{rs.label} ({rs.effective_from})"


def _recet_name(column: str) -> str:
    from recet.fieldnames import recet_name

    return recet_name(column)


def _summary(problems: list, records: int, notes: list[str], rule_set: str, key: str) -> dict:
    rejected = {(p.table, p.row, p.record_id) for p in problems if p.severity == "reject"}
    warned = {(p.table, p.row, p.record_id) for p in problems if p.severity != "reject"} - rejected
    by_rule = defaultdict(lambda: [0, "", ""])
    for p in problems:
        by_rule[p.rule_id][0] += 1
        by_rule[p.rule_id][1:] = [p.severity, p.message]
    top = sorted(by_rule.items(), key=lambda kv: (kv[1][1] != "reject", -kv[1][0]))
    return {
        "records": records,
        "rejected": len(rejected),
        "warnings": len(warned),
        "notes": notes,
        "rule_set": rule_set,
        "findings_key": key,
        "findings": len(problems),
        "by_rule": [{"rule_id": k, "count": c, "severity": s, "example": m} for k, (c, s, m) in top[:20]],
    }


def findings(v: SubmissionVersion) -> list[dict]:
    key = (v.findings or {}).get("findings_key")
    if not key or not storage.store("uploads").exists(key):
        return []
    return json.loads(storage.store("uploads").get_bytes(key))


def findings_csv(v: SubmissionVersion) -> str:
    return rules.findings_csv([rules.Finding(**f) for f in findings(v)])


# --- each program's year to date


def lines(tables: dict[str, upload.Table], year: int) -> list[dict]:
    """Each program's year to date from the compiled tables: its ProgramCost row for the year, its measures' rebates and incentives,
    its number of claims and the totals filed on them."""
    out: dict[str, dict] = {}

    def line(prg: str) -> dict:
        return out.setdefault(
            prg,
            {"prg_id": prg, "pa": "", "program_costs": {}, "on_bill_financing": Decimal(0), "rebates_and_incentives": Decimal(0),
             "claims": 0, "filed": defaultdict(float)},
        )  # fmt: skip

    for r in tables.get("ProgramCost", upload.Table("ProgramCost", [], [])).rows:
        if r.get("PrgYear", "").split(".")[0] != str(year) or not r.get("PrgID"):
            continue
        ln = line(r["PrgID"])
        ln["pa"] = r.get("PA", "")
        ln["program_costs"] = {f: str(_num(r.get(f, "")) or 0) for f, _ in PROGRAM_COST_FIELDS}
        ln["on_bill_financing"] = _num(r.get("OnBillFinancing", "")) or Decimal(0)
    claim_rows = {_claim(r): r for r in tables.get("Claim", upload.Table("Claim", [], [])).rows}
    for r in tables.get("Measure", upload.Table("Measure", [], [])).rows:
        if not r.get("PrgID"):
            continue
        ln = line(r["PrgID"])
        units = _num(r.get("NumUnits", "")) or Decimal(0)
        ln["rebates_and_incentives"] += units * sum((_num(r.get(f, "")) or Decimal(0)) for f in INCENTIVE_FIELDS)
        ln["claims"] += 1
        c = claim_rows.get(_claim(r), {})
        for f in FILED_TOTALS:
            ln["filed"][f] += float(_num(c.get(f, "")) or 0)
    return [{**ln, "filed": dict(ln["filed"])} for ln in out.values()]


def _replace_lines(v: SubmissionVersion, rows: list[dict]) -> None:
    ClaimProgramLine.objects.filter(version=v).delete()
    ClaimProgramLine.objects.bulk_create(ClaimProgramLine(version=v, **row) for row in rows)
    ClaimResult.objects.filter(version=v).delete()


def ensure_checked(v: SubmissionVersion) -> None:
    """A reopened version (n+1) starts with its predecessor's upload: compile and check it."""
    if v.locked or v.findings or not own_file(v):
        return
    check(None, v)


# --- pricing and confirming


def price(actor, v: SubmissionVersion) -> str:
    """Price a validated version's year to date on the year's ACC (BR-02.5), with ACC-per-record checks for claims; a year with
    statewide programs is allocated with the year's allocation version, or the most recent confirmed one with factors for the year, and
    the run records which (BR-13.8). Returns the run's id."""
    import tempfile
    from pathlib import Path

    cy = claim_year(v.submission.deadline)
    key = (v.findings or {}).get("job_key")
    if not key:
        raise AccessDenied("There are no measures to price yet.")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "job.zip"
        path.write_bytes(storage.store("uploads").get_bytes(key))
        name = f"{v.submission.organisation.code} {label(v.submission.deadline)}, version {v.number}"
        statewide = Program.objects.filter(prg_id__in=v.claim_lines.values("prg_id"), statewide=True).exists()
        return submissions.price(
            actor,
            v,
            path,
            name=name,
            acc_version=cy.acc_version,
            allocation_version=cy.allocation_version,
            settings={"allocate_statewide": statewide, "acc_purpose": "claims"},
        )


def priced(v: SubmissionVersion) -> bool:
    """A finished run on exactly the version's current year-to-date job."""
    job = (v.findings or {}).get("job_sha256")
    return bool(v.calculation and job and v.calculation.get("job_sha256") == job)


def stale(v: SubmissionVersion) -> bool:
    """The confirmation before this one has changed since the version was compiled on it."""
    if v.locked or not v.findings:
        return False
    current = base_for(v.submission.organisation, v.submission.deadline)
    if (current.pk if current else None) != v.findings.get("base"):
        return True
    return bool(current) and (current.findings or {}).get("compiled_sha256", "") != v.findings.get("base_sha256", "")


def confirm(actor, v: SubmissionVersion) -> SubmissionVersion:
    """Confirm the priced version: the sign-off on exactly the results reviewed; publication follows with no further calculation."""
    if stale(v):
        raise AccessDenied("An earlier confirmation has changed since this version was compiled on it. Check it again, then price it.")
    _require_in_order(v.submission.organisation, v.submission.deadline)
    if not priced(v):
        raise AccessDenied("Price this version first: confirming needs a finished run on exactly these records.")
    return submissions.confirm(actor, v, v.calculation["input_hash"])


def unconfirm(actor, v: SubmissionVersion, reason: str = "") -> SubmissionVersion:
    later = later_locked(v.submission.organisation, v.submission.deadline)
    if later is not None:
        raise AccessDenied(f"The {label(later)} is confirmed on top of this one; it cannot be un-confirmed now.")
    return submissions.unconfirm(actor, v, reason)


def request_reopen(actor, submission: Submission, reason: str):
    """After the due date, only the most recently confirmed claim period can be reopened (as CEDARS has it): a later confirmation is
    compiled on this one."""
    later = later_locked(submission.organisation, submission.deadline)
    if later is not None:
        raise AccessDenied(
            f"Only the most recently confirmed period can be reopened, and the {label(later)} is confirmed. Correct these records in it."
        )
    return submissions.request_reopen(actor, submission, reason)


def import_results(v: SubmissionVersion) -> bool:
    """Read the version's priced results into ClaimResult rows (once per run): the run's group sums, summed by credited PA and program.
    False when the version has none."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    calc = v.calculation or {}
    key = (calc.get("results") or {}).get("group_sums")
    if not key:
        return False
    if ClaimResult.objects.filter(version=v, run=calc["run"]).exists():
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
        ClaimResult.objects.filter(version=v).delete()
        ClaimResult.objects.bulk_create(
            ClaimResult(version=v, run=calc["run"], pa=k[0], prg_id=k[1], program_name=s["name"], measures=s["n"], values=dict(s["values"]))
            for k, s in sums.items()
        )
    return True


def on_publish(v: SubmissionVersion) -> None:
    """Publication hook: the results the version was confirmed on are read now, so the published version holds them."""
    if v.submission.deadline.kind in KINDS:
        import_results(v)


def on_unpublish(v: SubmissionVersion) -> None:
    """Un-confirmed before the due date: nothing is kept apart from the version, which goes back to validated."""


# --- results by program

METRICS = [
    ("expenditures", "Expenditures", "money", "Program costs plus measure rebates and incentives, year to date, nominal dollars"),
    ("TotalSystemBenefit", "Total system benefit", "money", "Total system benefit (TSB), present value"),
    ("TRC", "TRC", "ratio", "Total Resource Cost ratio: TRC benefits / TRC cost"),
    ("PAC", "PAC", "ratio", "Program Administrator Cost ratio: PAC benefits / PAC cost"),
    ("FirstYearNetKWh", "First-year net kWh", "number", "First-year net electric savings, as priced"),
    ("FirstYearNetKW", "First-year net kW", "number", "First-year net peak demand savings, as priced"),
    ("FirstYearNetThm", "First-year net therms", "number", "First-year net gas savings, as priced"),
    ("TotalFirstYearNetkWh", "Filed first-year net kWh", "filed", "The claims' own TotalFirstYearNetkWh, as filed"),
    ("TotalFirstYearNetTherm", "Filed first-year net therms", "filed", "The claims' own TotalFirstYearNetTherm, as filed"),
]
RATIOS = {"TRC": ("TRCBen", "TRCCost"), "PAC": ("PACBen", "PACCost")}


@dataclass
class Row:
    prg_id: str
    name: str = ""
    pa: str = ""
    program_costs: Decimal = Decimal(0)
    rebates: Decimal = Decimal(0)
    on_bill_financing: Decimal = Decimal(0)
    by_field: dict = field(default_factory=lambda: defaultdict(Decimal))
    claims: int = 0
    filed: dict = field(default_factory=lambda: defaultdict(float))
    sums: dict = field(default_factory=lambda: defaultdict(float))
    priced: bool = False

    @property
    def expenditures(self) -> Decimal:
        return self.program_costs + self.rebates

    def value(self, key: str):
        if key == "expenditures":
            return self.expenditures
        if key in FILED_TOTALS:
            return self.filed.get(key, 0.0)
        if key in RATIOS:
            ben, cost = (self.sums.get(c) for c in RATIOS[key])
            return ben / cost if ben is not None and cost else None
        return self.sums.get(key) if self.priced else None

    def add(self, other: "Row") -> None:
        self.program_costs += other.program_costs
        self.rebates += other.rebates
        self.on_bill_financing += other.on_bill_financing
        self.claims += other.claims
        for k, val in other.by_field.items():
            self.by_field[k] += val
        for k, val in other.filed.items():
            self.filed[k] += val
        for k, val in other.sums.items():
            self.sums[k] += val
        self.priced = self.priced or other.priced


@dataclass
class Table:
    rows: list[Row]
    total: Row
    pas: list[str]
    priced: bool


def table(v: SubmissionVersion, *, pa: str = "") -> Table:
    """The version's year to date by program: what was filed (costs, incentives, the claims' own totals) and the priced results summed
    over the credited PAs (or one credited PA)."""
    ensure_checked(v)
    import_results(v)
    results = list(v.claim_results.filter(run=(v.calculation or {}).get("run", "")))
    rows: dict[str, Row] = {}
    for ln in v.claim_lines.all():
        r = rows.setdefault(ln.prg_id, Row(ln.prg_id))
        r.pa = ln.pa
        costs = {k: Decimal(val) for k, val in (ln.program_costs or {}).items()}
        for k, val in costs.items():
            r.by_field[k] += val
        r.program_costs += sum(costs.values(), Decimal(0))
        r.rebates += ln.rebates_and_incentives
        r.on_bill_financing += ln.on_bill_financing
        r.claims += ln.claims
        for k, val in (ln.filed or {}).items():
            r.filed[k] += val
    for res in results:
        if pa and res.pa != pa:
            continue
        r = rows.setdefault(res.prg_id, Row(res.prg_id))
        r.name = r.name or res.program_name
        r.priced = True
        for k, val in res.values.items():
            r.sums[k] += val
    names = {
        p.prg_id: p for p in Program.objects.filter(prg_id__in=rows).prefetch_related("versions")
    }  # the name in force in the claim year
    year = v.submission.deadline.period.year
    for r in rows.values():
        p = names.get(r.prg_id)
        pv = registry.version_for_year(p, year) if p else None
        if pv is not None:
            r.name = pv.name or r.name
    shown = sorted(rows.values(), key=lambda r: r.prg_id)
    total = Row("Total")
    for r in shown:
        total.add(r)
    return Table(shown, total, sorted({r.pa for r in results if r.pa}), bool(results))


def published_files(v: SubmissionVersion) -> list[tuple[str, str, str]]:
    """(label, area, key) of what a confirmed version publishes so it can be reproduced (BR-05.9): the compiled non-PII year-to-date
    inputs (every table of the template, the infrastructure cost table among them, BR-05.13), this version's own upload, the job priced,
    the run's job spec and its receipt."""
    s, calc, out = v.findings or {}, v.calculation or {}, []
    if s.get("compiled_key"):
        out.append(("Year-to-date inputs (every claims table, compiled)", "uploads", s["compiled_key"]))
    f = own_file(v)
    if f:
        out.append(("This version's upload (its new and changed records)", "uploads", f["key"]))
    if s.get("job_key"):
        out.append(("The job priced (Measure and ProgramCost)", "uploads", s["job_key"]))
    if calc.get("spec_key"):
        out.append(("Run settings (job spec)", "results", calc["spec_key"]))
    if calc.get("receipt_key"):
        out.append(("Run receipt (engine, reference data, outputs)", "results", calc["receipt_key"]))
    return out


# --- what other modules read (through the submissions service)


def year_totals(organisation, year: int) -> list[dict]:
    """A PA's confirmed claims for a year by program (submissions.claims_totals; the Monthly reports module's 13th-month report reads it):
    the year to date of its latest confirmation. Expenditures = the ten ProgramCost fields + the measures' rebates and incentives;
    savings and TSB are the priced first-year values summed over every credited PA."""
    v, quarters, final = year_standing(organisation, year)
    if v is None:
        return []
    t = table(v)
    out = []
    for r in t.rows:
        s = r.sums
        out.append(
            {
                "prg_id": r.prg_id,
                "pa": r.pa or organisation.code,
                "year": year,
                "quarters": quarters,
                "final": final,
                "records": r.claims,
                "expenditures": r.expenditures,
                "incentives": r.rebates,
                "gross_kwh": s.get("FirstYearGrossKWh", 0.0),
                "gross_kw": s.get("FirstYearGrossKW", 0.0),
                "gross_therm": s.get("FirstYearGrossThm", 0.0),
                "net_kwh": s.get("FirstYearNetKWh", 0.0),
                "net_kw": s.get("FirstYearNetKW", 0.0),
                "net_therm": s.get("FirstYearNetThm", 0.0),
                "tsb": s.get("TotalSystemBenefit", 0.0),
            }
        )
    return out
