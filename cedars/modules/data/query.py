"""The query layer: one query, over one release of one dataset, gives every number a page, its chart, its table and its download show,
so their totals cannot disagree (spec "Foundations"). DuckDB reads the release's Parquet snapshots (decision I); each query opens its
own in-memory connection, so requests never share state.

A query sums the metric catalog's fields by the dimensions asked for, over the rows its filters and exclusions keep. Its state is
plain GET parameters (`from_params` / `Query.params`), so a link reproduces the view.

Datasets: claims (each PA's latest year-to-date claim per year), forecast (budget filings; the filing selector picks one kind, or by
default each PA's latest filing in each cycle) and monthly (each PA's latest year-to-date report per year)."""

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import url2pathname

from django.conf import settings

from cedars.core import storage
from cedars.services.snapshots import service as snapshots
from cedars.services.snapshots.models import Dataset, Release

from . import metrics as catalog


@dataclass(frozen=True)
class Dimension:
    code: str
    label: str
    column: str  # the canonical field it reads ("" for one built from the release itself)
    expression: str = ""  # SQL over the unioned rows; default the column as text

    @property
    def in_sentence(self) -> str:
        """The label inside a sentence ("by primary sector", "by PA type"): lower case, except an acronym that starts it."""
        first, _, rest = self.label.partition(" ")
        return f"{first if first.isupper() else first.lower()}{' ' if rest else ''}{rest.lower()}"


DIMENSIONS = {
    d.code: d
    for d in [
        # Built at query time from each PA's type in the platform (pa_type_expression); a PA with no type reads as blank.
        Dimension("pa_type", "PA type", "CreditedPA"),
        # As filed (the credited PA, else the filer), read under its party's current code at query time (pa_expression): a former
        # code (EBCE) counts as the PA's current one (AVA).
        Dimension("pa", "PA", "CreditedPA", "coalesce(nullif(trim(CreditedPA), ''), _organisation)"),
        Dimension("segment", "Segment", "Prg_ProgramSegment"),
        Dimension("sector", "Primary sector", "Prg_PrimarySector"),
        Dimension("implementer_type", "Implementer type", "Prg_ProgramImplementer"),
        Dimension("use_category", "Measure use category", "UseCategory"),
        Dimension("year", "Year", "Used_Year", "try_cast(Used_Year AS INTEGER)"),
        Dimension("program", "Program", "PrgID"),
        Dimension("program_name", "Program name", "Prg_ProgramName"),
        Dimension("category", "Program category", "Prg_ProgramCategory"),
        Dimension("measure", "Measure", "MeasureID"),
        Dimension(
            "measure_type",
            "Measure type",
            "Fuel_Sub_Flag",
            "CASE WHEN lower(trim(Fuel_Sub_Flag)) IN ('true', '1', 'yes', 'y') THEN 'Fuel substitution' ELSE 'Energy efficiency' END",
        ),
        Dimension("organisation", "Filed by", "", "_organisation"),
        Dimension("filing", "Filing", "", "_filing"),
    ]
}
# The view-by choices the pages offer (Jake, 2026-10-31; PA type added with the PA types the same day); the rest are for drill-down,
# filters and downloads.
VIEW_BY = ("pa", "pa_type", "segment", "sector", "implementer_type", "use_category")
# A PA filter and a PA type filter make one selection, either matching (Jake, 2026-10-31: "all RENs" and PG&E is the RENs and PG&E),
# not two filters that must both match.
PA_SELECTION = ("pa_type", "pa")


@dataclass(frozen=True)
class Exclusion:
    code: str
    label: str
    column: str
    values: tuple[str, ...]  # compared trimmed and case-insensitively


EXCLUSIONS = {
    e.code: e
    for e in [
        Exclusion("pension", "Pension and Benefits programs", "Prg_ProgramCategory", ("Pension and Benefits",)),
        Exclusion("esa", "Energy Savings Assistance programs", "Prg_ProgramCategory", ("Energy Savings Assistance",)),
        Exclusion("cs", "Codes and standards", "Prg_ProgramSegment", ("codes_and_standards", "Codes and Standards")),
        Exclusion("no_ce", "Programs excluded from cost-effectiveness", "Prg_Exclude_From_CE", ("true", "1", "yes", "y", "t")),
    ]
}
# As the Power BI and CEDARS' pages have them (Jake, 2026-10-31): Pension and Benefits always out, C&S out on most pages, ESA out on
# the goals page only; programs flagged Exclude_From_CE out of the cost-effectiveness page (Jake, 2026-10-31). A page passes its own
# defaults to from_params; the page and the download say what was left out.
DEFAULT_EXCLUDE = ("pension", "cs")


