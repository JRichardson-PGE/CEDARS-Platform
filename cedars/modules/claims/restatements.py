"""Restated results for confirmed claims (BR-06.17): a published version's year to date priced again on the current engine, as an analysis
that never changes the filed record (BRD v0.5: "re-run prior claims with corrected calculations ... to show restated results, without
changing the official filed record"), optionally on another ACC, or an ACC per record (BR-06.5), "so a change in results can be separated
into the effect of the ACC and the effect of the program".

    filed       the version as confirmed: its run (release, ACC, programs, allocation, cost-test set at the time)
    bridge      the version's published job on the chosen release, with today's program registry, the claim year's allocation version and
                cost-test set, on the FILED ACC (only when the restated ACC differs from it)
    restated    the same, on the chosen ACC (records named in an uploaded ClaimID / ACC file on their own ACC)

    program and engine effect = bridge - filed      (the engine release, the registry's programs, allocation and cost tests)
    ACC effect                = restated - bridge   (the avoided costs alone: nothing else differs between the two runs)

The runs go through the submissions service (`submissions.restate`), which the Calculations module serves; they are ordinary analysis
runs of the requester's organization, never linked to the version, so nothing they do reaches the filing. A restatement can also be set
beside another claim year's confirmed claims: a year restated on the ACC the other year was filed on differs from it by what the programs
did (the working group's "how much better is this program doing than three, four years ago" question, 10/26)."""

import csv
import io
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry.models import Program
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from . import service
from .models import ClaimYear, Restatement, RestatementLine

ROLES = ("restated", "bridge")
MAX_OVERRIDE_BYTES = 20 * 1024 * 1024
# The figures compared, as (key, label, kind): a ratio is its benefits over its cost (service.RATIOS).
FIGURES = [
    ("TotalSystemBenefit", "Total system benefit", "money"),
    ("TRCBen", "TRC benefits", "money"),
    ("TRCCost", "TRC cost", "money"),
    ("TRC", "TRC ratio", "ratio"),
    ("PACBen", "PAC benefits", "money"),
    ("PACCost", "PAC cost", "money"),
    ("PAC", "PAC ratio", "ratio"),
    ("ElecBen", "Electric benefits", "money"),
    ("GasBen", "Gas benefits", "money"),
    ("FirstYearNetKWh", "First-year net kWh", "number"),
    ("FirstYearNetThm", "First-year net therms", "number"),
]
FIGURE_KEYS = [k for k, _, _ in FIGURES]


# --- who may do what


def organisations_for(user) -> list:
    """The organizations a person may start a restatement for: those where they may start calculation runs (calculations.submit). Anyone
    signed in may also restate as their own analysis (no organization), as on New run."""
    return [o for o in access.organisations_of(user) if access.has_permission(user, o, "calculations.submit")]


def can_read(user, rs: Restatement) -> bool:
    """As for its runs: the owner of their own, the organization's calculation readers and submitters, a CPUC system administrator."""
    if not user.is_authenticated:
        return False
    if access.is_system_admin(user):
        return True
    if rs.organisation is None:
        return rs.owner_id == user.pk
    return any(access.has_permission(user, rs.organisation, f"calculations.{a}") for a in ("read", "submit"))


def visible(user, version: SubmissionVersion | None = None):
    """The restatements a person sees (of one version when given)."""
    qs = Restatement.objects.select_related("version__submission__deadline__period", "version__submission__organisation", "organisation")
    if version is not None:
        qs = qs.filter(version=version)
    if not user.is_authenticated:
        return qs.none()
    if access.is_system_admin(user):
        return qs
    orgs = [
        o for o in access.organisations_of(user) if any(access.has_permission(user, o, f"calculations.{a}") for a in ("read", "submit"))
    ]
    from django.db.models import Q

    return qs.filter(Q(organisation__isnull=True, owner=user) | Q(organisation__in=orgs))


def restatable(v: SubmissionVersion) -> str:
    """Why the version cannot be restated ("" when it can): it must be confirmed (published), priced on this platform, with its job kept."""
    if v.status != SubmissionVersion.Status.PUBLISHED:
        return "Only confirmed claims are restated. A version in progress is priced for confirmation on its period page."
    if v.imported:
        return "These claims were imported from legacy CEDARS as its published outputs: there are no measure inputs to price again."
    calc = v.calculation or {}
    if not (calc.get("job_key") or (v.findings or {}).get("job_key")):
        return "This version has no priced job of measures to price again."
    if not calc.get("acc"):
        return "This version's calculation does not say which ACC it was priced on, so a change cannot be split by its cause."
    return ""


