"""Quarterly and annual claims (BR-05). A claim year has five confirmations, each a deadline of the "claims" module in the submissions
service's calendar: Q1 to Q4 on the year's quarters, then the final annual one on the year (due the following spring), as CEDARS has
them. Each PA's filing for one of them is a submission with versions.

    upload (.zip or .xlsx: the records new or changed since the last confirmed quarter)
    -> compiled year to date (the earlier quarters' confirmed records, this upload's over them by primary key) -> checks -> validated
    -> priced (Calculations, through the submissions service: the year to date, allocated) -> confirm
    -> published: the compiled non-PII inputs, run settings and results readable by every signed-in user (BR-05.9)

A quarter is compiled on the standing version of the quarter before it, so the quarters are filed in order: a quarter is confirmed only
on the earlier quarter it was checked against, and only the most recently confirmed quarter is reopened (CEDARS' own guard). Records an
upload changes from an earlier quarter are counted and listed, never silently replaced.

An upload withdraws a claim filed earlier in the year by naming it in its Withdrawal table with the reason (BR-05.2): the claim's records
stay in the compiled year, marked with the confirmation that withdrew it, but are no longer priced or counted. After the due date a
correction is a new version of the most recently confirmed confirmation (once the year's final claim is confirmed, of that one), and its
publication writes a public change letter (BR-09.3) of what it changed against the version it replaces."""

import csv
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

from cedars.core import letters, storage
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.legacy import service as legacy
from cedars.services.notifications.service import Concern
from cedars.services.refdata.models import PackVersion
from cedars.services.registry import service as registry
from cedars.services.registry.models import AllocationVersion, Program, ReportingPeriod
from cedars.services.rules import service as rules
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, Submission, SubmissionVersion
from cedars.services.vault import service as vault

from . import spec, upload
from .models import FILED_TOTALS, PROGRAM_COST_FIELDS, ChangeLetter, ClaimProgramLine, ClaimRecord, ClaimResult, ClaimYear

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
CLAIM_TABLES = ("Claim", "Measure", "ContactClaim", "InfrastructureCost", "ClaimSite")  # the tables that hold a claim's own records


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
    return {
        "year": cy.year,
        "acc": cy.acc_version,
        "allocation_version": str(cy.allocation_version or "most recent"),
        "cost_test_set": str(cy.cost_test_set or "in force"),
    }


def _check_cost_tests(pack) -> None:
    if pack is not None and (pack.kind != PackVersion.Kind.COST_TESTS or pack.status != PackVersion.Status.PUBLISHED):
        raise AccessDenied(f"{pack} is not a published cost-test set; a filing is priced only on a published one.")


@transaction.atomic
def add_year(
    actor, *, year: int, acc_version: str, due: dict, opens: dict | None = None, allocation_version=None, notes="", cost_test_set=None
) -> ClaimYear:
    """A claim year with its five deadlines (`due` = {kind: datetime} for every kind in PERIODS; `opens` optionally the same), priced
    on an ACC and an allocation version (or the most recent)."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator sets up claim years.")
    if ClaimYear.objects.filter(year=year).exists():
        raise AccessDenied(f"{year} claims are already set up.")
    if not acc_version:
        raise AccessDenied("Choose the ACC the year's claims are priced on.")
    _check_allocation(allocation_version)
    _check_cost_tests(cost_test_set)
    missing = [k for k, _ in PERIODS if not due.get(k)]
    if missing:
        raise AccessDenied(f"Give a due date for each confirmation: {', '.join(missing)}.")
    registry.ensure_periods(year)
    cy = ClaimYear.objects.create(
        year=year, acc_version=acc_version, allocation_version=allocation_version, notes=notes, cost_test_set=cost_test_set
    )
    for kind, n in PERIODS:
        period = ReportingPeriod.objects.get(kind="quarter" if n else "year", year=year, number=n)
        submissions.add_deadline(actor, module=MODULE, kind=kind, period=period, due_at=due[kind], opens_at=(opens or {}).get(kind))
    audit.record("claim_year.set_up", obj=cy, after=_setup_fields(cy), actor=actor)
    return cy


@transaction.atomic
def change_setup(actor, cy: ClaimYear, *, acc_version: str, allocation_version=None, reason: str, cost_test_set=None) -> ClaimYear:
    """Change how a year's claims are priced. Versions already priced keep what they were priced with; the next pricing uses the new
    setting."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator changes how claims are priced.")
    if not reason.strip():
        raise AccessDenied("Give the reason for the change.")
    _check_allocation(allocation_version)
    _check_cost_tests(cost_test_set)
    before = _setup_fields(cy)
    cy.acc_version, cy.allocation_version, cy.cost_test_set = acc_version, allocation_version, cost_test_set
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
    """A published version is public to every signed-in user (BR-05.9); the rest only to the organization and CPUC staff."""
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
    """A later confirmation of the same year that the organization has confirmed, if any."""
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


def compile_tables(
    base: dict[str, upload.Table], own: dict[str, upload.Table], withdrawn_in: str = "", corrected: dict[str, upload.Table] | None = None
) -> Compiled:
    """The year to date: the base's rows with this upload's over them by primary key. For the tables whose rows for a claim are a set
    (spec.BY_CLAIM) an upload's rows for a claim replace all the claim's earlier rows. The upload's withdrawals are marked with the
    confirmation that makes them (`withdrawn_in`); a correction that withdraws a claim the version it corrects filed (`corrected`, that
    version's compiled tables) keeps that claim's records as they were filed. Columns are the template's, in its order, that either
    side has."""
    out = Compiled({})
    for name in spec.TABLES:
        b, o = base.get(name), own.get(name)
        if b is None and o is None:
            continue
        have = set((b.columns if b else []) + (o.columns if o else []))
        own_rows = list(o.rows) if o else []
        if name == "Withdrawal" and own_rows:
            have.add("WithdrawnIn")
            own_rows = [{**r, "WithdrawnIn": withdrawn_in} for r in own_rows]
        columns = [c for c in spec.columns(name) if c in have]
        base_rows = list(b.rows) if b else []
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
    if corrected:
        have = {_claim(r) for name in CLAIM_TABLES for r in out.tables.get(name, upload.Table(name, [], [])).rows}
        keep = withdrawn(own) - have
        for name in CLAIM_TABLES:
            rows = [r for r in corrected.get(name, upload.Table(name, [], [])).rows if _claim(r) in keep]
            if rows:
                t = out.tables.get(name) or upload.Table(name, [], [])
                columns = [c for c in spec.columns(name) if c in set(t.columns) | set(corrected[name].columns)]
                out.tables[name] = upload.Table(name, columns, t.rows + rows)
    return out