class QueryError(ValueError):
    """A query that cannot run as asked (unknown metric or dimension, bad years). Its message is safe to show."""


@dataclass(frozen=True)
class Query:
    dataset: str
    metrics: tuple[str, ...]
    by: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    filters: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (dimension, values), each kept as given
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    filing: str = ""  # forecast: a filing kind ("TUAL") or "<cycle>|<kind>" ("2028-2031|TUAL"); blank = each PA's latest
    release: int | None = None  # an earlier release's number; None = the current one

    def __post_init__(self):
        if self.dataset not in Dataset.values:
            raise QueryError(f"unknown dataset {self.dataset!r}")
        if not self.metrics:
            raise QueryError("no metric asked for")
        for m in self.metrics:
            catalog.get(m)
        for d in self.by + tuple(d for d, _ in self.filters):
            if d not in DIMENSIONS:
                raise QueryError(f"unknown dimension {d!r}")
        for e in self.exclude:
            if e not in EXCLUSIONS:
                raise QueryError(f"unknown exclusion {e!r}")
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise QueryError("the first year is after the last")

    @property
    def filter_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.filters)

    def params(self, defaults_exclude=DEFAULT_EXCLUDE) -> dict[str, list[str]]:
        """The GET parameters that reproduce this query (from_params' inverse)."""
        p: dict[str, list[str]] = {"dataset": [self.dataset], "metric": list(self.metrics)}
        if self.by:
            p["by"] = list(self.by)
        if self.year_from:
            p["year_from"] = [str(self.year_from)]
        if self.year_to:
            p["year_to"] = [str(self.year_to)]
        for d, values in self.filters:
            p[d] = list(values)
        for code in EXCLUSIONS:
            if (code in self.exclude) != (code in defaults_exclude):
                p[code] = ["exclude" if code in self.exclude else "include"]
        if self.filing:
            p["filing"] = [self.filing]
        if self.release:
            p["release"] = [str(self.release)]
        return p

    def query_string(self, defaults_exclude=DEFAULT_EXCLUDE) -> str:
        return urlencode(self.params(defaults_exclude), doseq=True)

    def but(self, **changes) -> "Query":
        return replace(self, **changes)


def _year(value: str | None, name: str) -> int | None:
    if not value:
        return None
    try:
        year = int(value)
    except ValueError as e:
        raise QueryError(f"{name} must be a year") from e
    if not 1990 <= year <= 2100:
        raise QueryError(f"{name} must be a year")
    return year


def from_params(params, *, dataset: str | None = None, metrics: tuple[str, ...] = ("tsb",), exclude=DEFAULT_EXCLUDE) -> Query:
    """A query from GET parameters (a QueryDict or a dict of lists). `metric` names catalog codes or view-control bases ("kwh") that
    `gross=1` and `lifecycle=1` resolve; each exclusion is a parameter set to include or exclude, defaulting to `exclude`."""

    def many(key):
        if hasattr(params, "getlist"):
            values = params.getlist(key)
        else:
            values = params.get(key) or []
            values = [values] if isinstance(values, str) else list(values)
        return [v.strip() for v in values if v and v.strip()]

    def one(key):
        values = many(key)
        return values[-1] if values else ""

    gross, lifecycle = one("gross") in ("1", "true", "on"), one("lifecycle") in ("1", "true", "on")
    codes = []
    for m in many("metric") or list(metrics):
        try:
            codes.append(catalog.resolve(m, gross=gross, lifecycle=lifecycle).code)
        except ValueError as e:
            raise QueryError(str(e)) from e
    excluded = set(exclude)
    for code in EXCLUSIONS:
        choice = one(code)
        if choice == "include":
            excluded.discard(code)
        elif choice == "exclude":
            excluded.add(code)
    release = one("release")
    return Query(
        dataset=dataset or one("dataset") or "claims",
        metrics=tuple(dict.fromkeys(codes)),
        by=tuple(dict.fromkeys(many("by"))),
        year_from=_year(one("year_from"), "year_from"),
        year_to=_year(one("year_to"), "year_to"),
        # "filing" names the forecast filing to read (below), never a filter on the filing dimension's label
        filters=tuple((d, tuple(dict.fromkeys(many(d)))) for d in DIMENSIONS if d != "filing" and many(d)),
        exclude=tuple(c for c in EXCLUSIONS if c in excluded),
        filing=one("filing"),
        release=int(release) if release.isdigit() else None,
    )


