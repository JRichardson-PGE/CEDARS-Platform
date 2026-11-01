"""Record-level downloads (BR-07.5, BR-07.2): the published measure-level records a query keeps, with the columns a person picks, as a
page of a hundred and as one file (CSV, XLSX or Parquet) for every PA together, not one file per IOU.

The rows are the dashboard query's own (`query.where_terms`: dataset, filing, years, filters, exclusions), so a download of a
dashboard row's records holds exactly the records behind it. Columns come from the release's snapshots as the runs wrote them, under
their canonical names, grouped as the data dictionary groups them; a field the dictionary does not mark public is never offered.
Every file is stamped like the dashboard downloads (release, as-of time, snapshots, the PAs and filings read, filters, exclusions,
columns, dictionary version), so it can be traced to exactly what it holds.

Column choices are GET parameters, kept short so a link to any choice fits in a URL: `columns` (key, the default; all; none) is the
starting set, `colgroup` adds a whole dictionary group, `col` one column and `drop` removes one. `encode` writes the shortest such
set for a choice, which is what the page's form redirects to."""

import json
from dataclasses import dataclass, field
from datetime import date

from django.utils import timezone
from django.utils.text import slugify

from . import query as dq
from .downloads import FORMATS  # noqa: F401 - the formats a records file comes in, as the dashboard downloads

SOURCE = "Where the record came from"
FILED_BY, FILING, COUNTED_PA = "FiledBy", "Filing", "CountedPA"
SOURCE_COLUMNS = {
    FILED_BY: "The PA whose published filing or claim holds the record, under its current code.",
    FILING: "The filing or claim the record was published in (a budget filing's cycle and kind, or a claim's kind).",
    COUNTED_PA: "The PA the dashboards count the record under: the credited PA, else the filer, under its current code. A record "
    "filed under a former code (EBCE) is counted under the PA's current one (AVA); CreditedPA keeps the code as filed.",
}
# The default columns (ReCET's "Key results" output template, with the record's program, measure and year): the ones present are used.
KEY_COLUMNS = (
    FILED_BY,
    FILING,
    "CEInputID",
    "PrgID",
    "Prg_ProgramName",
    "MeasureID",
    "MeasDescription",
    "ClaimYearQuarter",
    "Used_Year",
    "CreditedPA",
    COUNTED_PA,
    "UseCategory",
    "Used_ACCVersion",
    "TotalSystemBenefit",
    "ElecBen",
    "GasBen",
    "OtherBen",
    "TRCCost",
    "PACCost",
    "TRCRatio",
    "PACRatio",
    "FirstYearNetKWh",
    "FirstYearNetKW",
    "FirstYearNetThm",
    "LifecycleNetElecCO2",
    "LifecycleNetGasCO2",
)
PRESETS = ("key", "all", "none")
COLUMN_PARAMS = ("columns", "colgroup", "col", "drop")
TEXT_FILTERS = ("program", "measure", "use_category")  # typed as comma-separated lists
# The filters the page's form shows; any other filter rides along as a hidden field.
FORM_FILTERS = ("pa_type", "pa", "program", "measure", "sector", "segment", "implementer_type", "measure_type", "use_category")
DATASETS = {"claims": "Claimed", "forecast": "Forecast", "monthly": "Monthly reported"}
FORMAT_LABELS = {"csv": "CSV", "xlsx": "Excel (XLSX)", "parquet": "Parquet"}
XLSX_MAX_ROWS = 1_048_575  # a worksheet's rows, less the header
BATCH_ROWS = 50_000


@dataclass(frozen=True)
class Column:
    name: str
    group: str
    definition: str = ""


@dataclass
class Group:
    label: str
    slug: str
    columns: list[Column] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class Catalog:
    """The columns a set of snapshots offers, in groups, in the order the runs write them; `present` is every column any file has."""

    groups: list[Group]
    present: set[str]
    dictionary: str = ""  # the dictionary version the groups and definitions come from

    @property
    def columns(self) -> list[Column]:
        return [c for g in self.groups for c in g.columns]

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    def group(self, slug: str) -> Group | None:
        return next((g for g in self.groups if g.slug == slug), None)

    @property
    def key(self) -> list[str]:
        names = set(self.names)
        return [c for c in KEY_COLUMNS if c in names]