def withdrawn(tables: dict[str, upload.Table]) -> set[str]:
    """The claims the tables withdraw (BR-05.2), as compared keys: their records stay, marked withdrawn, but are not priced or counted."""
    return {_claim(r) for r in tables.get("Withdrawal", upload.Table("Withdrawal", [], [])).rows if _claim(r)}


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
def upload_claims(
    actor, deadline: Deadline, organisation, data: bytes, filename: str, *, sources: list[dict] | None = None
) -> SubmissionVersion:
    """Take a PA's upload for one confirmation: start the submission (or use its open draft), keep the upload as this version's records
    (the version's earlier upload is deleted: superseded uploads are not kept, BR-05.9), then compile and check it (`check`).
    `sources`: the files a combined upload was made of (upload.combine), kept with the version's file entry."""
    if deadline.module != MODULE or deadline.kind not in KINDS or deadline.period_id is None:
        raise AccessDenied("That is not a claims deadline.")
    claim_year(deadline)
    _require_in_order(organisation, deadline)
    tables = {name: upload.without_system_fields(t) for name, t in upload.read(data, filename).items() if t.rows}
    if not tables:
        raise upload.UploadError("The file has no records: each table it holds has only its column names.")
    v = submissions.start(actor, deadline, organisation)
    content = upload.to_zip(tables)
    sha = hashlib.sha256(content).hexdigest()
    folder = f"{_prefix(v)}{sha[:12]}/"
    store = storage.store("uploads")
    store.put_bytes(folder + "claims.zip", content)
    # the superseded upload, once this one is recorded; a reopened version starts with its predecessor's files, which stay with it
    old = {f["key"].rsplit("/", 1)[0] + "/" for f in v.files or [] if f.get("key", "").startswith(_prefix(v))} - {folder}
    transaction.on_commit(lambda: [store.delete_prefix(o) for o in old])
    ignored = {t.name: t.ignored for t in tables.values() if t.ignored}
    entry = {"name": "claims.zip", "key": folder + "claims.zip", "sha256": sha, "bytes": len(content), "uploaded_as": filename}
    if ignored:
        entry["ignored_columns"] = ignored
    if sources:
        entry["sources"] = sources
    v = submissions.attach_files(actor, v, [entry])
    return check(actor, v)


def corrected_version(v: SubmissionVersion) -> SubmissionVersion | None:
    """The published version a correction (version 2 or later) replaces."""
    if v.number < 2:
        return None
    return (
        SubmissionVersion.objects.filter(submission=v.submission_id, number__lt=v.number, status=SubmissionVersion.Status.PUBLISHED)
        .select_related("submission__deadline__period", "submission__organisation")
        .order_by("-number")
        .first()
    )


def _adopt(actor, v: SubmissionVersion, f: dict) -> dict:
    """Copy a predecessor's upload into this version's own folder, so its compiled tables, job and findings are written there and the
    published version's files are never changed or deleted."""
    store = storage.store("uploads")
    content = store.get_bytes(f["key"])
    entry = {**f, "key": f"{_prefix(v)}{f['sha256'][:12]}/claims.zip"}
    store.put_bytes(entry["key"], content)
    submissions.attach_files(actor, v, [entry])
    return entry


def earlier_unconfirmed(organisation, deadline: Deadline) -> Deadline | None:
    """The first earlier confirmation of the year that has an upload but is not confirmed yet: it must be confirmed before this one
    takes an upload, as each confirmation is compiled on the one before it. None when this one may be uploaded."""
    for d in periods(deadline.period.year):
        if number(d) >= number(deadline):
            break
        sub = Submission.objects.filter(deadline=d, organisation=organisation).first()
        v = submissions.latest(sub) if sub else None
        if v is not None and not v.locked:
            return d
    return None


def _require_in_order(organisation, deadline: Deadline) -> None:
    later = later_locked(organisation, deadline)
    if later is not None:
        raise AccessDenied(
            f"{organisation.code}'s {label(later)} is already confirmed, and it was compiled on this one. Put corrections to these "
            "records in the latest confirmation's next upload."
        )
    d = earlier_unconfirmed(organisation, deadline)
    if d is not None:
        raise AccessDenied(f"Confirm the {label(d)} first: each confirmation is compiled on the one before it.")