# --- results


@dataclass
class Result:
    query: Query
    release: Release | None
    files: list[dict]  # the snapshots read (snapshots.release_files entries)
    rows: list[dict]  # the dimensions by code, each metric by code, and "_sums" (each field's sum) and "_rows"
    totals: dict  # each metric over every row kept, and "_sums" and "_rows"
    notes: list[str] = field(default_factory=list)
    used: set = field(default_factory=set)  # the snapshots that gave rows (a file read but matched by nothing is not one of them)

    @property
    def files_used(self) -> list[dict]:
        """The snapshots that contributed rows, when known: a one-PA download names that PA's filings, not every filing read."""
        return [f for f in self.files if f["snapshot"] in self.used] if self.used else self.files

    @property
    def as_of(self):
        return self.release.created_at if self.release else None

    @property
    def metric_list(self) -> list[catalog.Metric]:
        return [catalog.get(m) for m in self.query.metrics]

    @property
    def dimension_list(self) -> list[Dimension]:
        return [DIMENSIONS[d] for d in self.query.by]

    @property
    def fields(self) -> list[str]:
        return list(dict.fromkeys(f for m in self.metric_list for f in m.fields))

    def stamp(self) -> dict:
        """What a download records about where its rows came from (BR-07.2)."""
        q = self.query
        return {
            "dataset": q.dataset,
            "release": self.release.number if self.release else None,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "snapshots": [f["snapshot"] for f in self.files_used],
            "filings": sorted(
                {f"{f['organisation']} {f['cycle'] or f['year']} {f['kind']} v{f['version_number']}" for f in self.files_used}
            ),
            "metrics": {
                m.code: {"label": m.label, "unit": m.unit, "numerator": m.numerator, "denominator": m.denominator} for m in self.metric_list
            },
            "by": list(q.by),
            "years": [q.year_from, q.year_to],
            "filters": {d: list(v) for d, v in q.filters},
            "excluded": [EXCLUSIONS[e].label for e in q.exclude],
            "filing": q.filing,
            "query": q.query_string(),
        }

    def stamp_json(self) -> str:
        return json.dumps(self.stamp(), default=str)


def _release(q: Query) -> Release | None:
    if q.release:
        return Release.objects.filter(dataset=q.dataset, number=q.release).first()
    return snapshots.current_release(q.dataset)


def pick_files(q: Query, files: list[dict]) -> list[dict]:
    """The release's snapshots a query reads. Claims and monthly: all of them (one per PA and year already). Forecast: the filing
    asked for, else each PA's latest filing (by its due date) in each cycle."""
    if q.dataset != Dataset.FORECAST:
        return files
    if q.filing:
        cycle, _, kind = q.filing.rpartition("|")
        return [f for f in files if f["kind"].lower() == kind.lower() and (not cycle or f["cycle"] == cycle)]
    latest: dict[tuple, dict] = {}
    for f in files:
        slot = (f["organisation"], f["cycle"])
        if slot not in latest or (f["due_at"], f["version"]) > (latest[slot]["due_at"], latest[slot]["version"]):
            latest[slot] = f
    return sorted(latest.values(), key=lambda f: (f["organisation"], f["cycle"]))


def local_path(f: dict) -> Path:
    """A snapshot's file on this machine: local storage's own file, else a copy in DATA_CACHE named by its SHA-256 (a snapshot never
    changes, so a copy is good for as long as it exists)."""
    uri = f["uri"]
    if uri.startswith("file://"):
        return Path(url2pathname(urlparse(uri).path))
    cache = Path(settings.DATA_CACHE)
    path = cache / f"{f['sha256']}.parquet"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        with storage.store(snapshots.AREA).open(f["key"]) as src, open(tmp, "wb") as dst:
            while block := src.read(1 << 22):
                dst.write(block)
        tmp.replace(path)
    return path


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _start(q: Query) -> Result:
    """The result's release, files and notes, before any reading. No rows when nothing is published or no file matches."""
    release = _release(q)
    if release is None:
        return Result(q, None, [], [], {}, ["Nothing has been published in this dataset yet."])
    files = pick_files(q, snapshots.release_files(release))
    result = Result(q, release, files, [], {"_sums": {}, "_rows": 0})
    result.notes += [f"Left out: {EXCLUSIONS[e].label}." for e in q.exclude]
    if not files:
        result.notes.append("No published filing matches this choice." if q.filing else "Nothing in force in this release.")
    return result


