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
CHUNK_CELLS = 2_000_000  # values (records x columns) read at a time for a download; see _chunk_rows


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


def _data(q: dq.Query, files: list[dict], cat: Catalog, columns: list[str], *, keys_only: bool) -> tuple[str, list[str], list]:
    """The release's files unioned by name (a column missing from a file reads as blank), as `data`: the columns the WHERE terms read
    as text, each record's filer and filing, and either its place (`_file`: the snapshot's index in `files`, `_row`: its row in that
    file) or the chosen columns under `o:<name>`. The WHERE terms are the query's."""
    ident, literal = dq._ident, dq._literal
    raw = " UNION ALL BY NAME ".join(
        f"SELECT *, {literal(f['organisation'])} AS _organisation, {literal(dq.filing_label(f))} AS _filing, {i} AS _file "
        f"FROM read_parquet({literal(str(dq.local_path(f)))}{', file_row_number=true' if keys_only else ''})"
        for i, f in enumerate(files)
    )
    texts = dq.text_columns(q, ["CEInputID"])
    cols = [f"CAST({ident(c)} AS VARCHAR) AS {ident(c)}" if c in cat.present else f"CAST(NULL AS VARCHAR) AS {ident(c)}" for c in texts]
    cols += ["_organisation", "_filing"]
    if keys_only:
        cols += ["_file", "file_row_number AS _row"]
    else:
        cols += [f"{ident(c)} AS {ident('o:' + c)}" for c in columns if c not in SOURCE_COLUMNS]
    where, args = dq.where_terms(q)
    return f"WITH raw AS ({raw}), data AS (SELECT {', '.join(cols)} FROM raw)", where, args


def _sql(q: dq.Query, files: list[dict], cat: Catalog, columns: list[str]) -> tuple[str, list]:
    """One SELECT over the release's files: the chosen columns under their own names, in their own types, the rows the query's WHERE
    terms keep, in CEInputID order. Only for its column types (`_types`): sorting every chosen column of every record at once runs
    past the query memory limit once the columns are many (`_keys` and `_fetch` page and stream instead)."""
    ident = dq._ident
    head, where, args = _data(q, files, cat, columns, keys_only=False)
    source = {FILED_BY: "_organisation", FILING: "_filing", COUNTED_PA: dq.pa_expression()}
    out = [f"{source[c]} AS {ident(c)}" if c in source else f"{ident('o:' + c)} AS {ident(c)}" for c in columns]
    sql = (
        f"{head} SELECT {', '.join(out)} FROM data"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + ' ORDER BY "CEInputID" NULLS LAST, _organisation, _filing'
    )
    return sql, args


def _types(con, q: dq.Query, files: list[dict], cat: Catalog, columns: list[str]) -> dict[str, str]:
    """Each chosen column's type in the files unioned by name (read from the files' schemas; no rows are read)."""
    sql, args = _sql(q, files, cat, columns)
    return {c[0]: c[1] for c in con.execute(f"DESCRIBE {sql}", args).fetchall()}


def _keys(con, q: dq.Query, files: list[dict], cat: Catalog, *, limit: int | None = None, offset: int = 0):
    """The matching records' places, in CEInputID order (an Arrow table: `_file`, `_row` and the source columns). Only the sort keys
    and the WHERE terms' columns are read, so it costs the same whatever columns are chosen."""
    ident = dq._ident
    head, where, args = _data(q, files, cat, [], keys_only=True)
    source = [f"_organisation AS {ident(FILED_BY)}", f"_filing AS {ident(FILING)}", f"{dq.pa_expression()} AS {ident(COUNTED_PA)}"]
    sql = (
        f"{head} SELECT _file, _row, {', '.join(source)} FROM data"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + ' ORDER BY "CEInputID" NULLS LAST, _organisation, _filing, _file, _row'
    )
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args = [*args, limit, offset]
    return con.sql(sql, params=args).to_arrow_table()


