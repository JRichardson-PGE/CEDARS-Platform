"""The nightly eTRM sync: draft measure packages and their permutations, pulled from eTRM's restricted PostgreSQL database (full-precision
UECs), into a versioned snapshot with a sync history.

As in CEDARS' Load Shape Tool, only draft packages (Measure Version ID ending in "-draft") starting on or after
settings.LOAD_SHAPE_FIRST_START (1 January 2027: earlier measure versions keep their DEER impact profile IDs) are taken, and a package
with bad data is excluded and listed with the reason instead of stopping the sync. A package whose content has not changed keeps its
version; a changed package gets a new one, so an upload can name the exact permutations it was checked against.

The database's real schema is not known yet (no access): DatabaseSource reads the fixture schema in `etrm_fixture_schema.sql`, and only
its two queries need to change when the credentials arrive. FileSource reads the same data from a JSON file, for development and tests,
and ExportSource reads the permutations CSV that eTRM's measure pages download (a stop-gap until the database: the column labels are
eTRM's own). The public eTRM API (specification of 11 March 2026) is no substitute: its /permutations endpoints return published
measure versions only, every value as a string, and name no draft data, so the drafts a load shape is uploaded against come only from
the database or a download."""

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cedars.services import audit

from . import access
from .models import EtrmSyncRun, ExcludedPackage, MeasurePackage, PackageVersion, Permutation

TEXT_FIELDS = [
    "sector",
    "bldg_type",
    "bldg_vint",
    "bldg_hvac",
    "bldg_loc",
    "tech_group",
    "tech_type",
    "measure_tech_id",
    "standard_tech_id",
    "pre_existing_tech_id",
    "standard_tech_group",
    "standard_tech_type",
    "pre_existing_tech_group",
    "pre_existing_tech_type",
    "norm_unit",
    "measure_application_type",
    "elec_target_sector",
    "electric_impact_profile_id",
]
NUMBER_FIELDS = ["uec_kwh_meas", "uec_kwh_base1", "uec_kwh_base2"]
REQUIRED = ["sector", "bldg_type", "bldg_vint", "bldg_hvac", "bldg_loc", "tech_group", "tech_type", "measure_tech_id"]
LABELS = {
    "sector": "Sector",
    "bldg_type": "BldgType",
    "bldg_vint": "BldgVint",
    "bldg_hvac": "BldgHVAC",
    "bldg_loc": "BldgLoc",
    "tech_group": "TechGroup",
    "tech_type": "TechType",
    "measure_tech_id": "MeasTechID",
    "uec_kwh_meas": "UECkWhMeas",
    "uec_kwh_base1": "UECkWhBase1",
    "uec_kwh_base2": "UECkWhBase2",
}


@dataclass
class PackageRecord:
    measure_version_id: str
    name: str
    pa_lead: str
    start_date: date | None
    statewide_id: str = ""
    permutations: list[dict] = field(default_factory=list)


class SourceError(RuntimeError):
    """The eTRM data could not be read at all (the sync fails and changes nothing)."""


# --- sources


def _date(value) -> date | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _record(header: dict, permutations: list[dict]) -> PackageRecord:
    mvid = str(header.get("measure_version_id") or "").strip()
    return PackageRecord(
        measure_version_id=mvid,
        statewide_id=str(header.get("statewide_id") or mvid.split("-")[0]).strip(),
        name=str(header.get("name") or "").strip(),
        pa_lead=str(header.get("pa_lead") or "").strip().upper(),
        start_date=_date(header.get("start_date")),
        permutations=permutations,
    )


class FileSource:
    """{"packages": [{measure_version_id, statewide_id, name, pa_lead, start_date, permutations: [{field: value}]}]}."""

    def __init__(self, path):
        self.path = str(path)
        self.label = f"fixture file {self.path.rsplit('/', 1)[-1]}"

    def packages(self) -> list[PackageRecord]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise SourceError(f"could not read {self.path}: {e}") from e
        return [_record(p, list(p.get("permutations") or [])) for p in data.get("packages", [])]