def files_of(q: Query) -> list[dict]:
    """The snapshots a query reads (none when nothing is published)."""
    return _start(q).files


def _source(q: Query, files: list[dict], numbers: list[str], extra_texts: list[str] = ()) -> tuple[str, list[str], list]:
    """The rows a query reads: the release's files unioned under one set of columns (a missing column reads as blank), and its WHERE
    terms (years, filters, exclusions) with their arguments."""
    import pyarrow.parquet as pq

    texts = [c for c in text_columns(q, extra_texts) if c not in numbers]
    selects = []
    for f in files:
        names = set(pq.read_schema(local_path(f)).names)
        cols = [
            f"try_cast({_ident(c)} AS DOUBLE) AS {_ident(c)}" if c in names else f"CAST(NULL AS DOUBLE) AS {_ident(c)}" for c in numbers
        ]
        cols += [f"CAST({_ident(c)} AS VARCHAR) AS {_ident(c)}" if c in names else f"CAST(NULL AS VARCHAR) AS {_ident(c)}" for c in texts]
        cols += [f"{_literal(f['organisation'])} AS _organisation", f"{_literal(filing_label(f))} AS _filing"]
        cols += [f"{_literal(str(f['snapshot']))} AS _snapshot"]
        selects.append(f"SELECT {', '.join(cols)} FROM read_parquet({_literal(str(local_path(f)))})")
    where, args = where_terms(q)
    source = f"WITH data AS ({' UNION ALL '.join(selects)}) "
    return source, where, args


def text_columns(q: Query, extra: list[str] = ()) -> list[str]:
    """The columns a query's WHERE terms and dimensions read as text: they must be in the source rows under their own names."""
    return list(
        dict.fromkeys(
            [DIMENSIONS[d].column for d in q.by + tuple(d for d, _ in q.filters) if DIMENSIONS[d].column]
            + [EXCLUSIONS[e].column for e in q.exclude]
            + ["Used_Year", "CreditedPA", *extra]
        )
    )


def where_terms(q: Query) -> tuple[list[str], list]:
    """A query's WHERE terms (years, filters, exclusions) and their arguments, over rows that carry `text_columns(q)` as text and
    _organisation / _filing."""
    where, args = [], []
    year = DIMENSIONS["year"].expression
    if q.year_from:
        where.append(f"{year} >= ?")
        args.append(q.year_from)
    if q.year_to:
        where.append(f"{year} <= ?")
        args.append(q.year_to)
    either = []  # the PA selection's terms, joined by OR
    for d, values in q.filters:
        expr = _expression(DIMENSIONS[d])
        if d == "year":
            where.append(f"{expr} IN ({', '.join('?' for _ in values)})")
            args += [int(v) for v in values if v.lstrip("-").isdigit()] or [None]
        elif d in PA_SELECTION:
            if d == "pa":  # an old link's former code selects the PA it belongs to
                values = list(dict.fromkeys(current_codes(values)))
            either.append((f"lower(trim({expr})) IN ({', '.join('?' for _ in values)})", [v.strip().lower() for v in values]))
        else:
            if d == "organisation":
                values = list(dict.fromkeys(current_codes(values)))
            where.append(f"lower(trim({expr})) IN ({', '.join('?' for _ in values)})")
            args += [v.strip().lower() for v in values]
    if either:
        where.append("(" + " OR ".join(term for term, _ in either) + ")")
        args += [a for _, values in either for a in values]
    for e in q.exclude:
        ex = EXCLUSIONS[e]
        where.append(f"coalesce(lower(trim({_ident(ex.column)})), '') NOT IN ({', '.join('?' for _ in ex.values)})")
        args += [v.lower() for v in ex.values]
    return where, args


def filing_label(f: dict) -> str:
    """A snapshot's filing as the pages and files name it ("2028-2031 TUAL", or a claim's kind)."""
    return f"{f['cycle']} {f['kind']}".strip() if f["cycle"] else f["kind"]


def connect():
    """A DuckDB connection of the request's own, within the query limits (it spills to disk beyond the memory limit)."""
    import duckdb

    return duckdb.connect(config={"threads": settings.DATA_QUERY_THREADS, "memory_limit": settings.DATA_QUERY_MEMORY})