# --- the job and the ClaimID / ACC file


def _job_bytes(v: SubmissionVersion) -> bytes:
    calc = v.calculation or {}
    return storage.store("uploads").get_bytes(calc.get("job_key") or v.findings["job_key"])


def _read_measure(job: bytes) -> tuple[list[str], list[list[str]], dict[str, bytes]]:
    """The job's Measure.csv (header, rows) and every other file of the zip as it is."""
    with zipfile.ZipFile(io.BytesIO(job)) as z:
        others = {n: z.read(n) for n in z.namelist() if n != "Measure.csv"}
        text = z.read("Measure.csv").decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:], others


def claim_ids(job: bytes) -> set[str]:
    header, rows, _ = _read_measure(job)
    i = header.index("CEInputID")
    return {r[i] for r in rows if len(r) > i}


def parse_overrides(data: bytes, claims: set[str], accs: list[str]) -> dict[str, str]:
    """{ClaimID: ACC} from a CSV with a ClaimID (or CEInputID) column and an ACC (or ACCVersion) column, comma- or pipe-delimited. Every
    ClaimID must be one of the version's priced claims and every ACC one the release has ("2024" or "2024 ACC"). AccessDenied names
    the first few problems."""
    if len(data) > MAX_OVERRIDE_BYTES:
        raise AccessDenied(f"The ClaimID and ACC file is over {MAX_OVERRIDE_BYTES // (1024 * 1024)} MB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AccessDenied("The ClaimID and ACC file is not a UTF-8 CSV file.") from None
    first = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text), delimiter="|" if first.count("|") > first.count(",") else ",")
    header = [h.strip().lower() for h in next(reader, [])]
    claim_col = next((header.index(n) for n in ("claimid", "ceinputid") if n in header), None)
    acc_col = next((header.index(n) for n in ("acc", "accversion", "acc_version") if n in header), None)
    if claim_col is None or acc_col is None:
        raise AccessDenied("The ClaimID and ACC file needs a ClaimID column and an ACC column (a header row naming them).")
    out, problems = {}, []
    for line, r in enumerate(reader, start=2):
        if not any(c.strip() for c in r):
            continue
        cid = r[claim_col].strip() if len(r) > claim_col else ""
        acc = (r[acc_col].strip() if len(r) > acc_col else "").upper().removesuffix("ACC").strip()
        if cid not in claims:
            problems.append(f"line {line}: {cid or '(blank)'} is not a priced claim of this version")
        elif acc not in accs:
            problems.append(f"line {line}: {acc or '(blank)'} is not an ACC this release has ({', '.join(accs)})")
        elif out.get(cid, acc) != acc:
            problems.append(f"line {line}: {cid} is given two ACCs")
        else:
            out[cid] = acc
    if problems:
        more = f" (and {len(problems) - 5:,} more)" if len(problems) > 5 else ""
        raise AccessDenied("The ClaimID and ACC file has problems: " + "; ".join(problems[:5]) + more + ".")
    if not out:
        raise AccessDenied("The ClaimID and ACC file names no claims.")
    return out