def _group_of(name: str, f) -> str:
    if f is not None and f.group:
        if f.kind == "input":
            return {"Measure": "Measure inputs", "ProgramCost": "Program cost inputs"}.get(f.group, f"{f.group} inputs")
        return f.group
    if name.startswith("Prg_"):
        return "Program metadata"
    if name.startswith("Legacy_"):
        return "Legacy CEDARS columns"
    return "Other columns"


def catalog(files: list[dict]) -> Catalog:
    """The columns of the snapshots a query reads. A column the dictionary marks restricted or confidential is left out."""
    import pyarrow.parquet as pq

    from cedars.services.dictionary import service as dictionary
    from cedars.services.dictionary.models import Field

    names: dict[str, None] = {}
    for f in files:
        names |= dict.fromkeys(pq.read_schema(dq.local_path(f)).names)
    version = dictionary.current()
    fields = {f.name.lower(): f for f in Field.objects.filter(version=version)} if version else {}
    groups: dict[str, Group] = {SOURCE: Group(SOURCE, slugify(SOURCE), [Column(n, SOURCE, d) for n, d in SOURCE_COLUMNS.items()])}
    for n in names:
        if n.startswith("_") or n in SOURCE_COLUMNS:
            continue
        f = fields.get(n.lower())
        if f is not None and f.audience != Field.Audience.PUBLIC:
            continue
        label = _group_of(n, f)
        groups.setdefault(label, Group(label, slugify(label))).columns.append(Column(n, label, f.definition if f else ""))
    return Catalog(list(groups.values()), set(names), version.label if version else "")


# --- the choice of columns, as GET parameters


def _many(params, key) -> list[str]:
    if hasattr(params, "getlist"):
        values = params.getlist(key)
    else:
        values = params.get(key) or []
        values = [values] if isinstance(values, str) else list(values)
    return [v.strip() for v in values if v and v.strip()]


def chosen(cat: Catalog, params) -> tuple[list[str], list[str]]:
    """The columns chosen, in the catalog's order, and the names asked for that these records do not have."""
    preset = (_many(params, "columns") or ["key"])[-1]
    names = cat.names
    picked = set(names if preset == "all" else [] if preset == "none" else cat.key)
    unknown = []
    for slug in _many(params, "colgroup"):
        g = cat.group(slug)
        picked |= set(g.names) if g else set()
    for n in _many(params, "col"):
        if n in names:
            picked.add(n)
        else:
            unknown.append(n)
    picked -= set(_many(params, "drop"))
    return [n for n in names if n in picked], unknown


def encode(cat: Catalog, columns: list[str]) -> dict[str, list[str]]:
    """The shortest parameters that choose `columns`: from the key columns (a few added or dropped) or from none, by whole groups
    (a group mostly chosen is added whole, less what is not)."""
    want = set(columns)
    if want == set(cat.names):
        return {"columns": ["all"]}
    key = set(cat.key)
    from_key = {"col": [n for n in cat.names if n in want - key], "drop": [n for n in cat.names if n in key - want]}
    from_none: dict[str, list[str]] = {"columns": ["none"], "colgroup": [], "col": [], "drop": []}
    for g in cat.groups:
        have = [n for n in g.names if n in want]
        if len(have) == len(g.names):
            from_none["colgroup"].append(g.slug)
        elif len(have) * 2 > len(g.names):
            from_none["colgroup"].append(g.slug)
            from_none["drop"] += [n for n in g.names if n not in want]
        else:
            from_none["col"] += have
    best = min(
        ({k: v for k, v in from_key.items() if v}, {k: v for k, v in from_none.items() if v}),
        key=lambda p: sum(len(k) + len(x) + 2 for k, v in p.items() for x in v),
    )
    return best


# --- the query


@dataclass
class RecordQuery:
    query: dq.Query  # the rows (its `by` is always empty); its metrics are the totals the page shows
    params: dict  # the column parameters as given
    default_columns: bool = False  # the columns are the person's own default (no column parameter was given)


