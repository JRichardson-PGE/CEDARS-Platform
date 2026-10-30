"""The profile library in the engine's format, and importing legacy CEDARS' library into the catalog.

Export: every profile that is not retired, at its current version, written as ReCET reads its reference folder's impact_profiles/
(recet.pipeline.real_impact_profiles): catalog.parquet (one row per key: id, end_use_name, iou_ac_territory, target_sector,
year_represented ...) and hourly.parquet (impact_profile_id, climate_zone, hour_of_year 1-8760, impact), one row group per profile,
which the engine indexes from the file's metadata. A profile with no territory gets a catalog row for each of
settings.IMPACT_PROFILE_TERRITORIES (CEDARS stores the deemed shapes once per IOU territory); the shapes are written once. An open
mapping row adds a catalog row with the alias ID and the target's id, so both IDs price on the same shapes. Each export is numbered,
kept, hashed and registered as a draft impact_profiles pack in the reference-data service (usable for analysis runs at once,
for filings once published there), so a run pins the one it used.

Import: legacy CEDARS' library (the scraped catalog.parquet and hourly.parquet ReCET uses today) into the catalog, as DEER, TOU and
"imported from CEDARS" profiles, so the platform holds the whole library and the engine's file can be produced here. A zone that is
not exactly 8,760 distinct hours is left out and reported, never repaired (the re-scrape fixed the duplicate hours at the source).
"""

import hashlib
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Max, Prefetch
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts.models import Organisation
from cedars.services.refdata import service as refdata

from . import access, shapes
from .models import HOURS, ImpactProfile, LibraryExport, ProfileMapping, ProfileVersion, ProfileZone

HOURLY_SCHEMA = pa.schema(
    [("impact_profile_id", pa.int32()), ("climate_zone", pa.string()), ("hour_of_year", pa.int16()), ("impact", pa.float64())]
)
SOURCE_TYPE = {"deemed": "load_shapes", "custom": "uploaded"}
HOUR_INDEX = np.arange(1, HOURS + 1, dtype=np.int16)


def _zone_order(z: str):
    digits = "".join(c for c in z if c.isdigit())
    return (int(digits) if digits else 99, z)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def current_profiles():
    versions = ProfileVersion.objects.filter(superseded_at__isnull=True).prefetch_related(
        Prefetch("zones", queryset=ProfileZone.objects.select_related("shape"))
    )
    return (
        ImpactProfile.objects.filter(retired_at__isnull=True)
        .select_related("package", "owner")
        .prefetch_related(Prefetch("versions", queryset=versions, to_attr="current_versions"))
        .order_by("pk")
    )


