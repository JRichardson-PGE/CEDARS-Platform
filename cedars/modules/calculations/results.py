"""A run's Results and Compare tables (BR-06.9, 06.11, 04.1), built by ReCET's own results code (`recet.results_view`, imported
unchanged) over the group sums the worker keeps beside the measure-level table (`out/_results/group_sums<N>_<file>.parquet`: one row
per PA, program, sector, segment and program year, with every additive output column summed). So a 1,000,000-measure run's Results
open from a file of a few hundred rows, never the measure-level table; ratios are recomputed from the summed benefits and costs.

Files are read from the results area into a local cache (`CALCULATIONS_RESULTS_CACHE`) once per run: an ended run's files never change.
The measure level is offered for runs of up to the engine's MAX_RUN_MEASURE_ROWS (50,000) and reads the kept measure-level parquet.
Without the engine installed the pages say so."""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path, PurePosixPath

from django.conf import settings

from cedars.core import storage

from .models import Run

RESULTS_DIR = "_results/"
# The page's headline numbers, as ReCET's own run page shows them (recet/web/app.py Ctx.HEADLINE).
HEADLINE = [
    ("TotalSystemBenefit", "Total system benefit"),
    ("TotalExpenditures", "Budget"),
    ("TRC_ratio", "TRC"),
    ("PAC_ratio", "PAC"),
    ("RIM_ratio", "RIM"),
    ("SCB_ratio", "SCT Base"),
]


def headline_totals(totals: dict, measures: int | None) -> list[dict]:
    """The run page's tiles from the receipt's portfolio totals, labeled and formatted as the Results page shows them: the measure
    count, then HEADLINE's columns the run has (whole dollars; ratios to two decimals)."""
    out = [] if measures is None else [{"label": "Measures", "value": f"{int(measures):,}"}]
    for key, label in HEADLINE:
        if key in totals and totals[key] is not None:
            out.append({"label": label, "value": format_total(key, totals[key])})
    return out


def format_total(key: str, value) -> str:
    """A receipt total as text: ratios to two decimals, dollars and quantities to the whole number, blank for nothing."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v:  # NaN
        return "–"
    return f"{v:,.2f}" if key.lower().endswith(("_ratio", "ratio", "rationoadmin")) else f"{v:,.0f}"


def other_totals(totals: dict) -> list[tuple[str, str]]:
    """Every other total of the receipt, formatted, for the run page's full list."""
    shown = {k for k, _ in HEADLINE}
    return [(k, format_total(k, v)) for k, v in totals.items() if k not in shown]


class NoResults(Exception):
    """The run has no Results tables; the message says why."""


def engine():
    """recet.results_view, or None when the engine is not installed."""
    try:
        from recet import results_view
    except ImportError:
        return None
    return results_view


def _cache_root() -> Path:
    return Path(getattr(settings, "CALCULATIONS_RESULTS_CACHE", "var/cache/results"))


def _files(run: Run) -> dict[str, str]:
    """{name under out/: storage key} of the run's kept results files (out/_results/...)."""
    prefix = run.storage_prefix + "out/"
    return {f.key[len(prefix) :]: f.key for f in storage.store("results").list(prefix + RESULTS_DIR)}


def _fetch(run: Run, key: str) -> Path:
    """A local copy of one of the run's files (downloaded once; an ended run's files never change)."""
    dest = _cache_root() / str(run.pk) / PurePosixPath(key).name
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    src = storage.store("results").open(key)
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(src, f, 1 << 20)
    finally:
        src.close()
    tmp.replace(dest)
    return dest


def sources(run: Run) -> dict[str, str | None]:
    """The storage keys of the run's group sums and its measure-level parquet (None when it has none)."""
    rv = engine()
    files = _files(run)
    sums = next((k for n, k in sorted(files.items()) if PurePosixPath(n).name.startswith(f"group_sums{rv.CUBE_VERSION}_")), None)
    measures = next(
        (k for n, k in sorted(files.items()) if PurePosixPath(n).name.startswith("measure_level_") and n.endswith(".parquet")), None
    )
    return {"sums": sums, "measures": measures}


@lru_cache(maxsize=16)
def _load(run_pk: str, finished_at: str):
    import pandas as pd

    rv = engine()
    run = Run.objects.get(pk=run_pk)
    src = sources(run)
    if src["sums"]:
        res = rv.RunResults(pd.read_parquet(_fetch(run, src["sums"])))
    elif src["measures"]:
        res = rv.RunResults.from_file(_fetch(run, src["measures"]))  # builds the sums and keeps them beside the cached file
    else:
        raise NoResults("This run wrote no Results tables (a check-inputs-only run, or it stopped early).")
    res.measure_source = src["measures"]
    return res