# eTRM's permutations download ("Exported from: https://www.caetrm.com/measure/<id>/<version>/permutations/", then one row per
# permutation under eTRM's labels; 123 columns in the 2026 sample). It has no electric target sector, so a profile made from it is
# targeted at the permutation's Sector.
EXPORT_PACKAGE_COLUMNS = {
    "measure_version_id": "Measure Version ID",
    "statewide_id": "Statewide Measure ID",
    "name": "Measure Name",
    "pa_lead": "PA Lead",
    "start_date": "Effective Start Date",
}
EXPORT_COLUMNS = {
    "sector": "Sector",
    "bldg_type": "Building Type",
    "bldg_vint": "Building Vintage",
    "bldg_hvac": "Building HVAC",
    "bldg_loc": "Building Location",
    "tech_group": "Technology Group",
    "tech_type": "Technology Type",
    "measure_tech_id": "Measure Technology ID",
    "standard_tech_id": "Standard Technology ID",
    "pre_existing_tech_id": "Pre-Existing Technology ID",
    "standard_tech_group": "Standard Technology Group",
    "standard_tech_type": "Standard Technology Type",
    "pre_existing_tech_group": "Pre-Existing Technology Group",
    "pre_existing_tech_type": "Pre-Existing Technology Type",
    "norm_unit": "Normalized Unit",
    "measure_application_type": "Measure Application Type",
    "electric_impact_profile_id": "Electric Impact Profile ID",
    "uec_kwh_meas": "Measure UEC kWh",
    "uec_kwh_base1": "First Baseline - UEC kWh",
    "uec_kwh_base2": "Second Baseline - UEC kWh",
}


class ExportSource:
    """One or more eTRM permutations downloads. Rows are grouped into packages by Measure Version ID; the package's name, PA lead
    and start date come from its first row."""

    def __init__(self, paths):
        self.paths = [str(p) for p in ([paths] if isinstance(paths, str | bytes) or not hasattr(paths, "__iter__") else paths)]
        self.label = "eTRM export " + ", ".join(p.rsplit("/", 1)[-1] for p in self.paths)

    def _rows(self, path: str):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise SourceError(f"could not read {path}: {e}") from e
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines[:5]) if "Measure Version ID" in line), None)
        name = path.rsplit("/", 1)[-1]
        if start is None:
            raise SourceError(f"{name} is not an eTRM permutations download: no Measure Version ID column in its first lines")
        reader = csv.DictReader(lines[start:])
        wanted = list(EXPORT_PACKAGE_COLUMNS.values()) + list(EXPORT_COLUMNS.values())
        missing = [c for c in wanted if c not in (reader.fieldnames or [])]
        if missing:
            raise SourceError(f"{name} has no column {', '.join(missing)}")
        yield from reader

    def packages(self) -> list[PackageRecord]:
        headers: dict[str, dict] = {}
        perms: dict[str, list[dict]] = {}
        for path in self.paths:
            for row in self._rows(path):
                mvid = (row[EXPORT_PACKAGE_COLUMNS["measure_version_id"]] or "").strip()
                if not mvid:
                    continue
                headers.setdefault(mvid, {k: row[c] for k, c in EXPORT_PACKAGE_COLUMNS.items()})
                perms.setdefault(mvid, []).append({k: row[c] for k, c in EXPORT_COLUMNS.items()})
        return [_record(h, perms[m]) for m, h in headers.items()]


PACKAGES_SQL = "SELECT measure_version_id, statewide_id, name, pa_lead, start_date FROM {schema}.measure_versions WHERE is_draft"
PERMUTATIONS_SQL = (
    "SELECT measure_version_id, " + ", ".join(TEXT_FIELDS + NUMBER_FIELDS) + " FROM {schema}.permutations ORDER BY measure_version_id, id"
)