def from_params(params, *, today: date | None = None, default_columns: list[str] | None = None) -> RecordQuery:
    """The records a GET asks for. Nothing is excluded unless asked (record downloads hold every published record); with no
    parameters at all, the current year (BR-07.7). With no column parameter, `default_columns` (a person's own default, BR-07.4) are
    the columns instead of the key ones."""
    raw = {k: _many(params, k) for k in (params.keys() if hasattr(params, "keys") else [])}
    for k in TEXT_FILTERS:
        raw[k] = [part.strip() for v in raw.get(k, []) for part in v.split(",") if part.strip()]
    raw = {k: v for k, v in raw.items() if v}
    raw.pop("by", None)
    if not raw:
        year = str((today or timezone.localdate()).year)
        raw = {"year_from": [year], "year_to": [year]}
    q = dq.from_params(raw, exclude=())
    columns = {k: raw[k] for k in COLUMN_PARAMS if k in raw}
    if not columns and default_columns:
        return RecordQuery(q, {"columns": ["none"], "col": list(default_columns)}, default_columns=True)
    return RecordQuery(q, columns)


def query_string(q: dq.Query, cat: Catalog | None = None, columns: list[str] | None = None, *, preset: str = "") -> str:
    """The URL query for these records and columns, or a preset set of columns (each exclusion stated, since records exclude nothing
    by default)."""
    from urllib.parse import urlencode

    p = q.params(defaults_exclude=())
    p.pop("by", None)
    if preset:
        p["columns"] = [preset]
    elif cat is not None and columns is not None:
        p |= encode(cat, columns)
    return urlencode(p, doseq=True)


def behind(result: dq.Result, label: str, *, max_rows: int) -> dict:
    """The record-level download behind one dashboard series (BR-07.5): the records its rows add up, under the same query with every
    exclusion stated (records leave nothing out unless asked, so the file matches the totals shown), with the key columns. Up to
    `max_rows` records the files are made while the person waits; more are made in the background (`jobs.request`, from `query`)."""
    q = result.query.but(by=())
    qs = query_string(q)
    count = result.totals.get("_rows", 0) if result.files else 0
    files = [(f, FORMAT_LABELS[f], f"/data/records/download.{f}?{qs}") for f in FORMAT_LABELS if f != "xlsx" or count <= XLSX_MAX_ROWS]
    return {
        "series": label,
        "count": count,
        "query": qs,
        "page": f"/data/records/?{qs}",
        "background": count > max_rows,
        "links": files if count <= max_rows else [],
        "formats": [(f, FORMAT_LABELS[f]) for f, _, _ in files],
    }


def _sql(q: dq.Query, files: list[dict], cat: Catalog, columns: list[str]) -> tuple[str, list]:
    """One SELECT over the release's files: the chosen columns under their own names, in their own types (the files are unioned by
    name, a column missing from a file reading as blank), the rows the query's WHERE terms keep, in CEInputID order."""
    ident, literal = dq._ident, dq._literal
    raw = " UNION ALL BY NAME ".join(
        f"SELECT *, {literal(f['organisation'])} AS _organisation, {literal(dq.filing_label(f))} AS _filing "
        f"FROM read_parquet({literal(str(dq.local_path(f)))})"
        for f in files
    )
    texts = dq.text_columns(q, ["CEInputID"])
    cols = [f"CAST({ident(c)} AS VARCHAR) AS {ident(c)}" if c in cat.present else f"CAST(NULL AS VARCHAR) AS {ident(c)}" for c in texts]
    cols += ["_organisation", "_filing"] + [f"{ident(c)} AS {ident('o:' + c)}" for c in columns if c not in SOURCE_COLUMNS]
    source = {FILED_BY: "_organisation", FILING: "_filing", COUNTED_PA: dq.pa_expression()}
    out = [f"{source[c]} AS {ident(c)}" if c in source else f"{ident('o:' + c)} AS {ident(c)}" for c in columns]
    where, args = dq.where_terms(q)
    sql = (
        f"WITH raw AS ({raw}), data AS (SELECT {', '.join(cols)} FROM raw) SELECT {', '.join(out)} FROM data"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + ' ORDER BY "CEInputID" NULLS LAST, _organisation, _filing'
    )
    return sql, args


@dataclass
class Page:
    """One page of records with the chosen columns."""

    result: dq.Result  # release, files, notes, and the metric totals and count over every matching record
    catalog: Catalog
    columns: list[str]
    unknown: list[str]
    rows: list[list]
    offset: int
    limit: int

    @property
    def count(self) -> int:
        return self.result.totals.get("_rows", 0)


