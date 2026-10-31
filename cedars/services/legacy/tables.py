"""Reading staged legacy CEDARS downloads into canonical tables, for the modules' loads (Budgets: filings; Claims: claim years).

Every value is read as text under its canonical name (the crosswalk, `dictionary.legacy`), so nothing is lost before the checks; numbers
are cast where a load needs them, and a value that is not a number is a hard failure. A large all-PA file is split by PA as it streams
(`split_by_pa`), so no load holds a whole year in memory."""

import csv
import hashlib
import io
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.dictionary import legacy as crosswalk
from cedars.services.registry import service as registry

from .models import LegacySource
from .service import LegacyError, open_member

TRUE = {"true", "1", "yes", "y", "t"}
# The program flag columns (Prg_*) and the registry's flag names.
FLAGS = {
    "Prg_Resource_Flag": "Resource_Flag",
    "Prg_NonResource_Flag": "NonResource_Flag",
    "Prg_Deemed_Flag": "Deemed_Flag",
    "Prg_Custom_Flag": "Custom_Flag",
    "Prg_Upstream_Flag": "Upstream_Flag",
    "Prg_Midstream_Flag": "Midstream_Flag",
    "Prg_Downstream_Flag": "Downstream_Flag",
    "Prg_DirectInstall": "DirectInstall",
    "Prg_Audit_Flag": "Audit_Flag",
    "Prg_Financing": "Financing",
}


def _options(layout: str):
    import pyarrow.csv as pacsv

    names = crosswalk.layouts()[layout]
    return (
        names,
        pacsv.ReadOptions(column_names=names, skip_rows=1, encoding="utf-8", block_size=1 << 23),
        pacsv.ConvertOptions(column_types={n: "string" for n in names}, strings_can_be_null=False),
    )


def read(source: LegacySource, member: str, layouts: tuple[str, ...]):
    """One CSV of a staged zip as text columns under canonical names, with its layout (one of `layouts`)."""
    import pyarrow.csv as pacsv

    layout = source.members[member]
    if layout not in layouts:
        raise LegacyError(f"{source.name}: {member} is a {layout} file, not one of {', '.join(layouts)}")
    names, read_options, convert_options = _options(layout)
    with open_member(source, member, text=False) as f:
        table = pacsv.read_csv(f, read_options=read_options, convert_options=convert_options)
    return table.rename_columns([crosswalk.rename(layout)[n] for n in names]), layout


def split_by_pa(source: LegacySource, member: str, layouts: tuple[str, ...], folder: Path) -> tuple[str, dict[str, Path]]:
    """Stream one CSV of a staged zip into one Parquet file of text columns per PA under `folder`: (layout, {PA: path})."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq

    layout = source.members[member]
    if layout not in layouts:
        raise LegacyError(f"{source.name}: {member} is a {layout} file, not one of {', '.join(layouts)}")
    names, read_options, convert_options = _options(layout)
    renamed = [crosswalk.rename(layout)[n] for n in names]
    writers, paths = {}, {}
    try:
        with open_member(source, member, text=False) as f:
            for batch in pacsv.open_csv(f, read_options=read_options, convert_options=convert_options):
                batch = pa.Table.from_batches([batch]).rename_columns(renamed)
                for code in pc.unique(batch["PA"]).to_pylist():
                    part = batch.filter(pc.equal(batch["PA"], code))
                    if code not in writers:
                        paths[code] = folder / f"{hashlib.sha256(code.encode()).hexdigest()[:16]}.parquet"
                        writers[code] = pq.ParquetWriter(paths[code], part.schema, compression="zstd")
                    writers[code].write_table(part)
    finally:
        for w in writers.values():
            w.close()
    return layout, paths


def scratch() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="legacy-")


def count_rows(source: LegacySource, layouts: tuple[str, ...]) -> int:
    """The data rows of a staged file's CSVs in the given layouts (the per-PA files' count check)."""
    n = 0
    for member, layout in source.members.items():
        if layout not in layouts:
            continue
        with open_member(source, member) as f:
            n += sum(1 for _ in csv.reader(f)) - 1
    return n


def is_ratio(name: str) -> bool:
    return name.endswith(("Ratio", "RatioNoAdmin")) or name.startswith("Lev")


def blank_to_null(col):
    import pyarrow as pa
    import pyarrow.compute as pc

    col = pc.utf8_trim_whitespace(col)
    return pc.if_else(pc.equal(col, ""), pa.scalar(None, pa.string()), col)


def is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def numbers(table, columns: list[str], errors: list[str]) -> dict:
    """{column: (float64 array, exact sum as a Decimal)} for each column present; a value that is not a number is a hard failure. The
    exact sum adds the text's decimals (decimal256 when every value fits 38 decimal places, else Python's Decimal)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    out = {}
    for c in columns:
        if c not in table.column_names:
            continue
        col = blank_to_null(table[c])
        try:
            as_float = pc.cast(col, pa.float64())
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
            bad = next((v for v in col.to_pylist() if v is not None and not is_number(v)), "")
            errors.append(f"{c} holds a value that is not a number ({bad[:40]!r})")
            continue
        try:
            exact = Decimal(pc.sum(pc.cast(col, pa.decimal256(76, 38))).as_py() or 0)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
            exact = sum((Decimal(v) for v in col.to_pylist() if v is not None), Decimal(0))
        out[c] = (as_float, exact)
    return out


def texts(table, column: str) -> list[str | None]:
    """A column's values stripped, blanks as None (for exact Decimal sums)."""
    return [None if t is None or not t.strip() else t.strip() for t in table[column].to_pylist()]


def dec(value) -> Decimal:
    return Decimal(0) if value is None else Decimal(value)


