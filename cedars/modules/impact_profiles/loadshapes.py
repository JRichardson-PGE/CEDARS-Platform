"""Deemed load-shape uploads: the file, the four validation stages and saving the shapes.

The file (Load Shapes user guide): a ZIP holding one CSV, or the CSV itself, with the columns Sector, BldgType, BldgVint, BldgHVAC,
BldgLoc, NormUnit, Type, Source Year, TechGroup, TechType, TechID, Hour of Year and UECproportion; one or more unitized 8,760 series,
each identified by Sector, BldgType, BldgVint, BldgHVAC, BldgLoc, TechGroup, TechType and TechID.

The stages, each a set of rules with a stable ID, a message and a fix (RULES), reported in the rule library's findings format:
  F  file: a ZIP or CSV, not over the size limit, one CSV in the ZIP, readable, not empty;
  T  table: every required header present, none extra or repeated;
  S  series: hours 1 to 8,760 in order with no gaps, repeats or extra hours (no leap day), numeric proportions summing to 1 within
     0.01%, one NormUnit / Type / Source Year per series, an explicit BldgType (not Any: Com or Res stand in for it) and a climate-zone
     BldgLoc (with or without the CZ prefix);
  E  eTRM: each series matches a combination the package's permutations allow, on every field, with "Any" in a permutation matching
     any explicit value.
A file failing F or T stops there. By default any rejecting finding rejects the whole file and nothing is saved (CEDARS' rule); an
upload made with `accept_partial` keeps the series that passed every stage and lists the others.
"""

import csv
import hashlib
import io
import re
import uuid
import zipfile
from array import array
from dataclasses import dataclass, field

import numpy as np
from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from cedars.services import audit

from . import access, shapes
from .models import HOURS, LoadShape, LoadShapeUpload, LoadShapeVersion, MeasurePackage, ProcessingJob, UploadFinding

COLUMNS = [
    "Sector",
    "BldgType",
    "BldgVint",
    "BldgHVAC",
    "BldgLoc",
    "NormUnit",
    "Type",
    "Source Year",
    "TechGroup",
    "TechType",
    "TechID",
    "Hour of Year",
    "UECproportion",
]
KEY = ["Sector", "BldgType", "BldgVint", "BldgHVAC", "BldgLoc", "TechGroup", "TechType", "TechID"]
KEY_FIELDS = ["sector", "bldg_type", "bldg_vint", "bldg_hvac", "bldg_loc", "tech_group", "tech_type", "tech_id"]
SUM_TOLERANCE = 1e-4  # 0.01%
MAX_ROW_FINDINGS = 20  # per rule and series; the rest are counted in one more finding
STAGES = {"F": "File", "T": "Table headers", "S": "8,760 series", "E": "eTRM alignment"}

