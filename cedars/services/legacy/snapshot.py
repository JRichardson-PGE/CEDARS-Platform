"""A legacy import as the dashboards read it: one measure-level file per imported version, joined from the tables its load stored
(Measure: keys, inputs and Legacy_ columns; Outputs: CEDARS' CET outputs as filed; Programs: each program year's Prg_ columns as filed),
frozen as the version's snapshot (`snapshots.publish`). Built from the stored tables, so imports loaded before this existed get theirs
from `manage.py snapshot_legacy` with no reload.

Statewide unallocated claims (outside every total) get no snapshot. Columns the downloads lack are added the way the loads' own program
lines and checks count them (so the dashboards and the reconciliation agree):
  TRCBen, PACBen, RIMBen        where the layout has no benefit column: the benefit columns' sum, else ratio x cost (tables.benefits)
  TotalExpenditures             WeightedProgramCost + the four incentive outputs (claims before 2023 and every filing)
  RebatesandIncents             the four incentive outputs (likewise)
A file's own TotalExpenditures and RebatesandIncents (claims from 2023) are kept as filed."""

import tempfile
from pathlib import Path

from cedars.core import storage
from cedars.services.snapshots import service as snapshots
from cedars.services.snapshots.models import Snapshot

from . import tables
from .models import LegacyImport
from .service import AREA

FILE = "measure_level.parquet"
BATCH_ROWS = 50_000
INCENTIVES = ("EndUserRebate", "IncentiveToOthers", "DILaborCost", "DIMaterialCost")
KEYS = ("PrgID", "Used_Year")


def folder_of(imp: LegacyImport) -> str:
    """The stored tables' folder, from the version's file list."""
    for f in imp.version.files:
        if f.get("name") == "Measure.parquet":
            return f["key"].rsplit("/", 1)[0] + "/"
    raise ValueError(f"{imp} has no stored Measure table")


def _aligned(a, b):
    """Two batch iterators over files with the same rows, cut into matching slices whatever their row groups."""
    import pyarrow as pa

    ha = hb = None
    while True:
        ha = ha if ha is not None and ha.num_rows else next(a, None)
        hb = hb if hb is not None and hb.num_rows else next(b, None)
        if ha is None or hb is None:
            if (ha is None) != (hb is None):
                raise ValueError("the Measure and Outputs tables hold different numbers of rows")
            return
        n = min(ha.num_rows, hb.num_rows)
        yield pa.Table.from_batches([ha.slice(0, n)]), pa.Table.from_batches([hb.slice(0, n)])
        ha, hb = ha.slice(n), hb.slice(n)


def _add(t, name, values):
    return t.set_column(t.schema.get_field_index(name), name, values) if name in t.column_names else t.append_column(name, values)


def _derived(t):
    import pyarrow as pa
    import pyarrow.compute as pc

    def col(c):
        return pc.fill_null(t[c], 0.0) if c in t.column_names else pa.array([0.0] * t.num_rows, pa.float64())

    for ben, b in tables.benefits({c: t[c] for c in t.column_names if pa.types.is_floating(t.schema.field(c).type)}).items():
        if ben not in t.column_names:
            t = _add(t, ben, b.values)
    if "RebatesandIncents" not in t.column_names:
        incentives = col(INCENTIVES[0])
        for c in INCENTIVES[1:]:
            incentives = pc.add(incentives, col(c))
        t = _add(t, "RebatesandIncents", incentives)
    if "TotalExpenditures" not in t.column_names:
        t = _add(t, "TotalExpenditures", pc.add(col("WeightedProgramCost"), t["RebatesandIncents"]))
    return t