def _fetch(sql: str, args: list) -> tuple[list[str], list[tuple]]:
    con = connect()
    try:
        cursor = con.execute(sql, args)
        return [c[0] for c in cursor.description], cursor.fetchall()
    finally:
        con.close()


def run(q: Query) -> Result:
    result = _start(q)
    if not result.files:
        _finish(result)
        return result
    numbers = result.fields
    source, where, args = _source(q, result.files, numbers)
    dims = [f"{_expression(DIMENSIONS[d])} AS {_ident(d)}" for d in q.by]
    sums = [f"sum({_ident(c)}) AS {_ident(c)}" for c in numbers]
    # grouped by snapshot as well, so the result knows which filings gave it rows; the groups are added up here
    groups = [_ident(d) for d in q.by] + ["_snapshot"]
    sql = (
        source
        + f"SELECT {', '.join(dims + ['_snapshot'] + sums + ['count(*) AS _rows'])} FROM data"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + f" GROUP BY {', '.join(groups)} ORDER BY {', '.join(f'{g} NULLS LAST' for g in groups)}"
    )
    names, raw = _fetch(sql, args)
    rows: dict[tuple, dict] = {}
    for values in raw:
        record = dict(zip(names, values, strict=True))
        if not record["_rows"]:
            continue
        result.used.add(record["_snapshot"])
        key = tuple(record[d] for d in q.by)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {d: record[d] for d in q.by} | {"_sums": dict.fromkeys(numbers, 0), "_rows": 0}
        for c in numbers:
            row["_sums"][c] += record[c] or 0
        row["_rows"] += record["_rows"]
    result.rows = list(rows.values())
    _finish(result)
    return result


def _expression(d: Dimension) -> str:
    if d.code == "pa_type":
        return pa_type_expression()
    if d.code == "pa":
        return pa_expression()
    return d.expression or _ident(d.column)


def current_codes(codes) -> list[str]:
    """PA codes under their party's current codes (EBCE -> AVA); other codes as given."""
    from cedars.services.accounts import service as access

    mapping = access.code_map()
    return [access.current_code(c, mapping) for c in codes]


def pa_expression(mapping: dict[str, str] | None = None) -> str:
    """Each row's PA (the credited PA as filed, else the filer) under its party's current code: a CASE over the former codes (and
    merged organizations' codes), read when the query runs so a rename recorded since counts at once. With no renames, the PA as filed."""
    from cedars.services.accounts import service as access

    raw = DIMENSIONS["pa"].expression
    mapping = access.code_map() if mapping is None else mapping
    if not mapping:
        return raw
    whens = " ".join(f"WHEN {_literal(old)} THEN {_literal(new)}" for old, new in sorted(mapping.items()))
    return f"CASE upper(trim({raw})) {whens} ELSE {raw} END"


def pa_type_expression(types: dict[str, str] | None = None) -> str:
    """The PA type of each row's PA (the pa dimension, under current codes), from each PA's type in the platform: a CASE over the typed
    PA codes, read when the query runs so a PA typed since reads as typed. A PA with no type (or no PA) is NULL."""
    from cedars.services.accounts import service as access

    types = access.pa_types() if types is None else types
    if not types:
        return "CAST(NULL AS VARCHAR)"
    pa = f"upper(trim({pa_expression()}))"
    whens = " ".join(f"WHEN {_literal(code.upper())} THEN {_literal(t)}" for code, t in sorted(types.items()))
    return f"CASE {pa} {whens} END"


def selected_pas(q: Query, pas: dict[str, str]) -> set[str] | None:
    """The PA codes a query's PA selection keeps (its PA filter's codes and every PA of its PA type filter's types), from `pas` (code
    to type); None when it has no PA selection (every PA)."""
    f = q.filter_map
    if not any(d in f for d in PA_SELECTION):
        return None
    codes = {c.strip().upper() for c in current_codes(f.get("pa", ()))}
    types = {t.strip().upper() for t in f.get("pa_type", ())}
    return codes | {code for code, t in pas.items() if t.upper() in types}


def _finish(result: Result) -> None:
    numbers = result.fields
    totals = {c: sum(r["_sums"][c] for r in result.rows) for c in numbers}
    result.totals = {"_sums": totals, "_rows": sum(r["_rows"] for r in result.rows)}
    for m in result.metric_list:
        result.totals[m.code] = m.value(totals) if result.rows else None
        for r in result.rows:
            r[m.code] = m.value(r["_sums"])