def export(actor=None) -> LibraryExport:
    """Write the library and record it as the next numbered export."""
    access.require_admin(actor, "write the profile library")
    mappings = list(ProfileMapping.objects.filter(ended_at__isnull=True).select_related("target"))
    with tempfile.TemporaryDirectory() as tmp:
        cat_path, hourly_path = os.path.join(tmp, "catalog.parquet"), os.path.join(tmp, "hourly.parquet")
        rows, families, profiles, zone_shapes = [], Counter(), 0, 0
        written: dict[int, dict] = {}
        with pq.ParquetWriter(hourly_path, HOURLY_SCHEMA, compression="zstd") as w:
            for p in current_profiles().iterator(chunk_size=500):
                version = p.current_versions[0] if p.current_versions else None
                if version is None:
                    continue
                zones = sorted(version.zones.all(), key=lambda z: _zone_order(z.climate_zone))
                if not zones:
                    continue
                arrays = [shapes.load(z.shape) for z in zones]
                n = len(zones)
                table = pa.table(
                    {
                        "impact_profile_id": pa.array(np.full(n * HOURS, p.pk, dtype=np.int32)),
                        "climate_zone": pa.array(np.repeat([z.climate_zone for z in zones], HOURS)),
                        "hour_of_year": pa.array(np.tile(HOUR_INDEX, n)),
                        "impact": pa.array(np.concatenate(arrays)),
                    },
                    schema=HOURLY_SCHEMA,
                )
                w.write_table(table, row_group_size=len(table))  # one row group per profile: the engine indexes them from metadata
                profiles += 1
                zone_shapes += n
                families[p.family] += 1
                base = _catalog_row(p, version, [z.climate_zone for z in zones])
                written[p.pk] = base
                territories = [p.iou_ac_territory] if p.iou_ac_territory else list(settings.IMPACT_PROFILE_TERRITORIES)
                rows += [dict(base, iou_ac_territory=t, pa=t) for t in territories]
        for m in mappings:
            base = written.get(m.target_id)
            if base is None:
                continue
            territories = [m.target.iou_ac_territory] if m.target.iou_ac_territory else list(settings.IMPACT_PROFILE_TERRITORIES)
            alias = dict(base, end_use_name=m.alias_key, target_sector=m.target_sector, description=f"maps to {m.target.profile_key}")
            rows += [dict(alias, iou_ac_territory=t, pa=t, mapping=m.pk) for t in territories]
        catalog = pa.Table.from_pylist(rows, schema=_catalog_schema())
        pq.write_table(catalog, cat_path, compression="zstd")
        with transaction.atomic():
            number = (LibraryExport.objects.select_for_update().aggregate(n=Max("number"))["n"] or 0) + 1
            folder = f"impact_profiles/library/{number:04d}"
            names, manifest = {}, {}
            for name, path in (("catalog", cat_path), ("hourly", hourly_path)):
                with open(path, "rb") as f:
                    names[name] = default_storage.save(f"{folder}/{name}.parquet", File(f, name=f"{name}.parquet"))
                manifest[f"{name}.parquet"] = {"sha256": _sha256(path), "bytes": os.path.getsize(path)}
            pack = refdata.register_pack(
                actor,
                kind="impact_profiles",
                label="Impact-profile library",
                version=f"export {number}",
                manifest=manifest,
                storage_uri=folder,
                source=f"Impact profiles module, library export {number}",
                notes=f"{profiles} profiles, {len(rows)} catalog rows: " + ", ".join(f"{n} {k}" for k, n in sorted(families.items())),
            )
            export = LibraryExport.objects.create(
                number=number,
                created_by=actor,
                catalog_name=names["catalog"],
                hourly_name=names["hourly"],
                catalog_sha256=manifest["catalog.parquet"]["sha256"],
                hourly_sha256=manifest["hourly.parquet"]["sha256"],
                catalog_rows=len(rows),
                profiles=profiles,
                zone_shapes=zone_shapes,
                families=dict(families),
                pack=pack,
            )
            audit.record(
                "profile_library.exported",
                obj=export,
                after={
                    "number": number,
                    "profiles": profiles,
                    "catalog_rows": len(rows),
                    "catalog_sha256": export.catalog_sha256,
                    "hourly_sha256": export.hourly_sha256,
                },
                actor=actor,
            )
    return export


def _catalog_schema() -> pa.Schema:
    return pa.schema(
        [
            ("id", pa.int64()),
            ("end_use_name", pa.string()),
            ("pa", pa.string()),
            ("iou_ac_territory", pa.string()),
            ("target_sector", pa.string()),
            ("climate_zones", pa.string()),
            ("start_year", pa.float64()),
            ("explicit_end_year", pa.float64()),
            ("description", pa.string()),
            ("year_represented", pa.float64()),
            ("source_impact_type", pa.string()),
            ("ac_version", pa.float64()),
            ("family", pa.string()),
            ("profile_version", pa.int32()),
            ("owner", pa.string()),
            ("mapping", pa.int64()),
        ]
    )


def _catalog_row(p: ImpactProfile, version: ProfileVersion, zones: list[str]) -> dict:
    src = version.source or {}
    start = src.get("start_year")
    if start is None and p.package and p.package.start_date:
        start = p.package.start_date.year
    return {
        "id": p.pk,
        "end_use_name": p.profile_key,
        "pa": p.iou_ac_territory,
        "iou_ac_territory": p.iou_ac_territory,
        "target_sector": p.target_sector,
        "climate_zones": ", ".join(zones),
        "start_year": float(start) if start is not None else None,
        "explicit_end_year": src.get("explicit_end_year"),
        "description": p.description,
        "year_represented": float(version.represents_year) if version.represents_year else None,
        "source_impact_type": src.get("source_impact_type") or SOURCE_TYPE.get(p.family, "calculated"),
        "ac_version": src.get("ac_version"),
        "family": p.family,
        "profile_version": version.number,
        "owner": p.owner.code if p.owner else "",
        "mapping": None,
    }


# --- importing legacy CEDARS' library