class DatabaseSource:
    """The restricted eTRM PostgreSQL database, read with a read-only role (ETRM_DATABASE_URL), in the fixture schema's layout."""

    def __init__(self, url: str, schema: str = "etrm"):
        self.url, self.schema = url, schema
        self.label = f"eTRM database ({schema})"

    def packages(self) -> list[PackageRecord]:
        import psycopg
        from psycopg.rows import dict_row

        try:
            with psycopg.connect(self.url, row_factory=dict_row) as conn, conn.cursor() as cur:
                cur.execute(PACKAGES_SQL.format(schema=self.schema))
                headers = cur.fetchall()
                perms: dict[str, list[dict]] = {}
                with conn.cursor(name="etrm_permutations") as stream:  # server-side: a large package never sits in memory twice
                    stream.execute(PERMUTATIONS_SQL.format(schema=self.schema))
                    for row in stream:
                        perms.setdefault(row.pop("measure_version_id"), []).append(row)
        except psycopg.Error as e:
            raise SourceError(f"the eTRM database could not be read: {e}") from e
        return [_record(h, perms.get(h["measure_version_id"], [])) for h in headers]


def configured_source():
    if settings.ETRM_DATABASE_URL:
        return DatabaseSource(settings.ETRM_DATABASE_URL, settings.ETRM_SCHEMA)
    if settings.ETRM_FIXTURE:
        return FileSource(settings.ETRM_FIXTURE)
    raise SourceError("No eTRM source is configured: set ETRM_DATABASE_URL (or ETRM_FIXTURE for a fixture file).")


# --- checks


def _clean(perm: dict) -> dict:
    out = {k: ("" if perm.get(k) is None else str(perm.get(k)).strip()) for k in TEXT_FIELDS}
    for k in NUMBER_FIELDS:
        v = perm.get(k)
        out[k] = None if v in (None, "") else float(v)
    return out


def exclusion_reason(rec: PackageRecord, perms: list[dict]) -> str:
    """Why CEDARS would leave this package out, or "" (the reasons its sync history page lists)."""
    if not rec.pa_lead or not rec.name:
        return "Missing or blank required fields: " + ", ".join(n for n, v in (("PA lead", rec.pa_lead), ("name", rec.name)) if not v)
    if not perms:
        return "Missing permutations"
    texts = [rec.name, rec.pa_lead] + [p[k] for p in perms for k in TEXT_FIELDS]
    bad = next((t for t in texts if not t.isascii()), None)
    if bad is not None:
        return f"Permutations contain unexpected data: non-ASCII characters in {bad[:60]!r}"
    missing = sorted({LABELS[k] for p in perms for k in REQUIRED if not p[k]})
    if any(not (p["standard_tech_id"] or p["pre_existing_tech_id"]) for p in perms):
        missing.append("base case TechID")
    if any(p["uec_kwh_meas"] is None for p in perms):
        missing.append("UECkWhMeas")
    if any(p["uec_kwh_base1"] is None for p in perms):
        missing.append("UECkWhBase1")
    if missing:
        return "Missing or blank required fields: " + ", ".join(dict.fromkeys(missing))
    if any(p["uec_kwh_meas"] == 0 for p in perms):
        return "UECkWhMeas is zero"
    if any(p["uec_kwh_base1"] == 0 and not p["uec_kwh_base2"] for p in perms):
        return "Both UEC kWh baselines are zero"
    return ""