@transaction.atomic
def check(actor, v: SubmissionVersion) -> SubmissionVersion:
    """Compile the version's records over the year to date (the standing version of the confirmation before it), run the module's
    checks and the claims rule set on its own records, write the compiled tables and the job to price, and record the result: validated
    when nothing is rejected. Run again ("Check again") when an earlier confirmation has changed since."""
    v = SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation").get(pk=v.pk)
    if v.locked:  # before anything is written: a published version's files never change
        raise AccessDenied("A confirmed version cannot be checked again; un-confirm it or ask to reopen.")
    deadline, org = v.submission.deadline, v.submission.organisation
    year = deadline.period.year
    own = own_tables(v)
    if not own:
        raise AccessDenied("Upload the claims first.")
    base_v = base_for(org, deadline)
    base = compiled_tables(base_v) if base_v else {}
    previous = corrected_version(v)
    corrected = compiled_tables(previous) if previous else {}
    deemed, etrm_summary = fill_deemed(own, base)
    comp = compile_tables(base, own, f"{label(deadline)}, version {v.number}", corrected)
    problems = deemed + derive_checks(own, comp.tables, org, year, last_quarter(deadline), base, corrected)
    found, notes, rule_set = _rule_findings(own, year)
    problems += found
    own_f = own_file(v)
    store = storage.store("uploads")
    if not own_f["key"].startswith(_prefix(v)):  # a reopened version's predecessor's upload: copy it, never write beside the original
        own_f = _adopt(actor, v, own_f)
        v = SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation").get(pk=v.pk)
    folder = own_f["key"].rsplit("/", 1)[0] + "/"
    compiled = upload.to_zip(comp.tables)
    store.put_bytes(folder + "compiled.zip", compiled)
    job = job_zip(comp.tables)
    if job:
        store.put_bytes(folder + "job.zip", job)
    store.put_bytes(folder + "findings.json", json.dumps([p.__dict__ for p in problems]).encode())
    _replace_lines(v, lines(comp.tables, year))
    _replace_records(v, records(comp.tables))
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
            "withdrawn": len(withdrawn(own)),
            "withdrawn_total": len(withdrawn(comp.tables)),
            "etrm": etrm_summary,
        }
    )
    if etrm_summary.get("note"):
        summary["notes"] = [*summary["notes"], etrm_summary["note"]]
    return submissions.record_validation(actor, v, rules.in_force(RULE_SET), summary)


# --- deemed claims against eTRM (BR-05.8)


def _decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def _same(f: spec.Field, filed: str, etrm_value: str) -> bool:
    """Whether a filed value agrees with eTRM's: numbers to the precision eTRM's download shows them at, flags as flags, text without
    regard to case."""
    if f.type in ("number", "integer"):
        a, b = _num(filed), _num(etrm_value)
        if a is not None and b is not None:
            tol = Decimal(5) * Decimal(10) ** -(_decimals(etrm_value) + 1)
            return abs(a - b) <= tol + Decimal("1e-9") * max(abs(a), abs(b))
    if f.type == "bit" and _flag(filed) is not None:
        return _flag(filed) == _flag(etrm_value)
    return filed.strip().casefold() == etrm_value.strip().casefold()


def fill_deemed(own: dict[str, upload.Table], base: dict[str, upload.Table]) -> tuple[list[Problem], dict]:
    """Check the upload's deemed claims (ClaimType Deemed with a MeasDetailID) against the published eTRM permutations pack in force,
    by MeasDetailID (BR-05.8): a blank Claim or Measure field that eTRM gives is filled in from it (in `own`, so the compiled tables and
    the job carry it, while the stored upload stays as filed), and a filed value that differs from eTRM's rejects the record (Jake,
    2026-10-30: deemed values are eTRM's), except the NTG fields, which can vary and are a warning (`etrm.WARN_ONLY`). Where eTRM says
    "Any", any value is valid and the PA supplies it: the field is neither filled nor compared, so a blank one is still required. A
    MeasDetailID the pack does not have is rejected when the pack has its measure version, and a
    warning (not checked) when it does not. A permutation not in effect in the claim's quarter is a warning."""
    from cedars.services.refdata import etrm
    from cedars.services.refdata import service as refdata

    claims_in = {r.get("ClaimID", ""): (i, r) for i, r in enumerate(base.get("Claim", upload.Table("Claim", [], [])).rows, start=0)}
    own_claims = own.get("Claim")
    for i, r in enumerate(own_claims.rows if own_claims else [], start=2):
        claims_in[r.get("ClaimID", "")] = (i, r)
    measures = defaultdict(list)
    m = own.get("Measure")
    for i, r in enumerate(m.rows if m else [], start=2):
        measures[r.get("ClaimID", "")].append((i, r))
    deemed = {
        cid: (i, r)
        for cid, (i, r) in claims_in.items()
        if r.get("ClaimType", "").strip().lower() == "deemed" and r.get("MeasDetailID", "").strip() and (i >= 2 or cid in measures)
    }
    if not deemed:
        return [], {}
    pack = refdata.in_force(etrm.KIND)
    if pack is None:
        return [], {"deemed": len(deemed), "note": "Deemed claims were not checked against eTRM: no published eTRM permutations pack."}
    found = etrm.lookup(pack, [r["MeasDetailID"] for _, r in deemed.values()])
    known = etrm.versions(pack, [etrm.measure_version(r["MeasDetailID"]) for _, r in deemed.values()])
    problems: list[Problem] = []
    filled: dict[str, int] = defaultdict(int)
    differ = varied = unchecked = 0
    fields = spec.fields()

    def fill(table: str, i: int, r: dict, rid: str, field_map: dict, values: dict, compare: bool) -> None:
        nonlocal differ, varied
        t = own.get(table)
        types = {f.name: f for f in fields[table]}
        for name, col in field_map.items():
            theirs = values.get(col, "")
            if not theirs or theirs.strip().lower() == etrm.ANY:
                continue
            mine = r.get(name, "")
            if not mine:
                r[name] = theirs
                filled[name] += 1
                if t is not None and name not in t.columns:
                    t.columns.append(name)
            elif compare and not _same(types[name], mine, theirs):
                differ += name not in etrm.WARN_ONLY
                varied += name in etrm.WARN_ONLY
                problems.append(
                    Problem(
                        i,
                        rid,
                        table,
                        name,
                        "CLM-ETRM",
                        "warning" if name in etrm.WARN_ONLY else "reject",
                        f"{name} {mine} differs from eTRM's {theirs} ({col})",
                        f"Enter eTRM's {theirs} or leave it blank to have it filled in; if the measure is not this permutation, "
                        "give the MeasDetailID of the one it is.",
                        mine,
                    )
                )

    for cid, (i, r) in deemed.items():
        mdid = r["MeasDetailID"].strip()
        p = found.get(mdid.lower())
        if p is None:
            mv = etrm.measure_version(mdid)
            if i < 2:  # a claim confirmed earlier this year, with new Measure records in this upload: its MeasDetailID was checked then
                unchecked += 1
            elif mv in known:
                why = f"MeasDetailID {mdid} is not a permutation of {mv.upper()} in eTRM"
                fix = "Copy the Measure Detail ID of the permutation the measure was installed under from eTRM."
                problems.append(Problem(i, cid, "Claim", "MeasDetailID", "CLM-ETRM", "reject", why, fix, mdid))
            else:
                unchecked += 1
                why = f"MeasDetailID {mdid} was not checked: the eTRM pack in force has no permutations of {mv.upper() or 'its measure'}"
                fix = "Nothing to fix if the ID is right; the measure version is not in the platform's eTRM data yet."
                problems.append(Problem(i, cid, "Claim", "MeasDetailID", "CLM-ETRM", "warning", why, fix, mdid))
            continue
        if i >= 2:
            fill("Claim", i, r, cid, etrm.CLAIM_FIELDS, p.values, True)
        for mi, mr in measures.get(cid, []):
            fill("Measure", mi, mr, cid, etrm.MEASURE_FIELDS, p.values, True)
            fill("Measure", mi, mr, cid, etrm.MEASURE_DESCRIPTIONS, p.values, False)
            q = _quarter_dates(mr.get("ClaimYearQuarter", ""))
            if q and ((p.end_date and q[0] > p.end_date) or (p.start_date and q[1] < p.start_date)):
                problems.append(
                    Problem(
                        mi,
                        cid,
                        "Measure",
                        "ClaimYearQuarter",
                        "CLM-ETRM",
                        "warning",
                        f"The permutation {mdid} is in effect {p.start_date or 'from the start'} to {p.end_date or 'now'}, not in "
                        f"{mr['ClaimYearQuarter']}",
                        "Check the MeasDetailID: use the measure version in effect when it was installed.",
                        mr["ClaimYearQuarter"],
                    )
                )
    summary = {
        "deemed": len(deemed),
        "checked": len(deemed) - unchecked - sum(1 for p in problems if p.severity == "reject"),
        "filled": dict(sorted(filled.items())),
        "filled_total": sum(filled.values()),
        "differ": differ,
        "ntg_differ": varied,
        "pack": refdata.pin([pack])[etrm.KIND],
    }
    summary["note"] = (
        f"{summary['checked']:,} of {len(deemed):,} deemed claims checked against {pack}: {summary['filled_total']:,} blank values "
        f"filled from eTRM, {differ:,} filed values differ from it (rejected), {varied:,} NTG values differ (warnings)."
    )
    return problems, summary


