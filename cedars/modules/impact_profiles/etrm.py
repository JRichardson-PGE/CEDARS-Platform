"""The nightly eTRM sync: draft measure packages and their permutations, pulled from eTRM's restricted PostgreSQL database (full-precision
UECs), into a versioned snapshot with a sync history.

As in CEDARS' Load Shape Tool, only draft packages (Measure Version ID ending in "-draft") starting on or after
settings.LOAD_SHAPE_FIRST_START (1 January 2027: earlier measure versions keep their DEER impact profile IDs) are taken, and a package
with bad data is excluded and listed with the reason instead of stopping the sync. A package whose content has not changed keeps its
version; a changed package gets a new one, so an upload can name the exact permutations it was checked against.

The database's real schema is not known yet (no access): DatabaseSource reads the fixture schema in `etrm_fixture_schema.sql`, and only
its two queries need to change when the credentials arrive. FileSource reads the same data from a JSON file, for development and tests.
"""

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
            perms = [_clean(p) for p in rec.permutations]
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