def page(rq: RecordQuery, *, limit: int = 100, offset: int = 0) -> Page:
    result = dq.run(rq.query)
    cat = catalog(result.files)
    columns, unknown = chosen(cat, rq.params)
    rows: list[list] = []
    if limit and result.files and result.totals["_rows"] and columns:
        sql, args = _sql(rq.query, result.files, cat, columns)
        con = dq.connect()
        try:
            rows = [list(r) for r in con.execute(sql + " LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()]
        finally:
            con.close()
    return Page(result, cat, columns, unknown, rows, offset, limit)


# --- files


def stamp(p: Page) -> dict:
    """What a records file records about where its rows came from (BR-07.2), and which PAs it holds (BR-08.3)."""
    s = p.result.stamp()
    for k in ("metrics", "by"):
        s.pop(k, None)
    return s | {
        "kind": "records",
        "records": p.count,
        "pas": _pas(p),
        "columns": p.columns,
        "dictionary": p.catalog.dictionary,
        "query": query_string(p.result.query, p.catalog, p.columns),
        "generated_at": timezone.now().isoformat(),
        "notes": p.result.notes,
    }


def _pas(p: Page) -> dict[str, int]:
    """How many of the records each PA holds (the dashboards' PA: the credited PA, else the filer), so a file says which PAs it
    combines (BR-08.3)."""
    if not p.result.files or not p.count:
        return {}
    return {str(r["pa"]): r["_rows"] for r in dq.run(p.result.query.but(by=("pa",))).rows}


def filename(p: Page, fmt: str) -> str:
    release = p.result.release.number if p.result.release else 0
    return f"cedars_{p.result.query.dataset}_release{release}_records.{fmt}"


def write(p: Page, fmt: str, out) -> int:
    """Write every matching record (not only the page's) with the page's columns to the binary file `out`, in batches, so a large
    download never sits in memory. Returns the number of rows written."""
    meta = stamp(p)
    if not p.result.files or not p.count or not p.columns:
        import pyarrow as pa

        empty = pa.table({c: pa.array([], pa.string()) for c in p.columns}) if p.columns else pa.table({})
        return _writers[fmt](iter(empty.to_batches()), empty.schema, meta, out)
    sql, args = _sql(p.result.query, p.result.files, p.catalog, p.columns)
    con = dq.connect()
    try:
        rel = con.sql(sql, params=args)
        reader = (rel.to_arrow_reader if hasattr(rel, "to_arrow_reader") else rel.record_batch)(BATCH_ROWS)  # DuckDB 2 / 1: streamed
        return _writers[fmt](reader, reader.schema, meta, out)
    finally:
        con.close()


def _stamp_lines(meta: dict) -> list[tuple[str, str]]:
    return [(k, v if isinstance(v, str) else json.dumps(v, default=str)) for k, v in meta.items()]


def _csv(batches, schema, meta, out) -> int:
    import pyarrow.csv as pacsv

    out.write("".join(f"# {k}: {v}\n" for k, v in _stamp_lines(meta)).encode("utf-8"))
    n = 0
    with pacsv.CSVWriter(out, schema) as w:
        for b in batches:
            w.write_batch(b)
            n += b.num_rows
    return n


def _parquet(batches, schema, meta, out) -> int:
    import pyarrow.parquet as pq

    schema = schema.with_metadata({"cedars": json.dumps(meta, default=str)})
    n = 0
    with pq.ParquetWriter(out, schema) as w:
        for b in batches:
            w.write_batch(b.replace_schema_metadata(schema.metadata))
            n += b.num_rows
    return n


def _xlsx(batches, schema, meta, out) -> int:
    from cedars.core import workbooks

    book = workbooks.Book(
        f"CEDARS record download: {meta.get('dataset') or 'records'}",
        subject="Records from a CEDARS publication snapshot; what they are is on the About sheet",
        write_only=True,
    )
    data = book.sheet("Data", schema.names)
    n = 0
    for b in batches:
        cols = [b.column(i).to_pylist() for i in range(b.num_columns)]
        for row in zip(*cols, strict=True):
            data.append(row)
        n += b.num_rows
    book.sheet("About", ["Item", "Value"], _stamp_lines(meta), widths={0: 24, 1: 80})
    book.save(out)
    return n


_writers = {"csv": _csv, "xlsx": _xlsx, "parquet": _parquet}