# rule id: (stage, message, fix). Every rule rejects; warnings would be ("warning", ...) and none exist yet.
RULES = {
    "LS-F01": (
        "F",
        "The file must be a ZIP holding one CSV, or a CSV.",
        "Save the table as CSV (comma-separated) and upload it, zipped or not.",
    ),
    "LS-F02": ("F", "The file is larger than the upload limit.", "Split the shapes into several files and upload them one after another."),
    "LS-F03": ("F", "The ZIP must hold exactly one CSV.", "Put one CSV in each ZIP; upload several ZIPs for several CSVs."),
    "LS-F04": (
        "F",
        "The file cannot be opened and read.",
        "Check the file opens in a spreadsheet, save it as CSV (UTF-8) and upload it again.",
    ),
    "LS-F05": ("F", "The file has no data rows.", "Export the 8,760 rows with the header row and upload again."),
    "LS-T01": ("T", "Required columns are missing.", "Add the missing columns with exactly these headers, as the template has them."),
    "LS-T02": ("T", "The file has columns that are not in the load-shape layout.", "Remove or rename the extra columns."),
    "LS-T03": ("T", "A column header appears more than once.", "Keep one column per header."),
    "LS-S01": ("S", "Hour of Year must be a whole number.", "Write Hour of Year as 1 to 8760, with no decimals or text."),
    "LS-S02": (
        "S",
        "Hours must run 1 to 8,760 in order, with no gaps, repeats or extra hours.",
        "Give each series exactly one row per hour, 1 to 8760, in order. Do not include a leap day.",
    ),
    "LS-S03": ("S", "UECproportion must be a number.", "Write UECproportion as a plain decimal number."),
    "LS-S04": (
        "S",
        "The series' UECproportion values must add up to 1 (within 0.01%).",
        "Unitize the series: divide each hour by the series' annual total.",
    ),
    "LS-S05": (
        "S",
        "NormUnit, Type and Source Year must be the same in every row of a series.",
        "Use one value for each in the whole series.",
    ),
    "LS-S06": ("S", "Source Year must be a four-digit year.", "Enter the calendar year the shape is drawn on, usually 2009."),
    "LS-S07": (
        "S",
        'BldgType cannot be "Any" in an upload.',
        'Use the explicit building type; for an "Any" permutation use Com (nonresidential) or Res (residential), as CPUC guidance does.',
    ),
    "LS-S08": (
        "S",
        "BldgLoc must be a climate zone number, 1 to 16.",
        "Write the climate zone as 1 to 16, with or without CZ (for example 12 or CZ12).",
    ),
    "LS-S09": (
        "S",
        "A field that identifies the series is blank.",
        "Fill in Sector, BldgVint, BldgHVAC, TechGroup, TechType and TechID on every row.",
    ),
    "LS-E01": (
        "E",
        "The series does not match any combination the measure package's permutations allow.",
        "Check the series' Sector, building fields, TechGroup, TechType and TechID against the package's permutations in eTRM.",
    ),
    "LS-E02": (
        "E",
        "The series uses a value the measure package does not have.",
        "Correct the value, or add the permutation in eTRM and wait for the next nightly sync.",
    ),
}


@dataclass
class Finding:
    """The rule library's finding layout (row = the file line, header = 1; record_id = the series), plus the stage."""

    rule_id: str
    message: str = ""
    row: int | None = None
    record_id: str = ""
    field: str = ""
    value: str = ""
    severity: str = "reject"
    stage: str = ""
    fix: str = ""

    def __post_init__(self):
        stage, message, fix = RULES[self.rule_id]
        self.stage = self.stage or stage
        self.message = self.message or message
        self.fix = self.fix or fix


@dataclass
class Series:
    key: tuple
    first_row: int
    norm_unit: str = ""
    shape_type: str = ""
    source_year: str = ""
    hours: array = field(default_factory=lambda: array("l"))
    values: array = field(default_factory=lambda: array("d"))
    findings: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return "-".join(self.key)

    def add(self, finding: Finding) -> None:
        n = self.counts.get(finding.rule_id, 0) + 1
        self.counts[finding.rule_id] = n
        if n <= MAX_ROW_FINDINGS:
            self.findings.append(finding)


@dataclass
class CheckResult:
    stages: dict
    findings: list
    series: list
    passed: list  # the series with no rejecting finding

    @property
    def rejected(self) -> bool:
        return any(f.severity == "reject" for f in self.findings)


def zone(value: str) -> str:
    """A climate zone as the catalog writes it: "CZ03", "cz3" and "3" are "3"; "Any" stays "Any"."""
    v = value.strip()
    m = re.fullmatch(r"(?i)(?:cz)?0*(\d+[AB]?)", v)
    return m.group(1).upper() if m else v


def _header_key(h: str) -> str:
    return re.sub(r"[\s_]", "", h).lower()


# --- reading the file