def content_hash(rec: PackageRecord, perms: list[dict]) -> str:
    body = {
        "name": rec.name,
        "pa_lead": rec.pa_lead,
        "start_date": rec.start_date.isoformat() if rec.start_date else None,
        "permutations": sorted(json.dumps(p, sort_keys=True) for p in perms),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def in_scope(rec: PackageRecord) -> bool:
    first = date.fromisoformat(settings.LOAD_SHAPE_FIRST_START)
    return rec.measure_version_id.lower().endswith("-draft") and rec.start_date is not None and rec.start_date >= first


# --- the sync


def sync(actor=None, source=None, trigger: str = EtrmSyncRun.Trigger.AUTOMATIC) -> EtrmSyncRun:
    """Pull every draft package and store what changed. A source that cannot be read fails the run and changes nothing."""
    if actor is not None:
        access.require_admin(actor, "start an eTRM sync")
    run = EtrmSyncRun.objects.create(trigger=trigger, started_by=actor)
    try:
        source = source or configured_source()
        run.source = source.label
        records = source.packages()
    except SourceError as e:
        run.status, run.error, run.finished_at = EtrmSyncRun.Status.FAILED, str(e), timezone.now()
        run.save()
        audit.record("etrm_sync.failed", obj=run, reason=str(e), actor=actor)
        return run

    seen: set[str] = set()
    for rec in records:
        if not in_scope(rec):
            continue
        run.packages_seen += 1
        seen.add(rec.measure_version_id)
        try:
            # rows that differ only in fields load shapes do not use (delivery type, NTG, offering ...) are one permutation here
            perms = list({json.dumps(c, sort_keys=True): c for c in map(_clean, rec.permutations)}.values())
            reason = exclusion_reason(rec, perms)
        except (TypeError, ValueError) as e:
            reason = f"Unexpected processing error: {e}"
        if reason:
            ExcludedPackage.objects.create(
                sync_run=run, measure_version_id=rec.measure_version_id, name=rec.name[:300], pa_lead=rec.pa_lead[:20], reason=reason
            )
            run.excluded += 1
            continue
        if _store(run, rec, perms):
            run.imported += 1
        else:
            run.unchanged += 1
    MeasurePackage.objects.exclude(measure_version_id__in=seen).update(in_last_sync=False)
    run.status, run.finished_at = EtrmSyncRun.Status.SUCCEEDED, timezone.now()
    run.save()
    audit.record(
        "etrm_sync.finished",
        obj=run,
        after={
            "source": run.source,
            "seen": run.packages_seen,
            "imported": run.imported,
            "unchanged": run.unchanged,
            "excluded": run.excluded,
        },
        actor=actor,
    )
    return run


@transaction.atomic
def _store(run: EtrmSyncRun, rec: PackageRecord, perms: list[dict]) -> bool:
    """Store a package; True when it is new or its content changed (a new version)."""
    sha = content_hash(rec, perms)
    now = timezone.now()
    package, created = MeasurePackage.objects.select_for_update().get_or_create(
        measure_version_id=rec.measure_version_id,
        defaults={"statewide_id": rec.statewide_id, "name": rec.name, "pa_lead": rec.pa_lead, "start_date": rec.start_date},
    )
    package.last_seen_at, package.in_last_sync = now, True
    if not created and package.current is not None and package.current.content_sha256 == sha:
        package.save(update_fields=["last_seen_at", "in_last_sync"])
        return False
    number = (package.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    version = PackageVersion.objects.create(
        package=package,
        number=number,
        sync_run=run,
        content_sha256=sha,
        name=rec.name,
        pa_lead=rec.pa_lead,
        start_date=rec.start_date,
        permutation_count=len(perms),
    )
    Permutation.objects.bulk_create((Permutation(version=version, **p) for p in perms), batch_size=2000)
    before = {"version": package.current.number if package.current else None, "pa_lead": package.pa_lead, "name": package.name}
    package.current, package.name, package.pa_lead, package.start_date = version, rec.name, rec.pa_lead, rec.start_date
    package.statewide_id = rec.statewide_id
    package.save()
    audit.record(
        "measure_package.synced",
        obj=package,
        before=None if created else before,
        after={"version": number, "pa_lead": rec.pa_lead, "name": rec.name, "permutations": len(perms), "sha256": sha},
        actor=None,
    )
    return True


def last_success() -> EtrmSyncRun | None:
    return EtrmSyncRun.objects.filter(status=EtrmSyncRun.Status.SUCCEEDED).order_by("-started_at").first()