def with_record_accs(job: bytes, overrides: dict[str, str]) -> bytes:
    """The job with each named claim's measures given their own ACC in ReCET's ACCVersion column (BR-06.5; blank = the run's ACC). The
    other files and every other value are kept as they are."""
    header, rows, others = _read_measure(job)
    i = header.index("CEInputID")
    if "ACCVersion" not in header:
        header = [*header, "ACCVersion"]
        rows = [[*r, ""] for r in rows]
    a = header.index("ACCVersion")
    for r in rows:
        if r[i] in overrides:
            r[a] = overrides[r[i]]
    text = io.StringIO()
    w = csv.writer(text, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Measure.csv", text.getvalue().encode("utf-8"))
        for name, data in others.items():
            z.writestr(name, data)
    return out.getvalue()


# --- starting one


def start(
    actor,
    v: SubmissionVersion,
    *,
    organisation,
    release: str,
    acc_version: str,
    name: str = "",
    reason: str = "",
    overrides: bytes | None = None,
) -> Restatement:
    """Restate a confirmed version: one analysis run on `release` and `acc_version` (with `overrides`, a ClaimID / ACC file, those
    claims on their own ACC), and when any record's ACC differs from the filed one, a second on the filed ACC to split the change. Both
    use today's program registry and the claim year's allocation version and cost-test set, as a pricing of the version would today."""
    import tempfile
    from pathlib import Path

    why = restatable(v)
    if why:
        raise AccessDenied(why)
    if not service.can_read(actor, v):
        raise AccessDenied("You cannot read these claims.")
    if organisation is not None and organisation not in organisations_for(actor):
        raise AccessDenied(f"You need calculations.submit in {organisation.code} to start runs for it.")
    option = next((o for o in submissions.restate_options() if o["release"] == release), None)
    if option is None:
        raise AccessDenied(f"ReCET {release} is not an active release with ReCET's own calculation.")
    if acc_version not in option["accs"]:
        raise AccessDenied(f"ReCET {release} has no {acc_version} ACC; it has {', '.join(option['accs'])}.")
    filed_acc = str(v.calculation["acc"])
    job = _job_bytes(v)
    by_claim = parse_overrides(overrides, claim_ids(job), option["accs"]) if overrides else {}
    header, rows, _ = _read_measure(job)
    i = header.index("CEInputID")
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[by_claim.get(r[i], acc_version)] += 1
    acc_changes = set(counts) != {filed_acc}
    if acc_changes and filed_acc not in option["accs"]:
        raise AccessDenied(
            f"ReCET {release} has no {filed_acc} ACC, which these claims were filed on, so the ACC's effect could not be told apart "
            "from the engine's. Choose a release that has it."
        )
    cy = service.claim_year(v.submission.deadline)
    statewide = Program.objects.filter(prg_id__in=v.claim_lines.values("prg_id"), statewide=True).exists()
    settings = {"allocate_statewide": statewide, "acc_purpose": "claims"}  # as service.price: ReCET flags records off the year's ACC
    label = f"{v.submission.organisation.code} {service.label(v.submission.deadline)}, version {v.number}"
    name = (name or "").strip() or f"{label} restated on ReCET {release}, {acc_version} ACC{' and others' if by_claim else ''}"
    kw = {"organisation": organisation, "release": release, "allocation_version": cy.allocation_version, "settings": settings,
          "cost_test_set": cy.cost_test_set}  # fmt: skip
    with transaction.atomic(), tempfile.TemporaryDirectory() as tmp:
        rs = Restatement(
            version=v,
            organisation=organisation,
            owner=actor,
            name=name[:200],
            reason=reason.strip(),
            release=release,
            acc_version=acc_version,
            acc_records=dict(sorted(counts.items())),
            filed=_filed(v),
        )
        restated = Path(tmp) / "restated.zip"
        restated.write_bytes(with_record_accs(job, by_claim) if by_claim else job)
        if by_claim:
            rs.overrides_key = f"restatements/{uuid.uuid4()}/acc_by_claim.csv"
            storage.store("uploads").put_bytes(rs.overrides_key, overrides)
        runs = {
            "restated": submissions.restate(
                actor, v, restated, name=name[:200], acc_version=acc_version, record_accs=sorted(set(by_claim.values())), **kw
            )
        }
        if acc_changes:
            filed = Path(tmp) / "filed.zip"
            filed.write_bytes(job)
            runs["bridge"] = submissions.restate(
                actor, v, filed, name=f"{name[:160]} (bridge: {filed_acc} ACC)", acc_version=filed_acc, record_accs=[], **kw
            )
        rs.runs = runs
        rs.save()
        audit.record(
            "claims.restatement_started",
            obj=rs,
            organisation=organisation,
            after={
                "version": v.pk,
                "release": release,
                "acc": acc_version,
                "acc_records": rs.acc_records,
                "runs": runs,
                "reason": rs.reason,
            },
            actor=actor,
        )
    return rs


def _filed(v: SubmissionVersion) -> dict:
    calc = v.calculation or {}
    return {
        "run": calc.get("run", ""),
        "release": calc.get("release", ""),
        "acc": str(calc.get("acc", "")),
        "allocation": (calc.get("allocation_version") or {}).get("label", ""),
        "cost_tests": calc.get("cost_tests") or {},
        "programs_sha256": calc.get("programs_sha256") or "",
    }


# --- its runs and their results


def runs(rs: Restatement) -> dict[str, dict | None]:
    """{role: what the Calculations module says about the run (None: gone)} for the restatement's runs."""
    return {role: submissions.restated_run(rs.runs[role]) for role in ROLES if rs.runs.get(role)}


def state(rs: Restatement, described: dict | None = None) -> str:
    """ "running", "done", "failed" (a run ended without results, or is gone before its results were read)."""
    described = runs(rs) if described is None else described
    read = set(rs.lines.values_list("role", flat=True).distinct())
    out = "done"
    for role, d in described.items():
        if role in read:
            continue
        if d is None or (d["ended"] and not d["succeeded"]):
            return "failed"
        out = "running"
    return out


def import_lines(rs: Restatement, described: dict | None = None) -> None:
    """Read each finished run's group sums into RestatementLines, once (as service.import_results does for a filed run)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    described = runs(rs) if described is None else described
    read = set(rs.lines.values_list("role", flat=True).distinct())
    for role, d in described.items():
        key = ((d or {}).get("results") or {}).get("group_sums")
        if role in read or not d or not d["succeeded"] or not key:
            continue
        table = pq.read_table(pa.BufferReader(storage.store("results").get_bytes(key)))
        sums: dict[tuple, dict] = {}
        for row in table.to_pylist():
            k = (str(row.get("_pa") or ""), str(row.get("_prg") or ""))
            s = sums.setdefault(k, {"name": "", "n": 0, "values": defaultdict(float)})
            s["name"] = s["name"] or str(row.get("_prgname") or "")
            s["n"] += int(row.get("_n") or 0)
            for c, val in row.items():
                if c not in service.RESULT_KEYS and isinstance(val, int | float) and val == val:
                    s["values"][c] += float(val)
        with transaction.atomic():
            RestatementLine.objects.bulk_create(
                RestatementLine(
                    restatement=rs, role=role, pa=k[0], prg_id=k[1], program_name=s["name"], measures=s["n"], values=dict(s["values"])
                )
                for k, s in sums.items()
            )


def refresh(rs: Restatement) -> tuple[dict, str]:
    """Read what has finished; (the runs as described, the state)."""
    described = runs(rs)
    import_lines(rs, described)
    return described, state(rs, described)


# --- the comparison


def figure(sums: dict | None, key: str):
    """One figure from summed results: a ratio from its benefits and cost (None when the cost is 0), else the sum (None: not priced)."""
    if sums is None:
        return None
    if key in service.RATIOS:
        ben, cost = (sums.get(c) for c in service.RATIOS[key])
        return ben / cost if ben is not None and cost else None
    return sums.get(key)


def _less(a, b):
    return None if a is None or b is None else a - b


@dataclass
class ProgramFigures:
    prg_id: str
    name: str = ""
    filed: dict | None = None
    bridge: dict | None = None
    restated: dict | None = None

    def values(self, key: str) -> dict:
        f, b, r = (figure(x, key) for x in (self.filed, self.bridge, self.restated))
        return {"filed": f, "bridge": b, "restated": r, "engine": _less(b, f), "acc": _less(r, b), "total": _less(r, f)}


@dataclass
class Comparison:
    programs: list[ProgramFigures]
    total: ProgramFigures
    pas: list[str] = field(default_factory=list)
    acc_changes: bool = False


def _sum_into(target: dict, values: dict) -> None:
    for k, val in values.items():
        target[k] = target.get(k, 0.0) + val


def comparison(rs: Restatement, *, pa: str = "") -> Comparison:
    """Each program's figures as filed (the version's own results), on the current engine at the filed ACC (bridge) and restated, with
    the change split into its causes; summed over the credited PAs, or one. Without a bridge (the ACC did not change) bridge = restated."""
    t = service.table(rs.version, pa=pa)
    progs: dict[str, ProgramFigures] = {}
    for r in t.rows:
        if r.priced:
            progs[r.prg_id] = ProgramFigures(r.prg_id, r.name, filed=dict(r.sums))
    pas = set(t.pas)
    for ln in rs.lines.all():
        if ln.pa:
            pas.add(ln.pa)
        if pa and ln.pa != pa:
            continue
        p = progs.setdefault(ln.prg_id, ProgramFigures(ln.prg_id, ln.program_name))
        p.name = p.name or ln.program_name
        bucket = getattr(p, ln.role)
        if bucket is None:
            bucket = {}
            setattr(p, ln.role, bucket)
        _sum_into(bucket, ln.values)
    if not rs.acc_changes:
        for p in progs.values():
            p.bridge = p.restated
    shown = sorted(progs.values(), key=lambda p: p.prg_id)
    total = ProgramFigures("Total")
    for role in ("filed", "bridge", "restated"):
        parts = [getattr(p, role) for p in shown if getattr(p, role) is not None]
        if parts:
            s: dict = {}
            for part in parts:
                _sum_into(s, part)
            setattr(total, role, s)
    return Comparison(shown, total, sorted(pas), rs.acc_changes)


def other_years(rs: Restatement) -> list[tuple[int, SubmissionVersion]]:
    """The restated PA's latest confirmed claims of every other claim year priced on this platform, newest first: what a restatement
    can be set beside."""
    org, year = rs.version.submission.organisation, rs.version.submission.deadline.period.year
    out = []
    for y in ClaimYear.objects.exclude(year=year).values_list("year", flat=True):
        v, _, _ = service.year_standing(org, y)
        if v is not None and not v.imported and (v.calculation or {}).get("run"):
            out.append((y, v))
    return out


def year_comparison(rs: Restatement, other: SubmissionVersion, key: str, *, pa: str = "") -> list[dict]:
    """By program: this year restated and the other year as filed, and the difference. On the same ACC (and engine) that difference is
    what the programs did; the page says when the ACCs differ."""
    mine = comparison(rs, pa=pa)
    t = service.table(other, pa=pa)
    theirs = {r.prg_id: r for r in t.rows if r.priced}
    rows = []
    for p in mine.programs:
        o = theirs.pop(p.prg_id, None)
        a, b = figure(p.restated, key), figure(dict(o.sums) if o else None, key)
        rows.append({"prg_id": p.prg_id, "name": p.name, "restated": a, "other": b, "change": _less(b, a)})
    for o in theirs.values():
        b = figure(dict(o.sums), key)
        rows.append({"prg_id": o.prg_id, "name": o.name, "restated": None, "other": b, "change": None})
    rows.sort(key=lambda r: r["prg_id"])
    tot_other: dict = {}
    for r in t.rows:
        if r.priced:
            _sum_into(tot_other, r.sums)
    a, b = figure(mine.total.restated, key), figure(tot_other or None, key)
    rows.append({"prg_id": "Total", "name": "", "restated": a, "other": b, "change": _less(b, a), "total": True})
    return rows


def comparison_csv(rs: Restatement, *, pa: str = "") -> str:
    """Every program and figure: filed, bridge, restated and the three changes."""
    c = comparison(rs, pa=pa)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["PrgID", "ProgramName", "Figure", "Filed", "CurrentEngineFiledACC", "Restated", "ProgramAndEngineEffect", "ACCEffect",
                "TotalChange"])  # fmt: skip
    for p in [*c.programs, c.total]:
        for key, _, _ in FIGURES:
            v = p.values(key)
            cols = ("filed", "bridge", "restated", "engine", "acc", "total")
            w.writerow([p.prg_id, p.name, key] + ["" if v[k] is None else v[k] for k in cols])
    return out.getvalue()


def describe_filed_vs_restated(rs: Restatement, described: dict) -> list[tuple[str, str, str, bool]]:
    """(what, as filed, as restated, changed) for each input of the calculation, so the page says what the program and engine effect
    is made of."""
    d = (described.get("restated") or {}) if described else {}
    f = rs.filed or {}

    def tests(ct):
        return f"{ct.get('id', '')} {ct.get('version', '')}".strip() if ct else "The engine's own default set"

    rows = [
        ("ReCET release", f.get("release") or "Not recorded", rs.release),
        ("Program registry", "As it stood when the claims were priced", "Today's program registry for the program year"),
        ("Statewide allocation", f.get("allocation") or "Not allocated", (d.get("allocation") or "Not allocated") if d else ""),
        ("Cost-test set", tests(f.get("cost_tests")), tests(d.get("cost_tests")) if d else ""),
        ("ACC", f"{f.get('acc')} ACC", ", ".join(f"{a} ACC ({n:,} records)" for a, n in rs.acc_records.items())),
    ]
    out = []
    for what, a, b in rows:
        if what == "Program registry":
            same = bool(d) and bool(f.get("programs_sha256")) and f.get("programs_sha256") == d.get("programs_sha256")
            out.append((what, a, "The same program table as filed" if same else b, not same))
        elif what == "ACC":
            out.append((what, a, b, set(rs.acc_records) != {f.get("acc")}))
        else:
            out.append((what, a, b, bool(b) and a != b))
    return out