def _quarter_dates(text: str) -> tuple[date, date] | None:
    m = re.fullmatch(r"(\d{4})Q([1-4])", text.strip().upper())
    if not m:
        return None
    y, q = int(m.group(1)), int(m.group(2))
    end = date(y, 3 * q, 31 if q in (1, 4) else 30)
    return date(y, 3 * q - 2, 1), end


def job_zip(tables: dict[str, upload.Table]) -> bytes:
    """The job the engine prices: the compiled Measure (ClaimID is the engine's CEInputID) less the withdrawn claims, and the ProgramCost
    table. b"" when there are no measures to price."""
    m = tables.get("Measure")
    gone = withdrawn(tables)
    rows = [r for r in (m.rows if m else []) if _claim(r) not in gone]
    if not rows:
        return b""
    measure = upload.Table("Measure", ["CEInputID" if c == "ClaimID" else c for c in m.columns], [_as_ceinput(r) for r in rows])
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


def derive_checks(
    own: dict[str, upload.Table],
    compiled: dict[str, upload.Table],
    organisation,
    year: int,
    last_q: int,
    base: dict[str, upload.Table] | None = None,
    corrected: dict[str, upload.Table] | None = None,
) -> list[Problem]:
    """The module's checks (rule IDs CLM-*), on the upload's own records, with references checked against the compiled year to date:
    the template's columns, required fields and types for the tables the claims rule set does not cover (it covers Measure and
    ProgramCost), primary keys, ID prefixes, the claim year and quarter, programs in the registry and the PA's own, the links between
    the tables, withdrawals against the year so far (`base`, and `corrected`: the compiled tables of the version a correction replaces),
    and CEDARS' own data rules for the Claim and Site tables that a record can
    be checked against on its own."""
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
    for name in spec.BY_CLAIM:
        for i, r in enumerate(own.get(name, upload.Table(name, [], [])).rows, start=2):
            if r.get("ClaimID") and _claim(r) not in claims:
                add(name, i, r["ClaimID"], "ClaimID", "CLM-REF", f"{name} names claim {r['ClaimID']}, which has no Claim record",
                    "Use the ClaimID of a claim in this upload or an earlier confirmed quarter.", r["ClaimID"])  # fmt: skip
    for i, r in enumerate(own.get("ClaimSite", upload.Table("ClaimSite", [], [])).rows, start=2):
        site = r.get("SiteID", "")
        if site and site.strip().lower() not in sites:
            add("ClaimSite", i, rid_of("ClaimSite", r), "SiteID", "CLM-REF", f"Site {site} has no Site record",
                "Add the Site record for it.", site)  # fmt: skip
    for i, r in enumerate(own.get("Participant", upload.Table("Participant", [], [])).rows, start=2):
        site = r.get("SiteID", "")
        if site and site.strip().lower() not in sites:
            add("Participant", i, rid_of("Participant", r), "SiteID", "CLM-REF", f"Site {site} has no Site record",
                "Add the Site record, or leave SiteID blank for a count that covers all of the program's sites.", site)  # fmt: skip

    # withdrawals (BR-05.2): only a claim filed in an earlier confirmation (or in the version a correction replaces), once, with its
    # records left as they were filed; a withdrawn ClaimID is never filed again
    earlier = {_claim(r): r.get("WithdrawnIn", "") for r in (base or {}).get("Withdrawal", upload.Table("Withdrawal", [], [])).rows}
    gone = withdrawn(own)
    filed = {
        _claim(r)
        for t in (base or {}, corrected or {})
        for name in ("Claim", "Measure")
        for r in t.get(name, upload.Table(name, [], [])).rows
    }
    for i, r in enumerate(own.get("Withdrawal", upload.Table("Withdrawal", [], [])).rows, start=2):
        cid, k = r.get("ClaimID", ""), _claim(r)
        if not cid:
            continue
        if k in earlier:
            add("Withdrawal", i, cid, "ClaimID", "CLM-WD", f"Claim {cid} was already withdrawn in the {earlier[k]}",
                "Leave it out: a claim is withdrawn once.", cid)  # fmt: skip
        elif k not in filed:
            add("Withdrawal", i, cid, "ClaimID", "CLM-WD", f"Claim {cid} is not filed in {year}, so it cannot be withdrawn",
                f"Name a ClaimID already confirmed in {year}; to drop a claim of this upload, leave it out.", cid)  # fmt: skip
    for name in CLAIM_TABLES:
        for i, r in enumerate(own.get(name, upload.Table(name, [], [])).rows, start=2):
            cid, k = r.get("ClaimID", ""), _claim(r)
            if k in gone:
                add(name, i, cid, "ClaimID", "CLM-WD", f"This upload withdraws claim {cid} and also files {name} records for it",
                    "Leave its records out: a withdrawn claim keeps the records it was filed with.", cid)  # fmt: skip
            elif k in earlier:
                add(name, i, cid, "ClaimID", "CLM-WD", f"Claim {cid} was withdrawn in the {earlier[k]}",
                    "File it again under a new ClaimID: a withdrawn claim keeps its ID in the year's history.", cid)  # fmt: skip

    # NMEC chains (BR-05.3): a true-up's parent is a claim the PA filed, this year so far or in a confirmed earlier year, not withdrawn,
    # and never the claim itself or one of its own true-ups
    if any(r.get("ParentClaimID") for r in own.get("Claim", upload.Table("Claim", [], [])).rows):
        before = earlier_records(organisation, year)
        parents = {_claim(r): r.get("ParentClaimID", "").strip().lower() for r in compiled.get("Claim", upload.Table("Claim", [], [])).rows}
        out_now = withdrawn(compiled)
        for i, r in enumerate(own.get("Claim", upload.Table("Claim", [], [])).rows, start=2):
            cid, parent = r.get("ClaimID", ""), r.get("ParentClaimID", "")
            p = parent.strip().lower()
            if not p:
                continue
            older = before.get(p)
            if p == _claim(r):
                add("Claim", i, cid, "ParentClaimID", "CLM-CHAIN", "ParentClaimID is the claim itself",
                    "Give the ClaimID of the claim this true-up adjusts.", parent)  # fmt: skip
            elif p not in parents and older is None:
                add("Claim", i, cid, "ParentClaimID", "CLM-CHAIN", f"ParentClaimID {parent} is not a claim {code} has filed",
                    f"Give a ClaimID {code} filed this year or confirmed in an earlier year.", parent)  # fmt: skip
            elif p in out_now or (p not in parents and older.withdrawn):
                add("Claim", i, cid, "ParentClaimID", "CLM-CHAIN", f"ParentClaimID {parent} is a withdrawn claim",
                    "A true-up adjusts a claim that stands; give the ClaimID of the claim it replaces, if any.", parent)  # fmt: skip
            elif _cycle(_claim(r), parents):
                add("Claim", i, cid, "ParentClaimID", "CLM-CHAIN", f"ParentClaimID {parent} is one of this claim's own true-ups",
                    "A chain runs from the original claim to its latest true-up; check the ParentClaimIDs.", parent)  # fmt: skip
            else:
                impact = measures[p].get("MeasImpactType", "") if p in measures else (older.impact_type if older else "")
                if "NMEC" not in impact.upper():
                    add("Claim", i, cid, "ParentClaimID", "CLM-CHAIN", f"ParentClaimID {parent} is not a NMEC claim ({impact or 'none'})",
                        "Check the parent: a true-up adjusts a NMEC claim.", parent, severity="warning")  # fmt: skip

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