def open_csv(f, name: str, size: int) -> tuple[io.TextIOBase | None, list[Finding]]:
    max_bytes = settings.LOAD_SHAPE_MAX_UPLOAD_MB * 1024 * 1024
    if size > max_bytes:
        return None, [
            Finding(
                "LS-F02",
                value=f"{size / 1024 / 1024:.1f} MB",
                message=f"The file is {size / 1024 / 1024:.1f} MB; the limit is {settings.LOAD_SHAPE_MAX_UPLOAD_MB} MB.",
            )
        ]
    lower = name.lower()
    raw = f
    if lower.endswith(".zip"):
        try:
            z = zipfile.ZipFile(f)
            members = [
                i
                for i in z.infolist()
                if not i.is_dir() and not i.filename.startswith("__MACOSX/") and not i.filename.rsplit("/", 1)[-1].startswith(".")
            ]
        except zipfile.BadZipFile:
            return None, [Finding("LS-F04", message="The ZIP cannot be opened.")]
        csvs = [i for i in members if i.filename.lower().endswith((".csv", ".txt"))]
        if len(csvs) != 1 or len(members) != 1:
            names = ", ".join(i.filename for i in members) or "nothing"
            return None, [Finding("LS-F03", value=names[:200], message=f"The ZIP must hold exactly one CSV; it holds {names[:200]}.")]
        if csvs[0].file_size > settings.LOAD_SHAPE_MAX_UNZIPPED_MB * 1024 * 1024:
            return None, [Finding("LS-F02", message=f"The CSV inside the ZIP is over {settings.LOAD_SHAPE_MAX_UNZIPPED_MB} MB.")]
        raw = z.open(csvs[0])
    elif not lower.endswith((".csv", ".txt")):
        return None, [Finding("LS-F01", value=name[-40:])]
    return io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""), []


def check_file(f, name: str, size: int, permutations) -> CheckResult:
    """Run the four stages on an open binary file against a package version's permutations. Never raises on bad content."""
    stages = dict.fromkeys(STAGES, "not run")
    text, findings = open_csv(f, name, size)
    if text is None:
        stages["F"] = "failed"
        return CheckResult(stages, findings, [], [])
    try:
        reader = csv.reader(text)
        header = next(reader, None)
        if header is None:
            stages["F"] = "failed"
            return CheckResult(stages, [Finding("LS-F05")], [], [])
        stages["F"] = "passed"
        cols, tfind = check_header(header)
        if tfind:
            stages["T"] = "failed"
            return CheckResult(stages, tfind, [], [])
        stages["T"] = "passed"
        series, sfind = _read_series(reader, cols)
    except (UnicodeDecodeError, csv.Error) as e:
        stages["F"], stages["T"] = "failed", "not run"
        return CheckResult(stages, [Finding("LS-F04", message=f"The file cannot be read: {e}")], [], [])
    if not series and not sfind:
        stages["F"], stages["T"] = "failed", "not run"
        return CheckResult(stages, [Finding("LS-F05")], [], [])
    for s in series:
        _check_series(s)
    stages["S"] = "failed" if sfind or any(s.findings for s in series) else "passed"
    allowed = _allowed(permutations)
    for s in series:
        if not s.findings:
            _check_etrm(s, allowed)
    stages["E"] = "failed" if any(f.stage == "E" for s in series for f in s.findings) else "passed"
    findings = sfind + [f for s in series for f in s.findings]
    for s in series:
        for rule_id, n in s.counts.items():
            if n > MAX_ROW_FINDINGS:
                findings.append(
                    Finding(rule_id, record_id=s.label, message=f"{n - MAX_ROW_FINDINGS} more rows of this series break this rule.")
                )
    passed = [s for s in series if not s.findings]
    return CheckResult(stages, findings, series, passed)


def check_header(header: list[str], columns: list[str] = COLUMNS) -> tuple[dict, list[Finding]]:
    canonical = {_header_key(c): c for c in columns}
    seen, findings = {}, []
    for i, h in enumerate(header):
        name = canonical.get(_header_key(h))
        if name is None:
            findings.append(Finding("LS-T02", row=1, field=h[:60], value=h[:60], message=f"{h!r} is not a load-shape column."))
        elif name in seen:
            findings.append(Finding("LS-T03", row=1, field=name, message=f"{name} appears more than once."))
        else:
            seen[name] = i
    missing = [c for c in columns if c not in seen]
    if missing:
        findings.insert(0, Finding("LS-T01", row=1, field=", ".join(missing), message="Missing columns: " + ", ".join(missing) + "."))
    return seen, findings


