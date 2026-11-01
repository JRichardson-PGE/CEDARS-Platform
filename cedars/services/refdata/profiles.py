"""Reading one profile's 8,760s back out of a registered impact-profile library pack (BR-05.6).

A true-up is priced against the profile its parent claim was priced on: the version that run's pinned library held, which may since
have been replaced (a custom profile replaced for the true-up is the usual case). The pack's files are the engine's own format
(the Impact profiles module's library export: catalog.parquet, one row per name with its profile_version and the shape_id of the
hourly row group holding its 8,760s; hourly.parquet, one row group per stored profile), so a profile is read back exactly as that run
read it. `supplied_file` writes those 8,760s in ReCET's supplied 8,760-profile format (ReCET overrides.load_user_profiles) under a
name of their own, so a later run prices that version whatever the library in force holds then.
"""

import io
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.parquet as pq
from django.core.files.storage import default_storage

from .models import PackVersion

SUPPLIED_SCHEMA = pa.schema(
    [
        ("end_use_name", pa.string()),
        ("pa", pa.string()),
        ("target_sector", pa.string()),
        ("climate_zone", pa.string()),
        ("hour_of_year", pa.int32()),
        ("impact", pa.float64()),
        ("represents_year", pa.int32()),
    ]
)
SUPPLIED_COLUMNS = SUPPLIED_SCHEMA.names
DEFAULT_REPRESENTS_YEAR = 2009  # a catalog row with no year is on the DEER calendar (ReCET real_impact_profiles)


@dataclass(frozen=True)
class PackedProfile:
    """One catalog row of a library pack: the name a record filed, its version in that pack and where its 8,760s are."""

    name: str
    territory: str
    target_sector: str
    version: int | None
    represents_year: int | None
    shape_id: int | None


def library_of(calculation: dict, pack_versions: dict) -> PackVersion | None:
    """The impact-profile library a filing's run was priced on: the pack its calculation pinned (refdata.pin, by kind), else, for a
    run recorded before the calculation kept its pins, the pack whose catalog file is the one the run's receipt names."""
    pin = ((calculation or {}).get("packs") or {}).get(PackVersion.Kind.IMPACT_PROFILES) or {}
    if pin.get("id"):
        return PackVersion.objects.filter(pk=pin["id"], kind=PackVersion.Kind.IMPACT_PROFILES).first()
    sha = (pack_versions or {}).get("impact_profiles/catalog.parquet")
    if not sha:
        return None
    libraries = PackVersion.objects.filter(kind=PackVersion.Kind.IMPACT_PROFILES)
    return next((p for p in libraries if (p.manifest.get("catalog.parquet") or {}).get("sha256") == sha), None)


def _open(pack: PackVersion, name: str):
    return default_storage.open(f"{pack.storage_uri.rstrip('/')}/{name}", "rb")


def catalog(pack: PackVersion) -> list[PackedProfile]:
    """Every name in the pack's catalog."""
    if pack.kind != PackVersion.Kind.IMPACT_PROFILES:
        raise ValueError(f"{pack} is not an impact-profile library.")
    with _open(pack, "catalog.parquet") as f:
        rows = pq.read_table(f).to_pylist()
    out = []
    for r in rows:
        year = r.get("year_represented")
        shape = r.get("shape_id", r.get("id"))
        out.append(
            PackedProfile(
                name=str(r.get("end_use_name") or ""),
                territory=str(r.get("iou_ac_territory") or r.get("pa") or ""),
                target_sector=str(r.get("target_sector") or ""),
                version=int(r["profile_version"]) if r.get("profile_version") is not None else None,
                represents_year=int(year) if year is not None and year == year else None,
                shape_id=int(shape) if shape is not None and shape == shape else None,
            )
        )
    return out


def find(entries: list[PackedProfile], name: str, territory: str, target_sector: str) -> PackedProfile | None:
    """The catalog row a record's profile names: the same name (any case) in the record's territory, its own target sector first
    (the library keys a profile by name, sector and territory), else the first row of that name in the territory, else of that name."""
    key = name.strip().lower()
    same = [e for e in entries if e.name.strip().lower() == key]
    here = [e for e in same if e.territory.upper() == territory.strip().upper()] or same
    return next((e for e in here if e.target_sector.strip().lower() == target_sector.strip().lower()), here[0] if here else None)


def shapes(pack: PackVersion, shape_ids) -> dict[int, dict[str, list[float]]]:
    """{shape_id: {climate zone: its 8,760 impacts in hour order}} for the stored profiles asked for, read row group by row group."""
    wanted = {int(s) for s in shape_ids if s is not None}
    out: dict[int, dict[str, list[float]]] = {}
    if not wanted:
        return out
    with _open(pack, "hourly.parquet") as f:
        pf = pq.ParquetFile(f)
        for i in range(pf.num_row_groups):
            stats = pf.metadata.row_group(i).column(0).statistics
            if stats is not None and stats.has_min_max and not any(stats.min <= s <= stats.max for s in wanted):
                continue
            t = pf.read_row_group(i).to_pydict()
            for pid, zone, hour, impact in zip(t["impact_profile_id"], t["climate_zone"], t["hour_of_year"], t["impact"], strict=True):
                if int(pid) in wanted:
                    out.setdefault(int(pid), {}).setdefault(str(zone), []).append((int(hour), float(impact)))
    return {pid: {z: [v for _, v in sorted(hours)] for z, hours in zones.items()} for pid, zones in out.items()}


def supplied_file(entries: list[tuple[str, str, str, PackedProfile]], pack: PackVersion) -> bytes:
    """ReCET's supplied 8,760-profile file, as Parquet (so every impact reads back bit for bit), holding each (name, pa, target sector,
    catalog row) given: the catalog row's 8,760s under the name, for that PA (blank: any) and target sector (blank: any), on the row's
    calendar (blank: 2009, as the library has it)."""
    stored = shapes(pack, {e.shape_id for _, _, _, e in entries})
    cols: dict[str, list] = {c: [] for c in SUPPLIED_COLUMNS}
    for name, pa_code, sector, e in entries:
        for zone, values in sorted(stored.get(e.shape_id, {}).items()):
            n = len(values)
            cols["end_use_name"] += [name] * n
            cols["pa"] += [pa_code] * n
            cols["target_sector"] += [sector] * n
            cols["climate_zone"] += [zone] * n
            cols["hour_of_year"] += list(range(1, n + 1))
            cols["impact"] += values
            cols["represents_year"] += [e.represents_year or DEFAULT_REPRESENTS_YEAR] * n
    table = pa.table(cols, schema=SUPPLIED_SCHEMA)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()