def build(folder: str, out: Path) -> int:
    """Write the joined measure-level file for the tables in `folder` (uploads area) to `out`; the number of rows written."""
    import pyarrow.parquet as pq

    store = storage.store(AREA)
    with tempfile.TemporaryDirectory() as tmp:
        local = {}
        for name in ("Measure.parquet", "Outputs.parquet", "Programs.parquet"):
            local[name] = Path(tmp) / name
            with store.open(folder + name) as src, open(local[name], "wb") as dst:
                while block := src.read(1 << 22):
                    dst.write(block)
        programs = pq.read_table(local["Programs.parquet"])
        programs = programs.select([*KEYS, *(c for c in programs.column_names if c not in KEYS)])
        measure, outputs = pq.ParquetFile(local["Measure.parquet"]), pq.ParquetFile(local["Outputs.parquet"])
        own = set(measure.schema_arrow.names)
        writer, rows = None, 0
        try:
            for m, o in _aligned(measure.iter_batches(batch_size=BATCH_ROWS), outputs.iter_batches(batch_size=BATCH_ROWS)):
                for c in o.column_names:
                    if c not in own:
                        m = m.append_column(c, o[c])
                for k in KEYS:
                    if k not in m.column_names:
                        raise ValueError(f"the stored tables have no {k}")
                t = _derived(m).join(programs, list(KEYS), join_type="left outer", use_threads=False)
                if writer is None:
                    schema = t.schema
                    writer = pq.ParquetWriter(out, schema, compression="zstd")
                writer.write_table(t.select(schema.names).cast(schema))
                rows += t.num_rows
        finally:
            if writer is not None:
                writer.close()
    if writer is None:  # no rows: an empty file with the stored columns
        pq.write_table(_derived(measure.schema_arrow.empty_table()), out)
    return rows


def publish_snapshot(imp: LegacyImport, *, release: bool = False, replace: str = "") -> Snapshot | None:
    """The import's snapshot, built and stored beside its tables when it has none yet. None for statewide unallocated claims. `replace`
    (a reason) withdraws a snapshot it already has and builds it again, for one built by an earlier, wrong rule."""
    if imp.outside_totals:
        return None
    done = Snapshot.objects.filter(version=imp.version).first()
    if done is not None and replace:
        snapshots.withdraw(imp.version_id, reason=replace, release=False)
    elif done is not None:
        return done
    folder = folder_of(imp)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / FILE
        build(folder, path)
        storage.store(AREA).put_file(folder + FILE, path)
    return snapshots.publish(imp.version_id, area=AREA, key=folder + FILE, release=release)


def rebuild(datasets, *, changed: bool = False) -> None:
    """One release per dataset after a load or a backfill (each import was published with release=False). `changed` after snapshots
    were replaced, which leaves the set of versions as it was."""
    for d in sorted(set(datasets)):
        snapshots.rebuild(d, "legacy CEDARS snapshots replaced" if changed else "legacy CEDARS imports published", changed=changed)


def stored_benefits(imp: LegacyImport, keys: tuple[str, ...]) -> dict[tuple, dict[str, float]]:
    """{key values: {benefit: sum}} of the benefits a layout has no column for, built from the import's stored Outputs table by
    `tables.benefits` and summed on `keys` (PrgID, or PrgID and Used_Year), for correcting results computed by an earlier rule."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    out: dict[tuple, dict[str, float]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "Outputs.parquet"
        with storage.store(AREA).open(folder_of(imp) + "Outputs.parquet") as src, open(local, "wb") as dst:
            while block := src.read(1 << 22):
                dst.write(block)
        for batch in pq.ParquetFile(local).iter_batches(batch_size=BATCH_ROWS):
            t = pa.Table.from_batches([batch])
            built = {ben: b.values for ben, b in tables.benefits({c: t[c] for c in t.column_names if c not in keys}).items()}
            built = {ben: v for ben, v in built.items() if ben not in t.column_names}
            if not built:
                return {}
            g = pa.table({**{k: t[k] for k in keys}, **built}).group_by(list(keys)).aggregate([(b, "sum") for b in built])
            for r in g.to_pylist():
                mine = out.setdefault(tuple(r[k] or "" for k in keys), {})
                for b in built:
                    mine[b] = mine.get(b, 0.0) + (r[f"{b}_sum"] or 0.0)
    return out