def family_of(end_use_name: str) -> str:
    if end_use_name.startswith("DEER:"):
        return ImpactProfile.Family.DEER
    if re.match(r"^\d+ = ", end_use_name):
        return ImpactProfile.Family.TOU  # numbered end uses are TOU profiles, not DEER (CEDARS-Research notes)
    return ImpactProfile.Family.LEGACY


def _float_or_none(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


def import_library(catalog_path: str | Path, hourly_path: str | Path, *, limit: int | None = None, file_label: str = "") -> dict:
    """Load legacy CEDARS' library into the catalog (a system job: the management command). Re-running it adds a version only where
    the shapes changed."""
    catalog = pq.read_table(str(catalog_path)).to_pylist()
    pf = pq.ParquetFile(str(hourly_path))
    groups = {}
    id_col = pf.schema_arrow.get_field_index("impact_profile_id")
    for gi in range(pf.metadata.num_row_groups):
        stats = pf.metadata.row_group(gi).column(id_col).statistics
        if stats is not None and stats.has_min_max and stats.min == stats.max:
            groups[int(stats.min)] = gi
    orgs = {o.code: o for o in Organisation.objects.all()}
    counts = Counter()
    bad_zones = []
    done = 0
    for row in catalog:
        if limit is not None and done >= limit:
            break
        gi = groups.get(int(row["id"]))
        if gi is None:
            counts["no hourly data"] += 1
            continue
        done += 1
        t = pf.read_row_group(gi, columns=["climate_zone", "hour_of_year", "impact"])
        zone_col = np.asarray(t.column("climate_zone").to_pylist(), dtype=object)
        hours, values = t.column("hour_of_year").to_numpy(), t.column("impact").to_numpy()
        zones = {}
        for z in sorted(set(zone_col.tolist()), key=_zone_order):
            sel = zone_col == z
            h, v = hours[sel], values[sel]
            order = np.argsort(h, kind="stable")
            h, v = h[order], v[order]
            if len(h) != HOURS or not np.array_equal(h, HOUR_INDEX) or not np.isfinite(v).all():
                bad_zones.append(f"{row['id']} {row['end_use_name']} CZ {z}: {len(h)} rows, {len(np.unique(h))} distinct hours")
                continue
            zones[str(z)] = v.astype(float)
        if not zones:
            counts["no complete zone"] += 1
            continue
        counts[_import_one(row, zones, orgs, file_label)] += 1
    summary = {"profiles": done, **counts, "zones_left_out": len(bad_zones), "examples": bad_zones[:20]}
    audit.record(
        "profile_library.imported",
        object_type="impact_profiles.library",
        object_repr=file_label or str(catalog_path),
        after=summary,
        actor=None,
    )
    return summary


@transaction.atomic
def _import_one(row: dict, zones: dict, orgs: dict, file_label: str) -> str:
    key = (str(row["end_use_name"]).strip(), str(row["target_sector"]).strip(), str(row.get("iou_ac_territory") or "").strip())
    year = _float_or_none(row.get("year_represented"))
    profile, created = ImpactProfile.objects.get_or_create(
        profile_key=key[0],
        target_sector=key[1],
        iou_ac_territory=key[2],
        defaults={
            "family": family_of(key[0]),
            "owner": orgs.get(key[2]),
            "description": str(row.get("description") or "")[:500],
            "legacy_id": int(row["id"]),
        },
    )
    stored = {z: shapes.store(a) for z, a in zones.items()}
    current = None if created else profile.current_version()
    represents = int(year) if year else None
    if (
        current is not None
        and current.represents_year == represents
        and {z.climate_zone: z.shape_id for z in current.zones.all()} == {z: s.pk for z, s in stored.items()}
    ):
        return "unchanged"
    if current is not None:
        ProfileVersion.objects.filter(pk=current.pk).update(superseded_at=timezone.now())
    version = ProfileVersion.objects.create(
        profile=profile,
        number=(current.number + 1) if current else 1,
        represents_year=represents,
        reason="imported from legacy CEDARS" if current is None else "re-imported: shapes changed",
        source={
            "kind": "imported",
            "legacy_id": int(row["id"]),
            "source_impact_type": row.get("source_impact_type"),
            "start_year": _float_or_none(row.get("start_year")),
            "explicit_end_year": _float_or_none(row.get("explicit_end_year")),
            "ac_version": _float_or_none(row.get("ac_version")),
            "file": file_label,
        },
    )
    ProfileZone.objects.bulk_create(ProfileZone(version=version, climate_zone=z, shape=s) for z, s in stored.items())
    return "imported" if current is None else "new version"