def _cycle(start: str, parents: dict[str, str]) -> bool:
    """Whether following ParentClaimIDs up from a claim comes back to it."""
    seen, k = set(), parents.get(start, "")
    while k and k not in seen:
        if k == start:
            return True
        seen.add(k)
        k = parents.get(k, "")
    return False


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
        names = rules.spec_columns(rs, table)
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
        "by_rule": [
            {"rule_id": k, "count": c, "severity": s, "example": m, "unit": "columns" if k.endswith("-COL") else "records"}
            for k, (c, s, m) in top[:20]
        ],
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
    its number of claims and the totals filed on them, leaving out withdrawn claims."""
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
    gone = withdrawn(tables)
    for r in tables.get("Measure", upload.Table("Measure", [], [])).rows:
        if not r.get("PrgID") or _claim(r) in gone:
            continue
        ln = line(r["PrgID"])
        units = _num(r.get("NumUnits", "")) or Decimal(0)
        ln["rebates_and_incentives"] += units * sum((_num(r.get(f, "")) or Decimal(0)) for f in INCENTIVE_FIELDS)
        ln["claims"] += 1
        c = claim_rows.get(_claim(r), {})
        for f in FILED_TOTALS:
            ln["filed"][f] += float(_num(c.get(f, "")) or 0)
    return [{**ln, "filed": dict(ln["filed"])} for ln in out.values()]


def records(tables: dict[str, upload.Table]) -> list[dict]:
    """Each claim of the compiled tables with the fields that link it to other claims (ClaimRecord)."""
    measures = {_claim(r): r for r in tables.get("Measure", upload.Table("Measure", [], [])).rows}
    sites = defaultdict(list)
    for r in tables.get("ClaimSite", upload.Table("ClaimSite", [], [])).rows:
        sites[_claim(r)].append(r.get("SiteID", ""))
    gone = withdrawn(tables)
    out = []
    for r in tables.get("Claim", upload.Table("Claim", [], [])).rows:
        k = _claim(r)
        if not k:
            continue
        m = measures.get(k, {})
        out.append(
            {
                "claim_id": r.get("ClaimID", "").strip(),
                "prg_id": m.get("PrgID", ""),
                "impact_type": m.get("MeasImpactType", "")[:60],
                "parent_claim_id": r.get("ParentClaimID", "").strip(),
                "true_up": bool(_flag(r.get("NMEC_TU_Flag", ""))),
                "participant_id": r.get("ParticipantID", "").strip(),
                "sem_phase": r.get("SEM_Phase", "").strip(),
                "project_id": r.get("ProjectID", "").strip(),
                "site_id": r.get("SiteID", "").strip(),
                "sites": sorted(set(sites.get(k, []))),
                "withdrawn": k in gone,
                "filed": {f: float(_num(r.get(f, "")) or 0) for f in FILED_TOTALS},
            }
        )
    return out


def _replace_records(v: SubmissionVersion, rows: list[dict]) -> None:
    ClaimRecord.objects.filter(version=v).delete()
    ClaimRecord.objects.bulk_create(ClaimRecord(version=v, **row) for row in rows)


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
            cost_test_set=cy.cost_test_set,
        )


def pricing_run(v: SubmissionVersion) -> str | None:
    """The run started for the version since its records last changed, while it has not ended; the pages use
    submissions.pricing_run, which also tells them about a run that ended without pricing the version."""
    state = submissions.pricing_run(v, reset_on=("submission.files_attached", "submission.validated"))
    return state["id"] if state and not state["ended"] else None


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
    """Publication hook: the results the version was confirmed on are read now, so the published version holds them; a correction
    (version 2 or later) gets its change letter."""
    if v.submission.deadline.kind in KINDS:
        import_results(v)
        if v.number > 1:
            write_change_letter(v)


def on_unpublish(v: SubmissionVersion) -> None:
    """Un-confirmed before the due date: nothing is kept apart from the version, which goes back to validated."""


# --- personal information (the PII vault)


PII_KEYS = [
    ("ContactClaim", "ContactID", "PII_Contact"),
    ("Site", "SiteID", "PII_Site"),
    ("Claim", "SiteID", "PII_Site"),
    ("ClaimSite", "SiteID", "PII_Site"),
]


def pii_keys(v: SubmissionVersion) -> dict[str, set[str]]:
    """The ContactIDs and SiteIDs the version's compiled claims name (compared in lower case): a PII file may hold only these."""
    t = compiled_tables(v)
    keys = {"PII_Contact": set(), "PII_Site": set()}
    for table, column, pii in PII_KEYS:
        if table in t:
            keys[pii] |= {r.get(column, "").strip().lower() for r in t[table].rows if r.get(column, "").strip()}
    return keys