def close(a: float, b: float, rel: float) -> bool:
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def parquet(table) -> tuple[bytes, str]:
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def organisation(code: str) -> Organisation:
    """The PA a legacy file names, created under its CEDARS code when the platform does not have it yet."""
    org = Organisation.objects.filter(code=code).first()
    if org is None:
        org = access.create_organisation(None, code=code, name=code, kind="pa", pa_type=access.known_pa_type(code))
    return org


def programs(rows, program_cols: list[str]):
    """One row per (PrgID, program year) with the program columns as filed (the first record's)."""
    import pyarrow as pa

    seen, out = set(), defaultdict(list)
    cols = ["PrgID", "Used_Year", *program_cols]
    for r in rows.select(cols).to_pylist():
        k = (r["PrgID"], r["Used_Year"])
        if k in seen:
            continue
        seen.add(k)
        for c in cols:
            out[c].append(r[c])
    return pa.table({c: pa.array(out[c], pa.string()) for c in cols})


def add_program_history(programs_table, org: Organisation | None, reason: str, *, statewide: bool = False) -> int:
    """The program years the registry has no version for (its own history wins): `org` administers them, or none for statewide ones."""
    by_program: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in programs_table.to_pylist():
        year = r["Used_Year"]
        if not (r["PrgID"] or "").strip() or not str(year).strip().isdigit():
            continue
        by_program[r["PrgID"].strip()][int(year)] = {
            "name": (r.get("Prg_ProgramName") or r["PrgID"]).strip(),
            "status": (r.get("Prg_ProgramStatus") or "").strip(),
            "segment": (r.get("Prg_ProgramSegment") or "").strip(),
            "primary_sector": (r.get("Prg_PrimarySector") or "").strip(),
            "implementer": (r.get("Prg_ProgramImplementer") or "").strip(),
            "category": (r.get("Prg_ProgramCategory") or "").strip(),
            "statewide_program": (r.get("Prg_StatewideProgram") or "").strip(),
            "flags": {flag: (r.get(c) or "").strip() for c, flag in FLAGS.items() if (r.get(c) or "").strip()},
            "exclude_from_budget": (r.get("Prg_Exclude_From_Budget") or "").strip().lower() in TRUE,
            "exclude_from_ce": (r.get("Prg_Exclude_From_CE") or "").strip().lower() in TRUE,
        }
    added = 0
    for prg_id, years in sorted(by_program.items()):
        _, n = registry.add_history(
            None, prg_id=prg_id, administrator=None if statewide else org, statewide=statewide, years=years, reason=reason
        )
        added += n
    return added


# Each cost test's benefits per record (Jake's reconciliation, 2026-10-31: CEDARS' published ratios are the benefit columns over the
# costs; the files' ratio columns are rounded and, for some PAs, disagree with the record's own benefits): the filed benefit column where
# the layout has one, else the sum of the benefit columns below, else ratio x cost. A record whose benefit columns are all blank falls
# back to ratio x cost (a batch with no benefit values at all is "ratio x cost" throughout). RIM's benefits are PAC's (CET prices both on
# the avoided costs; RIM's bill losses are a cost), as CEDARS' published RIM ratios show.
NUMERATORS = {"TRCBen": ("TRCRatio", "TRCCost"), "PACBen": ("PACRatio", "PACCost"), "RIMBen": ("RIMRatio", "RIMCost")}
BENEFIT_PARTS = {"TRCBen": ("ElecBen", "GasBen", "OtherBen"), "PACBen": ("ElecBen", "GasBen"), "RIMBen": ("ElecBen", "GasBen")}
# Where the layout files PAC's benefits (claims from 2023: "PAC Benefits", which already holds Other Benefits and can exceed ElecBen +
# GasBen with that layout's further benefit columns), CEDARS builds TRC's and RIM's on it alone: TRC = PAC Benefits (2023 SCG's and 2025
# SDG&E's and SCE's published TRC ratios exactly; adding Other Benefits counts them twice).
PARTS_ON_PAC = {"TRCBen": ("PACBen",), "RIMBen": ("PACBen",)}


@dataclass
class Benefit:
    values: object  # float64 array, one per record
    from_ratio: object | None  # ratio x cost per record, for the soft check (None when the layout lacks the ratio or the cost)
    source: str  # filed | benefit columns | ratio x cost


def benefits(columns: dict) -> dict[str, Benefit]:
    """Each test's benefits per record from {column: float array} (the outputs of a batch)."""
    import pyarrow.compute as pc

    def filled(c):
        return pc.fill_null(columns[c], 0.0)

    out = {}
    for ben, (ratio, cost) in NUMERATORS.items():
        from_ratio = pc.multiply(filled(ratio), filled(cost)) if ratio in columns and cost in columns else None
        if ben in columns:
            out[ben] = Benefit(columns[ben], from_ratio, "filed")
            continue
        spec = PARTS_ON_PAC[ben] if "PACBen" in columns and ben in PARTS_ON_PAC else BENEFIT_PARTS[ben]
        parts = [c for c in spec if c in columns]
        present = None
        for c in parts:
            present = pc.is_valid(columns[c]) if present is None else pc.or_(present, pc.is_valid(columns[c]))
        if parts and (from_ratio is None or pc.any(present).as_py()):
            total = filled(parts[0])
            for c in parts[1:]:
                total = pc.add(total, filled(c))
            if from_ratio is not None:
                total = pc.if_else(present, total, from_ratio)
            out[ben] = Benefit(total, from_ratio, "benefit columns")
        elif from_ratio is not None:
            out[ben] = Benefit(from_ratio, from_ratio, "ratio x cost")
    return out