def _read_series(reader, cols: dict) -> tuple[list[Series], list[Finding]]:
    idx = [cols[c] for c in KEY]
    i_unit, i_type, i_year = cols["NormUnit"], cols["Type"], cols["Source Year"]
    i_hour, i_value = cols["Hour of Year"], cols["UECproportion"]
    width = max(cols.values()) + 1
    by_key: dict[tuple, Series] = {}
    findings: list[Finding] = []
    for line, row in enumerate(reader, start=2):
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) < width:
            row = row + [""] * (width - len(row))
        key = tuple(row[i].strip() for i in idx)
        key = key[:4] + (zone(key[4]),) + key[5:]
        s = by_key.get(key)
        if s is None:
            s = by_key[key] = Series(key, line, row[i_unit].strip(), row[i_type].strip(), row[i_year].strip())
        elif (row[i_unit].strip(), row[i_type].strip(), row[i_year].strip()) != (s.norm_unit, s.shape_type, s.source_year):
            s.add(Finding("LS-S05", row=line, record_id=s.label, field="NormUnit / Type / Source Year"))
        hour, value = row[i_hour].strip(), row[i_value].strip()
        try:
            s.hours.append(int(hour))
        except ValueError:
            s.add(Finding("LS-S01", row=line, record_id=s.label, field="Hour of Year", value=hour[:40]))
            s.hours.append(-1)
        try:
            v = float(value)
            if not np.isfinite(v):
                raise ValueError
            s.values.append(v)
        except ValueError:
            s.add(Finding("LS-S03", row=line, record_id=s.label, field="UECproportion", value=value[:40]))
            s.values.append(0.0)
    return list(by_key.values()), findings


def _check_series(s: Series) -> None:
    sector, bldg_type, bldg_vint, bldg_hvac, bldg_loc, tech_group, tech_type, tech_id = s.key
    blank = [name for name, v in zip(KEY, s.key, strict=True) if not v and name not in ("BldgType", "BldgLoc")]
    if blank:
        s.add(Finding("LS-S09", row=s.first_row, record_id=s.label, field=", ".join(blank)))
    if bldg_type.lower() == "any" or not bldg_type:
        s.add(Finding("LS-S07", row=s.first_row, record_id=s.label, field="BldgType", value=bldg_type))
    if not re.fullmatch(r"([1-9]|1[0-6])", bldg_loc):
        s.add(Finding("LS-S08", row=s.first_row, record_id=s.label, field="BldgLoc", value=bldg_loc[:20]))
    if not re.fullmatch(r"(19|20)\d\d", s.source_year):
        s.add(Finding("LS-S06", row=s.first_row, record_id=s.label, field="Source Year", value=s.source_year[:20]))
    hours = np.frombuffer(s.hours, dtype=np.int_) if len(s.hours) else np.array([], dtype=np.int_)
    if "LS-S01" not in s.counts:
        problem = hour_problem(hours)
        if problem:
            s.add(Finding("LS-S02", row=s.first_row, record_id=s.label, field="Hour of Year", message=problem))
    if "LS-S03" not in s.counts and len(s.values) == HOURS:
        total = float(np.sum(np.frombuffer(s.values, dtype=float)))
        if abs(total - 1) > SUM_TOLERANCE:
            s.add(
                Finding(
                    "LS-S04",
                    row=s.first_row,
                    record_id=s.label,
                    field="UECproportion",
                    value=f"{total:.6f}",
                    message=f"The series adds up to {total:.6f}, not 1.",
                )
            )


def hour_problem(hours: np.ndarray) -> str:
    if len(hours) == 0:
        return "The series has no rows."
    expected = np.arange(1, HOURS + 1)
    if len(hours) == HOURS and np.array_equal(hours, expected):
        return ""
    if len(hours) > HOURS:
        extra = "; a leap day adds 24 hours" if len(hours) == HOURS + 24 else ""
        return f"The series has {len(hours):,} rows, not 8,760{extra}."
    uniq, counts = np.unique(hours, return_counts=True)
    if (counts > 1).any():
        return f"Hour {int(uniq[counts > 1][0])} appears more than once."
    outside = hours[(hours < 1) | (hours > HOURS)]
    if len(outside):
        return f"Hour {int(outside[0])} is outside 1 to 8760."
    if len(hours) < HOURS:
        missing = np.setdiff1d(expected, hours)
        return f"{len(missing):,} hours are missing, the first is hour {int(missing[0])}."
    first = int(np.nonzero(hours != expected)[0][0])
    return f"The hours are out of order from row {first + 1} of the series (hour {int(hours[first])})."