def _fetch(con, files: list[dict], cat: Catalog, columns: list[str], types: dict[str, str], keys):
    """The chosen columns of the records at `keys` (from `_keys`), in their order, as one Arrow table. Each file is read on its own,
    for its own rows only, so no query ever holds more than one file's reader and these rows."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    ident, literal = dq._ident, dq._literal
    keys = keys.append_column("_ord", pa.array(range(keys.num_rows), pa.int64()))
    parts = []
    for i in pc.unique(keys["_file"]).to_pylist():
        mine = keys.filter(pc.equal(keys["_file"], i)).select(["_ord", "_row", FILED_BY, FILING, COUNTED_PA])
        path = str(dq.local_path(files[i]))
        own = set(pq.read_schema(path).names)
        out = ["k._ord"]
        for c in columns:
            if c in SOURCE_COLUMNS:
                out.append(f"k.{ident(c)}")
            elif c in own:
                out.append(f"CAST(d.{ident(c)} AS {types[c]}) AS {ident(c)}")
            else:
                out.append(f"CAST(NULL AS {types[c]}) AS {ident(c)}")
        con.register("_keys", mine)
        try:
            parts.append(
                con.sql(
                    f"SELECT {', '.join(out)} FROM read_parquet({literal(path)}, file_row_number=true) d "
                    "JOIN _keys k ON d.file_row_number = k._row"
                ).to_arrow_table()
            )
        finally:
            con.unregister("_keys")
    table = pa.concat_tables(parts) if len(parts) > 1 else parts[0]
    return table.take(pc.sort_indices(table["_ord"])).drop_columns(["_ord"])


def _chunk_rows(columns: list[str]) -> int:
    """Records fetched at a time for a file: about CHUNK_CELLS values, so a wide choice of columns stays within the query memory."""
    return max(1_000, min(BATCH_ROWS, CHUNK_CELLS // max(1, len(columns))))


def rows(q: dq.Query, files: list[dict], cat: Catalog, columns: list[str], *, limit: int, offset: int = 0) -> list[list]:
    """One page of the matching records (`limit` from `offset`, in CEInputID order) with the chosen columns."""
    con = dq.connect()
    try:
        keys = _keys(con, q, files, cat, limit=limit, offset=offset)
        if not keys.num_rows:
            return []
        table = _fetch(con, files, cat, columns, _types(con, q, files, cat, columns), keys)
    finally:
        con.close()
    return [list(r.values()) for r in table.to_pylist()]


def batches(q: dq.Query, files: list[dict], cat: Catalog, columns: list[str]):
    """Every matching record with the chosen columns, in CEInputID order: the Arrow schema and an iterator of record batches. The
    records' places are sorted first (narrow), then their columns are read a chunk at a time, so neither the sort nor a chunk ever
    holds every column of every record."""
    import pyarrow as pa

    con = dq.connect()
    try:
        types = _types(con, q, files, cat, columns)
        keys = _keys(con, q, files, cat)
        step = _chunk_rows(columns)
        first = _fetch(con, files, cat, columns, types, keys.slice(0, step)) if keys.num_rows else None
    except BaseException:
        con.close()
        raise
    schema = first.schema if first is not None else pa.schema([(c, pa.string()) for c in columns])

    def gen():
        try:
            if first is None:
                return
            yield from first.to_batches()
            for start in range(step, keys.num_rows, step):
                yield from _fetch(con, files, cat, columns, types, keys.slice(start, step)).cast(schema).to_batches()
        finally:
            con.close()

    return schema, gen()


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
    if limit and result.files and result.totals["_rows"] and columns:
        found = rows(rq.query, result.files, cat, columns, limit=limit, offset=offset)
    else:
        found = []
    return Page(result, cat, columns, unknown, found, offset, limit)


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
    schema, stream = batches(p.result.query, p.result.files, p.catalog, p.columns)
    try:
        return _writers[fmt](stream, schema, meta, out)
    finally:
        stream.close()


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
