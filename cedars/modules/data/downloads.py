"""Stamped downloads (BR-07.2): a query's rows as CSV, XLSX or Parquet, with the release, its as-of time, the snapshots read, the
filters and exclusions, and each metric's canonical fields, so a file can be traced back to exactly what it shows.

Columns: the dimensions, each metric as shown (scaled, its unit in the heading), then each summed canonical field under its own name
and the number of records. CSV carries the stamp as "#" lines above the header; XLSX on an About sheet; Parquet in the schema's
"cedars" metadata.

A dashboard showing forecast and claims together downloads both in one file (Jake, 2026-11-02): the same query once per dataset, a
Series column first, then each series' rows and its totals row, and each series' stamp. The records behind the rows stay one
download per series."""

import csv
import io
import json
from dataclasses import replace
from urllib.parse import urlencode

from django.utils import timezone

from .query import Query, Result

FORMATS = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "parquet": "application/vnd.apache.parquet",
}


SERIES_LABELS = {"forecast": "Forecast", "claims": "Claimed", "monthly": "Monthly"}  # dashboard.SERIES' names, by dataset


def for_dataset(q: Query, dataset: str) -> Query:
    """The query for one series of a combined download: the forecast filing applies to the forecast only (dashboard.View.for_series)."""
    return replace(q, dataset=dataset, filing=q.filing if dataset == "forecast" else "")


def links(queries: list[Query], defaults_exclude) -> list[dict]:
    """The page's "Download these rows": one file per format holding every series shown, or one per series when their queries differ
    in more than the dataset and the forecast filing (they never do on today's pages; the fallback keeps every download exact)."""
    if not queries:
        return []
    datasets = [q.dataset for q in queries]
    if len(queries) > 1 and all(q == for_dataset(queries[0], q.dataset) for q in queries) and len(set(datasets)) == len(datasets):
        params = queries[0].params(defaults_exclude) | {"dataset": datasets}
        if any(q.filing for q in queries):
            params["filing"] = [next(q.filing for q in queries if q.filing)]
        groups = [(" and ".join(SERIES_LABELS.get(d, d) for d in datasets).capitalize(), urlencode(params, doseq=True))]
    else:
        groups = [(SERIES_LABELS.get(q.dataset, q.dataset), q.query_string(defaults_exclude)) for q in queries]
    return [{"series": label, "links": [(fmt.upper(), f"/data/download.{fmt}?{qs}") for fmt in FORMATS]} for label, qs in groups]


def table(result: Result, totals: bool = True) -> tuple[list[str], list[list]]:
    """The download's header and rows, the totals row last when the rows are grouped (not in Parquet, which keeps one type per column)."""
    header = [d.label for d in result.dimension_list]
    header += [f"{m.label} ({m.unit})" if m.unit else m.label for m in result.metric_list]
    header += result.fields + ["Records"]
    rows = [_row(result, r) for r in result.rows]
    if totals and result.query.by and result.rows:
        total = dict(result.totals)
        rows.append(_row(result, total, label="Total"))
    return header, rows


def _row(result: Result, r: dict, label: str | None = None) -> list:
    dims = [label if label and i == 0 else (None if label else r.get(d.code)) for i, d in enumerate(result.dimension_list)]
    return dims + [r.get(m.code) for m in result.metric_list] + [r["_sums"].get(f) for f in result.fields] + [r["_rows"]]


def filename(result: Result, fmt: str) -> str:
    release = result.release.number if result.release else 0
    by = "_".join(result.query.by) or "total"
    return f"cedars_{result.query.dataset}_release{release}_{by}.{fmt}"


def combined_table(results: list[Result], totals: bool = True) -> tuple[list[str], list[list]]:
    """Several series' rows in one table, a Series column first (the queries differ only in dataset, so their columns are the same)."""
    header, rows = None, []
    for r in results:
        h, body = table(r, totals)
        header = header or ["Series"] + h
        label = SERIES_LABELS.get(r.query.dataset, r.query.dataset)
        rows += [[label] + row for row in body]
    return header or [], rows


def combined_filename(results: list[Result], fmt: str) -> str:
    datasets = "_".join(r.query.dataset for r in results)
    releases = "-".join(str(r.release.number if r.release else 0) for r in results)
    by = "_".join(results[0].query.by) or "total"
    return f"cedars_{datasets}_release{releases}_{by}.{fmt}"


def render_combined(results: list[Result], fmt: str) -> bytes:
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}")
    header, rows = combined_table(results, totals=fmt != "parquet")
    stamp = {"dataset": " and ".join(r.query.dataset for r in results), "series": [SERIES_LABELS.get(r.query.dataset) for r in results]}
    for r in results:
        label = SERIES_LABELS.get(r.query.dataset, r.query.dataset)
        stamp |= {f"{label} {k}": v for k, v in r.stamp().items() if k != "dataset"}
        stamp[f"{label} notes"] = r.notes
    stamp["generated_at"] = timezone.now().isoformat()
    return {"csv": _csv, "xlsx": _xlsx, "parquet": _parquet}[fmt](header, rows, stamp)


def render(result: Result, fmt: str) -> bytes:
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}")
    header, rows = table(result, totals=fmt != "parquet")
    stamp = result.stamp() | {"generated_at": timezone.now().isoformat(), "notes": result.notes}
    return {"csv": _csv, "xlsx": _xlsx, "parquet": _parquet}[fmt](header, rows, stamp)


def _stamp_lines(stamp: dict) -> list[tuple[str, str]]:
    return [(k, v if isinstance(v, str) else json.dumps(v, default=str)) for k, v in stamp.items()]


def _csv(header, rows, stamp) -> bytes:
    out = io.StringIO()
    for k, v in _stamp_lines(stamp):
        out.write(f"# {k}: {v}\n")
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue().encode("utf-8")


def _xlsx(header, rows, stamp) -> bytes:
    from cedars.core import workbooks

    book = workbooks.Book(
        f"CEDARS {stamp.get('dataset', 'dashboard')} download",
        subject="A CEDARS dashboard's rows; what they are is on the About sheet",
        write_only=True,
    )
    book.sheet("Data", header, rows)
    book.sheet("About", ["Item", "Value"], _stamp_lines(stamp), widths={0: 24, 1: 80})
    return book.save()


def _parquet(header, rows, stamp) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    columns = list(zip(*rows, strict=True)) if rows else [[] for _ in header]
    names = _unique(header)
    t = pa.table({n: list(c) for n, c in zip(names, columns, strict=True)})
    t = t.replace_schema_metadata({"cedars": json.dumps(stamp, default=str)})
    out = io.BytesIO()
    pq.write_table(t, out)
    return out.getvalue()


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    return out