# --- eTRM alignment


def _allowed(permutations) -> dict:
    """The package's allowed values per field and its combinations, from its permutations."""
    combos, values = [], {f: set() for f in KEY_FIELDS}
    for p in permutations:
        techs = {t for t in (p.measure_tech_id, p.standard_tech_id, p.pre_existing_tech_id) if t}
        loc = zone(p.bldg_loc)
        combos.append((p.sector, p.bldg_type, p.bldg_vint, p.bldg_hvac, loc, p.tech_group, p.tech_type, frozenset(techs)))
        for f, v in zip(KEY_FIELDS[:-1], (p.sector, p.bldg_type, p.bldg_vint, p.bldg_hvac, loc, p.tech_group, p.tech_type), strict=True):
            values[f].add(v)
        values["tech_id"] |= techs
    return {"combos": combos, "values": values}


def _matches(perm_value: str, upload_value: str) -> bool:
    return perm_value == upload_value or perm_value.lower() == "any"


def matches_permutation(key: tuple, combo: tuple) -> bool:
    sector, bldg_type, bldg_vint, bldg_hvac, bldg_loc, tech_group, tech_type, tech_id = key
    p_sector, p_type, p_vint, p_hvac, p_loc, p_group, p_ttype, p_techs = combo
    return (
        sector == p_sector
        and _matches(p_type, bldg_type)
        and bldg_vint == p_vint
        and _matches(p_hvac, bldg_hvac)
        and _matches(p_loc, bldg_loc)
        and tech_group == p_group
        and tech_type == p_ttype
        and tech_id in p_techs
    )


def _check_etrm(s: Series, allowed: dict) -> None:
    if any(matches_permutation(s.key, c) for c in allowed["combos"]):
        return
    values = allowed["values"]
    for name, f, v in zip(KEY, KEY_FIELDS, s.key, strict=True):
        ok = v in values[f] or (f in ("bldg_type", "bldg_hvac", "bldg_loc") and any(x.lower() == "any" for x in values[f]))
        if not ok:
            s.add(
                Finding(
                    "LS-E02",
                    row=s.first_row,
                    record_id=s.label,
                    field=name,
                    value=v[:200],
                    message=f"{name} {v!r} is not in any of the package's permutations.",
                )
            )
            return
    s.add(Finding("LS-E01", row=s.first_row, record_id=s.label))


# --- uploads


def create_upload(actor, organisation, package: MeasurePackage, uploaded, *, accept_partial: bool = False) -> LoadShapeUpload:
    """Store an uploaded file (a Django UploadedFile or any binary file with .name and .size) and queue its check on the PA's queue."""
    access.require_package_lead(actor, organisation, package)
    if package.current is None:
        raise access.AccessDenied(f"{package.measure_version_id} has no permutations from eTRM yet.")
    name = (getattr(uploaded, "name", "") or "load_shapes.csv").rsplit("/", 1)[-1][:200]
    size = getattr(uploaded, "size", None) or 0
    limit = settings.LOAD_SHAPE_MAX_UPLOAD_MB * 1024 * 1024
    if size > limit:
        raise access.AccessDenied(
            f"That file is {size / 1024 / 1024:.1f} MB; the limit is {settings.LOAD_SHAPE_MAX_UPLOAD_MB} MB per file."
        )
    digest = hashlib.sha256()
    for chunk in uploaded.chunks() if hasattr(uploaded, "chunks") else iter(lambda: uploaded.read(1 << 20), b""):
        digest.update(chunk)
    uploaded.seek(0)
    stored = default_storage.save(f"impact_profiles/uploads/{uuid.uuid4().hex}/{name}", File(uploaded, name=name))
    with transaction.atomic():
        upload = LoadShapeUpload.objects.create(
            package=package,
            package_version=package.current,
            organisation=organisation,
            uploaded_by=actor,
            file_name=name,
            stored_name=stored,
            size=size,
            sha256=digest.hexdigest(),
            accept_partial=accept_partial,
        )
        audit.record(
            "load_shape_upload.created",
            obj=upload,
            after={"package": package.measure_version_id, "version": package.current.number, "file": name, "sha256": upload.sha256},
            actor=actor,
            organisation=organisation,
        )
        from . import jobs

        jobs.enqueue(ProcessingJob.Kind.CHECK, organisation.code, actor=actor, upload=upload)
    return upload