def pii_versions(v: SubmissionVersion) -> list[int]:
    """The version and the earlier versions of its submission, newest first: a reopened version reads its predecessor's PII until it
    has its own."""
    return list(v.submission.versions.filter(number__lte=v.number).order_by("-number").values_list("pk", flat=True))


def upload_pii(actor, v: SubmissionVersion, data: bytes, filename: str, assurance: str | None):
    """Check a PII file against the version's compiled claims and keep it in the vault (BR-10.1, BR-10.4). Only while the version is
    open, and only once its claims are checked: the PII joins them by ContactID and SiteID."""
    if v.locked:
        raise AccessDenied("This version is confirmed. Ask to reopen it to change its personal information.")
    ensure_checked(v)
    v.refresh_from_db()
    keys = pii_keys(v)
    if not any(keys.values()):
        raise AccessDenied("Upload and check the claims first: the PII file may name only their ContactIDs and SiteIDs.")
    return vault.store(actor, v.submission.organisation, v.pk, data, filename, keys, assurance)


def pii(v: SubmissionVersion):
    """The version's PII upload as it stands (its own, else its predecessor's), or None."""
    return vault.latest(pii_versions(v)) if vault.enabled() else None


# --- withdrawals and corrections


def withdrawals(v: SubmissionVersion) -> list[dict]:
    """The claims withdrawn in the year to date of a version, in the order withdrawn: ClaimID, reason, the confirmation that withdrew it
    and the date it was confirmed (blank while that confirmation is not)."""
    rows = compiled_tables(v).get("Withdrawal")
    if rows is None:
        return []
    org, year = v.submission.organisation, v.submission.deadline.period.year
    when = {f"{label(x.submission.deadline)}, version {x.number}": x.confirmed_at for x in _versions(org, year)}
    return [
        {"claim": r.get("ClaimID", ""), "reason": r.get("WithdrawalReason", ""), "withdrawn_in": r.get("WithdrawnIn", ""),
         "confirmed_at": when.get(r.get("WithdrawnIn", ""))}
        for r in rows.rows
    ]  # fmt: skip


LETTER_LIST = 200  # claims listed by name in each group of a change letter; the counts are always whole
LETTER_FIGURES = [
    ("claims", "Claims", "count"),
    ("expenditures", "Expenditures", "money"),
    ("TotalSystemBenefit", "Total system benefit", "money"),
    ("FirstYearNetKWh", "First-year net kWh", "number"),
    ("FirstYearNetThm", "First-year net therms", "number"),
]


def _claim_records(tables: dict[str, upload.Table]) -> dict[str, tuple[str, tuple]]:
    """{claim key: (ClaimID as filed, every record of the claim)} over the tables that hold a claim's records."""
    out: dict[str, tuple[str, list]] = {}
    for name in CLAIM_TABLES:
        for r in tables.get(name, upload.Table(name, [], [])).rows:
            k = _claim(r)
            if k:
                out.setdefault(k, (r.get("ClaimID", ""), []))[1].append((name, tuple(sorted((c, x) for c, x in r.items() if x))))
    return {k: (cid, tuple(sorted(recs))) for k, (cid, recs) in out.items()}