def load(run: Run):
    """The run's recet.results_view.RunResults. Raises NoResults with the reason when there are none to show."""
    if engine() is None:
        raise NoResults("The Results tables need the ReCET engine package installed on this server (engine/README.md).")
    if run.status != Run.Status.SUCCEEDED:
        ended = {Run.Status.FAILED: "failed", Run.Status.REFUSED: "was refused by the engine", Run.Status.CANCELLED: "was stopped"}
        if run.status in ended:  # an ended run never will have results (UI review 2, #28)
            raise NoResults(f"This run {ended[run.status]}, so it has no results. Its page says why.")
        raise NoResults("Results are shown once the run has finished.")
    return _load(str(run.pk), run.finished_at.isoformat() if run.finished_at else "")


def with_measure_rows(run: Run, res):
    """`res` able to show the measure level: its measure-level parquet fetched (only for runs small enough to show it)."""
    if res.path is None and res.measure_source and res.measure_level_available():
        res.path = _fetch(run, res.measure_source)
    return res


def cet_comparison(run: Run, files: list[dict]) -> dict | None:
    """A parallel run's comparison with CET (the engine's cet_comparison_summary_* file): each summed metric's ReCET and CET totals
    over the measures matched by CET_ID, and their ratio. None when the run has none, or the engine is not installed."""
    if "cet_outputs" not in run.inputs or run.status != Run.Status.SUCCEEDED:
        return None
    names = {PurePosixPath(f["name"]).name.split("_", 3)[2] if f["name"].startswith("cet_comparison_") else "": f["name"] for f in files}
    if "summary" not in names:
        return None
    try:
        from recet import outputs
    except ImportError:
        return None
    t = outputs.read_table(_fetch(run, run.storage_prefix + "out/" + names["summary"]))
    rows = []
    for r in t.to_dict("records"):
        ours, cet, ratio = r.get("POC"), r.get("CET"), r.get("POC/CET")
        rows.append(
            {
                "metric": r["metric"],
                "recet": f"{ours:,.0f}",
                "cet": f"{cet:,.0f}",
                "ratio": "" if ratio is None or ratio != ratio else f"{ratio:.4f}",  # NaN: CET's total is 0
            }
        )
    return {"rows": rows, "summary": names["summary"], "measures": names.get("measures")}


def tiles(res, pa: str, years=None) -> list[dict]:
    """The headline numbers for the PA and program years picked: measures, then HEADLINE's columns this run has."""
    rv = engine()
    cat = res.catalog()
    keys = [k for k, _ in HEADLINE if k in cat]
    t = res.table("portfolio", pa, keys, years)
    if not len(t):
        return []
    row, labels = t.iloc[0], dict(HEADLINE)
    out = [{"label": "Measures", "value": f"{int(row['Measures']):,}", "help": "Measures in the tables below"}]
    return out + [{"label": labels[k], "value": rv.format_value(cat[k], row[k]) or "–", "help": cat[k].help or cat[k].label} for k in keys]


def sector_bars(res, pa: str, years=None, limit: int = 12) -> dict | None:
    """Total system benefit by program sector, as bars (widths in percent of the largest value; negative values marked in words)."""
    rv = engine()
    cat = res.catalog()
    key = "TotalSystemBenefit"
    if key not in cat:
        return None
    t = res.table("sector", pa, [key], years)
    if len(t) < 2 or key not in t:
        return None
    t = t.sort_values(key, ascending=False).head(limit)
    top = max(float(t[key].abs().max()), 1e-9)
    bars = [
        {
            "name": str(r["Program sector"]) or "(blank)",
            "value": rv.format_value(cat[key], r[key]),
            "pct": round(100 * abs(float(r[key])) / top, 1),
            "negative": float(r[key]) < 0,
        }
        for _, r in t.iterrows()
    ]
    return {"title": "Total system benefit by program sector", "bars": bars}


def picker_groups(cat) -> list[tuple[str, list]]:
    """The column picker's groups in the engine's order: [(group, [(key, label, help)])]."""
    rv = engine()
    out: dict[str, list] = {g: [] for g in rv.GROUP_ORDER}
    for k, c in cat.items():
        out[rv.column_group(c)].append((k, rv.display_label(c), c.help or f"Output column {k}, summed over the measures in each row."))
    return [(g, items) for g, items in out.items() if items]


def sort_rows(table, column: str, descending: bool):
    """The table sorted on one of its columns (numbers as numbers, blanks last), or unchanged for an unknown column."""
    if column not in table.columns:
        return table
    return table.sort_values(column, ascending=not descending, na_position="last", kind="stable")


def filter_rows(table, text: str):
    """The rows whose text columns contain `text` (case ignored)."""
    text = (text or "").strip().lower()
    if not text or not len(table):
        return table
    import pandas as pd

    textual = [c for c in table.columns if not pd.api.types.is_numeric_dtype(table[c])]
    if not textual:
        return table
    hit = table[textual].astype(str).apply(lambda col: col.str.lower().str.contains(text, regex=False)).any(axis=1)
    return table[hit]