def run_check(upload: LoadShapeUpload) -> LoadShapeUpload:
    """The CHECK job: run the stages, save the findings and, if the file is accepted, its series as load-shape versions."""
    upload.status = LoadShapeUpload.Status.CHECKING
    upload.save(update_fields=["status"])
    with default_storage.open(upload.stored_name, "rb") as f:
        result = check_file(f, upload.file_name, upload.size, upload.package_version.permutations.all())
    with transaction.atomic():
        UploadFinding.objects.filter(upload=upload).delete()
        UploadFinding.objects.bulk_create(
            UploadFinding(
                upload=upload,
                stage=x.stage,
                rule_id=x.rule_id,
                severity=x.severity,
                row=x.row,
                record_id=x.record_id[:600],
                field=x.field[:60],
                value=x.value[:200],
                message=x.message,
                fix=x.fix,
            )
            for x in result.findings
        )
        upload.stages = result.stages
        upload.series_count = len(result.series)
        upload.checked_at = timezone.now()
        keep = result.passed if (upload.accept_partial or not result.rejected) else []
        if result.stages["F"] != "passed" or result.stages["T"] != "passed":
            keep = []
        counts = _save_series(upload, keep) if keep else {"new": 0, "replaced": 0, "unchanged": 0}
        upload.accepted_count = len(keep)
        upload.status = LoadShapeUpload.Status.ACCEPTED if keep else LoadShapeUpload.Status.REJECTED
        upload.save()
        audit.record(
            f"load_shape_upload.{upload.status}",
            obj=upload,
            after={"stages": result.stages, "series": len(result.series), "kept": len(keep), "findings": len(result.findings), **counts},
            actor=None,
            organisation=upload.organisation,
        )
    return upload


def _save_series(upload: LoadShapeUpload, series: list[Series]) -> dict:
    counts = {"new": 0, "replaced": 0, "unchanged": 0}
    now = timezone.now()
    for s in series:
        shape = shapes.store(np.frombuffer(s.values, dtype=float))
        ls, created = LoadShape.objects.get_or_create(package=upload.package, **dict(zip(KEY_FIELDS, s.key, strict=True)))
        current = None if created else ls.current_version()
        meta = (s.norm_unit, s.shape_type, int(s.source_year))
        if current is not None and current.shape_id == shape.pk and (current.norm_unit, current.shape_type, current.source_year) == meta:
            counts["unchanged"] += 1
            continue
        if current is not None:
            LoadShapeVersion.objects.filter(pk=current.pk).update(superseded_at=now)
            counts["replaced"] += 1
        else:
            counts["new"] += 1
        LoadShapeVersion.objects.create(
            load_shape=ls,
            number=(current.number + 1) if current else 1,
            upload=upload,
            shape=shape,
            norm_unit=s.norm_unit,
            shape_type=s.shape_type,
            source_year=int(s.source_year),
        )
    return counts


def template_csv() -> str:
    """A one-series example in the upload layout (a flat shape), so the headers are exactly right."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(COLUMNS)
    for h in range(1, HOURS + 1):
        w.writerow(
            [
                "Com",
                "OfL",
                "Ex",
                "rDXGF",
                "CZ12",
                "Area-1000SF",
                "Whole Building",
                2009,
                "Lighting",
                "LED",
                "Example-Msr",
                h,
                f"{1 / HOURS:.12g}",
            ]
        )
    return out.getvalue()