def _table_changes(before: dict[str, upload.Table], after: dict[str, upload.Table]) -> list[dict]:
    """Each table's records before and after, and how many the correction added, changed and removed (by primary key; by claim for
    the tables whose rows for a claim are a set)."""
    out = []
    for name in spec.TABLES:
        b, a = before.get(name), after.get(name)
        if b is None and a is None:
            continue
        group = _claim if name in spec.BY_CLAIM else (lambda r, n=name: _key(n, r))
        rows_b, rows_a = defaultdict(set), defaultdict(set)
        for rows, into in ((b.rows if b else [], rows_b), (a.rows if a else [], rows_a)):
            for r in rows:
                into[group(r)].add(tuple(sorted((c, x) for c, x in r.items() if x)))
        both = rows_b.keys() & rows_a.keys()
        out.append(
            {
                "table": name,
                "before": len(b.rows) if b else 0,
                "after": len(a.rows) if a else 0,
                "added": len(rows_a.keys() - rows_b.keys()),
                "changed": sum(1 for k in both if rows_a[k] != rows_b[k]),
                "removed": len(rows_b.keys() - rows_a.keys()),
            }
        )
    return out


def _figures(r: "Row") -> dict:
    out = {}
    for key, _, kind in LETTER_FIGURES:
        x = r.claims if key == "claims" else r.value(key)
        out[key] = None if x is None else (int(x) if kind == "count" else float(x))
    return out


def correction_changes(previous: SubmissionVersion, v: SubmissionVersion) -> dict:
    """What a correction changed against the version it replaces: claims added, changed (any of their records), removed, withdrawn
    (with the reason) and reinstated; each table's records; each program whose year to date changed, before and after; and the totals."""
    before, after = compiled_tables(previous), compiled_tables(v)
    a, b = _claim_records(before), _claim_records(after)
    wa = {_claim(r): r for r in before.get("Withdrawal", upload.Table("Withdrawal", [], [])).rows}
    wb = {_claim(r): r for r in after.get("Withdrawal", upload.Table("Withdrawal", [], [])).rows}

    def listed(ids: list) -> dict:
        return {"count": len(ids), "ids": ids[:LETTER_LIST]}

    claims = {
        "added": listed(sorted(b[k][0] for k in b.keys() - a.keys())),
        "changed": listed(sorted(b[k][0] for k in a.keys() & b.keys() if a[k][1] != b[k][1])),
        "removed": listed(sorted(a[k][0] for k in a.keys() - b.keys())),
        "reinstated": listed(sorted(wa[k].get("ClaimID", "") for k in wa.keys() - wb.keys())),
    }
    gone = sorted(wb.keys() - wa.keys())
    claims["withdrawn"] = {
        "count": len(gone),
        "ids": [{"claim": wb[k].get("ClaimID", ""), "reason": wb[k].get("WithdrawalReason", "")} for k in gone[:LETTER_LIST]],
    }
    tb, ta = table(previous), table(v)
    rows_b, rows_a = {r.prg_id: r for r in tb.rows}, {r.prg_id: r for r in ta.rows}
    programs = []
    for prg in sorted(rows_b.keys() | rows_a.keys()):
        fb = _figures(rows_b[prg]) if prg in rows_b else None
        fa = _figures(rows_a[prg]) if prg in rows_a else None
        if fb != fa:
            name = (rows_a.get(prg) or rows_b.get(prg)).name
            programs.append({"prg_id": prg, "name": name, "before": fb, "after": fa})
    return {
        "previous_label": f"{label(previous.submission.deadline)}, version {previous.number}",
        "claims": claims,
        "tables": _table_changes(before, after),
        "programs": programs,
        "totals": {"before": _figures(tb.total), "after": _figures(ta.total)},
        "priced": {"before": tb.priced, "after": ta.priced},
    }


def write_change_letter(v: SubmissionVersion) -> ChangeLetter | None:
    """Write a correction's change letter on its publication (once); None when there is no earlier published version to compare."""
    if ChangeLetter.objects.filter(version=v).exists():
        return None
    previous = corrected_version(v)
    if previous is None:
        return None
    changes = correction_changes(previous, v)
    letter = ChangeLetter.objects.create(version=v, previous=previous, reason=v.reason, published_at=timezone.now(), changes=changes)
    c = changes["claims"]
    audit.record(
        "claims.change_letter_published",
        obj=letter,
        reason=v.reason,
        after={
            "version": v.pk,
            "previous": previous.pk,
            **{k: c[k]["count"] for k in ("added", "changed", "removed", "withdrawn", "reinstated")},
            "programs": len(changes["programs"]),
        },
        organisation=v.submission.organisation,
    )
    return letter


def change_letters(*, organisation=None, year: int | None = None):
    """Published change letters, newest first."""
    out = ChangeLetter.objects.select_related("version__submission__deadline__period", "version__submission__organisation", "previous")
    if organisation is not None:
        out = out.filter(version__submission__organisation=organisation)
    if year is not None:
        out = out.filter(version__submission__deadline__period__year=year)
    return out


def notice(v: SubmissionVersion, event) -> Concern:
    """What a notification about one of the module's versions says (registered with the notification service): the programs it
    claims, or for a published correction the programs it changed, a summary and the page to open."""
    what = f"{v.submission.organisation.code} {label(v.submission.deadline)}"
    link = f"/claims/versions/{v.pk}/"
    lines = list(v.claim_lines.all())
    programs = {ln.prg_id for ln in lines}
    if event.action == "submission.published":
        letter = ChangeLetter.objects.filter(version=v).first()
        if letter is not None:
            c = letter.changes or {}
            counts = c.get("claims", {})
            claims = ", ".join(
                f"{k} {counts.get(k, {}).get('count', 0):,}" for k in ("added", "changed", "removed", "withdrawn", "reinstated")
            )
            parts = [f"Reason: {letter.reason}" if letter.reason else "", f"Claims {claims}.", letters.summary(c, LETTER_FIGURES)]
            return Concern(
                programs={p["prg_id"] for p in c.get("programs", [])},
                link=f"/claims/changes/{letter.pk}/",
                title=f"Corrected claims: {what}, version {v.number}",
                body="\n".join(x for x in parts if x) + "\nThe change letter has every change.",
            )
        n = sum(ln.claims for ln in lines)
        return Concern(
            programs=programs,
            link=link,
            title=f"Claims published: {what}",
            body=f"{len(programs)} program{'s' if len(programs) != 1 else ''}, {n:,} claim{'s' if n != 1 else ''} year to date.",
        )
    return Concern(programs=programs, link=link)


# --- links between claims: NMEC chains, SEM participants, projects (BR-05.3, 05.5, 05.7, 05.11)


def standing_versions(*, organisation=None, year: int | None = None) -> list[SubmissionVersion]:
    """Each PA's year to date that stands: per organization and claim year, the published version of its latest confirmation."""
    qs = SubmissionVersion.objects.filter(
        status=SubmissionVersion.Status.PUBLISHED, submission__deadline__module=MODULE, submission__deadline__kind__in=KINDS
    ).select_related("submission__deadline__period", "submission__organisation")
    if organisation is not None:
        qs = qs.filter(submission__organisation=organisation)
    if year is not None:
        qs = qs.filter(submission__deadline__period__year=year)
    best: dict[tuple, SubmissionVersion] = {}
    for v in qs:
        k = (v.submission.organisation_id, v.submission.deadline.period.year)
        rank = (number(v.submission.deadline), v.number)
        if k not in best or rank > (number(best[k].submission.deadline), best[k].number):
            best[k] = v
    return list(best.values())


def _standing_records(**filters):
    versions = standing_versions()
    return (
        ClaimRecord.objects.filter(version__in=versions, **filters)
        .select_related("version__submission__deadline__period", "version__submission__organisation")
        .order_by("version__submission__deadline__period__year", "claim_id")
    )


def earlier_records(organisation, year: int) -> dict[str, ClaimRecord]:
    """The PA's confirmed claims of earlier claim years, by compared ClaimID: the parents a true-up may name."""
    versions = [v for v in standing_versions(organisation=organisation) if v.submission.deadline.period.year < year]
    return {r.claim_id.strip().lower(): r for r in ClaimRecord.objects.filter(version__in=versions)}


def find_claim(claim_id: str) -> ClaimRecord | None:
    """A confirmed claim, wherever it stands (ClaimIDs carry the PA code and claim year, so they are unique)."""
    return _standing_records(claim_id__iexact=claim_id.strip()).first()


@dataclass
class ChainLink:
    record: ClaimRecord
    depth: int
    change: dict  # {Claim total field: this claim's filed value less its parent's}


def chain(claim_id: str) -> list[ChainLink]:
    """A NMEC claim's chain end to end (BR-05.3): from the original claim down through every true-up, in filing order, with each
    true-up's filed totals against its parent's (the true-up's adjustment). Empty when the claim is not confirmed."""
    start = find_claim(claim_id)
    if start is None:
        return []
    org = start.version.submission.organisation
    mine = list(_standing_records(version__submission__organisation=org))
    by_id = {r.claim_id.lower(): r for r in mine}
    root, seen = start, {start.claim_id.lower()}
    while root.parent_claim_id and root.parent_claim_id.lower() in by_id and root.parent_claim_id.lower() not in seen:
        root = by_id[root.parent_claim_id.lower()]
        seen.add(root.claim_id.lower())
    children = defaultdict(list)
    for r in mine:
        if r.parent_claim_id:
            children[r.parent_claim_id.lower()].append(r)
    out, todo, done = [], [(root, 0, None)], set()
    while todo:
        r, depth, parent = todo.pop()
        if r.claim_id.lower() in done:
            continue
        done.add(r.claim_id.lower())
        change = {f: r.filed.get(f, 0.0) - parent.filed.get(f, 0.0) for f in FILED_TOTALS} if parent else {}
        out.append(ChainLink(r, depth, change))
        todo += [(c, depth + 1, r) for c in sorted(children[r.claim_id.lower()], key=lambda c: c.claim_id, reverse=True)]
    return out


def participant_claims(participant_id: str):
    """Every confirmed claim of one SEM participant, across years and phases (BR-05.7)."""
    return _standing_records(participant_id__iexact=participant_id.strip())


def project_claims(project_id: str, organisation=None):
    """Every confirmed claim of one project (ProjectID, the project key, BR-05.11): the meters of a multi-meter project, a population
    cohort and its projects, a claim before and after it was reclassified."""
    qs = _standing_records(project_id__iexact=project_id.strip())
    return qs.filter(version__submission__organisation=organisation) if organisation is not None else qs


def links_csv(v: SubmissionVersion) -> str:
    """The version's claims with their links (project, NMEC parent, SEM participant and phase, sites), for downloads and evaluation
    samples grouped by project."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["ClaimID", "PrgID", "ProjectID", "ParentClaimID", "NMEC_TU_Flag", "MeasImpactType", "ParticipantID", "SEM_Phase",
                "SiteID", "ClaimSites", "Status"])  # fmt: skip
    for r in v.claim_records.all():
        w.writerow([r.claim_id, r.prg_id, r.project_id, r.parent_claim_id, int(r.true_up), r.impact_type, r.participant_id,
                    r.sem_phase, r.site_id, ";".join(r.sites), "withdrawn" if r.withdrawn else "active"])  # fmt: skip
    return out.getvalue()


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
    the run's job spec and its receipt. A version imported from legacy CEDARS publishes the tables it was loaded as and its
    reconciliation report."""
    if v.imported:
        return legacy.published_files(v)
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
    savings and TSB are the priced first-year values summed over every credited PA. A legacy year's statewide unallocated claims (the SW
    version, `legacy.py`) are outside every total: none."""
    v, quarters, final = year_standing(organisation, year)
    if v is None or (v.calculation or {}).get("outside_totals"):
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
