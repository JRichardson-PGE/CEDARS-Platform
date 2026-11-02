"""/api/v1/data: the open data API (BR-11.5, decision J) over the published snapshots, for anyone, with no account.

It serves what the dashboards and record downloads serve, through the same code, so an API answer equals the page and the file for the
same query: the metric catalog (`metrics`), the dimensions, filters and exclusions the dashboards use (`dimensions`), aggregated queries
(`query`: `query.run`, the dashboards' query) and paged record-level reads with a choice of columns (`records`, `columns`: the record
downloads' query and column choice). Every answer that carries data is stamped as the downloads are (release, as-of time, snapshots
and filings read, filters, exclusions, dictionary version), so a number can be traced to exactly what it was read from.

Public fields only (BR-08.3, BR-11.5): a column, metric, dimension or exclusion is served only when every canonical field it reads is
marked public in the data dictionary in force (`public_fields`). A field the dictionary does not have (a `Legacy_` column, a column a
run added that the dictionary has not caught up with) is not marked public, so it is never served; nor is one marked restricted or
confidential. The three columns the platform adds to every record (FiledBy, Filing, CountedPA) say where a record came from and are
served with every record.

Calls are rate limited like every API call (REST_FRAMEWORK throttles: per address without a token, per person with one); pages hold at
most DATA_API_MAX_ROWS rows, and `next` / `previous` give the neighboring pages."""

import math
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from cedars.services.snapshots import service as snapshots
from cedars.services.snapshots.models import Dataset, Release

from . import dashboard, records
from . import metrics as catalog
from . import query as dq

# The platform's own columns on every record: where it came from, not filed data.
SOURCE_COLUMNS = tuple(records.SOURCE_COLUMNS)
# Parameters each read takes besides the dimensions' and exclusions' own (a dimension's code filters on it; an exclusion's code takes
# include or exclude). Anything else is refused, so a misspelled parameter is not silently ignored.
QUERY_PARAMS = {"dataset", "metric", "by", "year_from", "year_to", "gross", "lifecycle", "filing", "release", "limit", "offset"}
RECORD_PARAMS = (QUERY_PARAMS - {"by", "gross", "lifecycle"}) | {"columns", "colgroup", "col", "drop"}
COLUMN_PARAMS = {"dataset", "filing", "release"}


class Refused(ValueError):
    """A call that cannot be answered as asked; its message says why (400)."""


# --- what is public


def public_fields() -> tuple[set[str], str]:
    """The canonical fields the data dictionary in force marks public (lower case), and the dictionary's version label. No dictionary
    loaded: none."""
    from cedars.services.dictionary import service as dictionary
    from cedars.services.dictionary.models import Field

    version = dictionary.current()
    if version is None:
        return set(), ""
    names = Field.objects.filter(version=version, audience=Field.Audience.PUBLIC).values_list("name", flat=True)
    return {n.lower() for n in names}, version.label


def _is_public(fields, public: set[str]) -> bool:
    return all(f.lower() in public for f in fields if f)


def public_metrics(public: set[str]) -> list[catalog.Metric]:
    return [m for m in catalog.CATALOG.values() if _is_public(m.fields, public)]


def public_dimensions(public: set[str]) -> list[dq.Dimension]:
    """The dimensions whose column is public. Filed by and Filing come from the release itself; PA type reads the PA's column."""
    return [d for d in dq.DIMENSIONS.values() if _is_public([d.column], public)]


def public_exclusions(public: set[str]) -> list[dq.Exclusion]:
    return [e for e in dq.EXCLUSIONS.values() if _is_public([e.column], public)]


def public_catalog(cat: records.Catalog, public: set[str]) -> records.Catalog:
    """The record columns a call may read: the platform's source columns and every column the dictionary marks public (`present`, the
    files' own columns, is kept whole: the query reads its filters' columns from it, never serving them)."""
    groups = []
    for g in cat.groups:
        kept = [c for c in g.columns if c.name in SOURCE_COLUMNS or c.name.lower() in public]
        if kept:
            groups.append(records.Group(g.label, g.slug, kept))
    return records.Catalog(groups, cat.present, cat.dictionary)


# --- reading a call's parameters


def _check_names(params, allowed: set[str]) -> None:
    known = allowed | set(dq.DIMENSIONS) | set(dq.EXCLUSIONS)
    unknown = sorted(k for k in params if k not in known)
    if unknown:
        raise Refused(f"unknown parameter{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}")


def _paging(params) -> tuple[int, int]:
    def number(key, default):
        value = (params.get(key) or "").strip()
        if not value:
            return default
        if not value.isdigit():
            raise Refused(f"{key} must be a whole number")
        return int(value)

    limit = number("limit", settings.DATA_API_DEFAULT_ROWS)
    if not 1 <= limit <= settings.DATA_API_MAX_ROWS:
        raise Refused(f"limit must be from 1 to {settings.DATA_API_MAX_ROWS:,} (see /limits/)")
    return limit, number("offset", 0)


def _check_release(q: dq.Query) -> None:
    if q.release and not Release.objects.filter(dataset=q.dataset, number=q.release).exists():
        raise Refused(f"the {q.dataset} dataset has no release {q.release}")


def _guard(q: dq.Query, public: set[str], default_exclude=dq.DEFAULT_EXCLUDE) -> None:
    """Refuse a query that names a metric, dimension or exclusion reading a field the dictionary does not mark public."""
    metrics = {m.code for m in public_metrics(public)}
    dims = {d.code for d in public_dimensions(public)}
    exclusions = {e.code for e in public_exclusions(public)}
    for m in q.metrics:
        if m not in metrics:
            raise Refused(f"metric {m!r} is not public data (see /api/v1/data/metrics)")
    for d in list(q.by) + [d for d, _ in q.filters]:
        if d not in dims:
            raise Refused(f"dimension {d!r} is not public data (see /api/v1/data/dimensions)")
    for e in set(q.exclude) ^ set(default_exclude):  # an exclusion asked for or taken off (the defaults only leave rows out)
        if e not in exclusions:
            raise Refused(f"exclusion {e!r} is not public data (see /api/v1/data/dimensions)")


def _query(params, public: set[str]) -> dq.Query:
    _check_names(params, QUERY_PARAMS)
    q = dq.from_params(params)
    _check_release(q)
    _guard(q, public)
    return q


def _record_query(params, public: set[str]) -> tuple[records.RecordQuery, dict]:
    _check_names(params, RECORD_PARAMS)
    given = {k: params.getlist(k) for k in params if k not in ("limit", "offset")}
    rq = records.from_params(given)
    _check_release(rq.query)
    _guard(rq.query, public, default_exclude=())
    return rq, given


# --- values as JSON


def _value(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None  # JSON has no NaN; a sum or file value that is not a number reads as blank
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else None
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _url(request, params: dict) -> str:
    clean = {k: v for k, v in params.items() if v not in (None, "", [])}
    return request.build_absolute_uri(request.path) + ("?" + urlencode(clean, doseq=True) if clean else "")


def _pages(request, count: int, limit: int, offset: int) -> dict:
    params = {k: request.query_params.getlist(k) for k in request.query_params}

    def at(n):
        return _url(request, params | {"limit": [str(limit)], "offset": [str(n)]})

    return {
        "count": count,
        "limit": limit,
        "offset": offset,
        "next": at(offset + limit) if offset + limit < count else None,
        "previous": at(max(offset - limit, 0)) if offset > 0 else None,
    }


def _refusal(e: Exception) -> Response:
    return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


def _metric_json(m: catalog.Metric) -> dict:
    return {
        "code": m.code,
        "label": m.label,
        "group": m.group,
        "unit": m.unit,
        "scale": m.scale,
        "decimals": m.decimals,
        "numerator": list(m.numerator),
        "denominator": list(m.denominator),
        "adds_up": m.adds_up,
        "definition": m.definition,
    }


# --- schema pieces

Detail = inline_serializer("DataRefusal", {"detail": serializers.CharField()})
Stamp = inline_serializer(
    "DataStamp",
    {
        "dataset": serializers.CharField(),
        "release": serializers.IntegerField(allow_null=True, help_text="The release read: null when nothing is published yet"),
        "as_of": serializers.DateTimeField(allow_null=True, help_text="When the release was made: the data's as-of time"),
        "snapshots": serializers.ListField(child=serializers.CharField(), help_text="The snapshots that gave rows"),
        "filings": serializers.ListField(child=serializers.CharField(), help_text="Their filings: PA, cycle or year, kind, version"),
        "years": serializers.ListField(child=serializers.IntegerField(allow_null=True)),
        "filters": serializers.DictField(child=serializers.ListField(child=serializers.CharField())),
        "excluded": serializers.ListField(child=serializers.CharField(), help_text="What was left out, in words"),
        "filing": serializers.CharField(),
        "query": serializers.CharField(help_text="The page's own query string: the same data on /data/ or /data/records/"),
        "dictionary": serializers.CharField(help_text="The data dictionary version whose public fields were served"),
        "generated_at": serializers.DateTimeField(),
        "notes": serializers.ListField(child=serializers.CharField()),
    },
)
Paging = {
    "count": serializers.IntegerField(help_text="Rows over every page"),
    "limit": serializers.IntegerField(),
    "offset": serializers.IntegerField(),
    "next": serializers.URLField(allow_null=True),
    "previous": serializers.URLField(allow_null=True),
}


def _p(name, description, many=False, type_=OpenApiTypes.STR, **kw):
    return OpenApiParameter(name, type_, OpenApiParameter.QUERY, required=False, description=description, many=many, **kw)


COMMON = [
    _p("dataset", "claims (default), forecast (budget filings) or monthly", enum=list(Dataset.values)),
    _p("year_from", "The first program year", type_=OpenApiTypes.INT),
    _p("year_to", "The last program year", type_=OpenApiTypes.INT),
    _p(
        "filing",
        "Forecast only: a filing kind (TUAL) or cycle|kind (2028-2031|TUAL); default each PA's latest filing in each cycle",
    ),
    _p("release", "An earlier release's number; default the current one", type_=OpenApiTypes.INT),
    _p("pa", "Filter: PA codes (a former code counts as its PA's current one); ORed with pa_type", many=True),
    _p("pa_type", "Filter: IOU, REN or CCA; ORed with pa", many=True),
    *[
        _p(
            d.code,
            f"Filter: {d.label.lower() if not d.label.isupper() else d.label}, repeated for several values (matched ignoring case "
            "and surrounding spaces; values in GET /api/v1/data/dimensions)",
            many=True,
        )
        for d in dq.DIMENSIONS.values()
        if d.code not in ("pa", "pa_type", "filing")  # "filing" names the forecast filing to read, above
    ],
    _p("limit", "Rows per page, at most DATA_API_MAX_ROWS (see /limits/)", type_=OpenApiTypes.INT),
    _p("offset", "Rows to skip", type_=OpenApiTypes.INT),
]


def _exclusions(default: tuple[str, ...]) -> list[OpenApiParameter]:
    return [
        _p(
            e.code,
            f"{e.label}: include or exclude (default {'exclude' if e.code in default else 'include'})",
            enum=["include", "exclude"],
        )
        for e in dq.EXCLUSIONS.values()
    ]


# --- the endpoints


@extend_schema(
    operation_id="data_index",
    summary="The published datasets",
    description="Each dataset's current release (its number, as-of time and the snapshots in it) and the addresses of the data API. "
    "Public, no account needed.",
    responses=inline_serializer(
        "DataIndex",
        {
            "datasets": inline_serializer(
                "DataDataset",
                {
                    "code": serializers.CharField(),
                    "label": serializers.CharField(),
                    "release": serializers.IntegerField(allow_null=True),
                    "as_of": serializers.DateTimeField(allow_null=True),
                    "snapshots": serializers.IntegerField(),
                },
                many=True,
            ),
            "links": serializers.DictField(child=serializers.URLField()),
        },
    ),
)
@api_view(["GET"])
@permission_classes([AllowAny])
def index(request):
    out = []
    for code, label in Dataset.choices:
        release = snapshots.current_release(code)
        out.append(
            {
                "code": code,
                "label": label,
                "release": release.number if release else None,
                "as_of": release.created_at if release else None,
                "snapshots": release.snapshots.count() if release else 0,
            }
        )
    base = request.build_absolute_uri("/api/v1/data/")
    links = {name: base + name for name in ("metrics", "dimensions", "query", "columns", "records")}
    return Response({"datasets": out, "links": links | {"schema": request.build_absolute_uri("/api/v1/schema")}})


@extend_schema(
    operation_id="data_metrics",
    summary="The metric catalog",
    description="Every metric a query can ask for: the canonical fields it sums (a ratio divides two sums, never averages ratios, and "
    "is blank when its denominator is 0), its unit and the scale its sums are divided by for that unit. Only metrics whose fields the "
    "data dictionary marks public. `bases` are the dashboards' view-control names a query also takes as `metric` (kwh, kw, therms, "
    "co2, nox, pm10, tsb), turned into a catalog code by `gross=1` and `lifecycle=1`.",
    responses=inline_serializer(
        "DataMetrics",
        {
            "dictionary": serializers.CharField(),
            "results": serializers.ListField(child=serializers.DictField()),
            "bases": serializers.DictField(child=serializers.DictField()),
        },
    ),
)
@api_view(["GET"])
@permission_classes([AllowAny])
def metric_list(request):
    public, version = public_fields()
    shown = public_metrics(public)
    codes = {m.code for m in shown}
    bases = {}
    for base in ("tsb", *catalog.SAVINGS, *catalog.EMISSIONS):
        forms = {
            ("gross" if g else "net") + ("_lifecycle" if lc else "_first_year"): catalog.resolve(base, gross=g, lifecycle=lc).code
            for g in (False, True)
            for lc in (False, True)
        }
        if set(forms.values()) <= codes:
            bases[base] = forms
    return Response({"dictionary": version, "results": [_metric_json(m) for m in shown], "bases": bases})


@extend_schema(
    operation_id="data_dimensions",
    summary="Dimensions, filters and exclusions",
    description="What a query can group by (`by`) and filter on (each dimension's code as a parameter), with the values the dashboards "
    "offer where they come from the platform (sectors, segments and implementer types from the program registry; the PAs by type); "
    "the exclusions, with which a query and a records read leave out by default; and the forecast filings. Only dimensions whose "
    "column the data dictionary marks public.",
    responses=inline_serializer(
        "DataDimensions",
        {
            "dictionary": serializers.CharField(),
            "datasets": serializers.ListField(child=serializers.DictField()),
            "dimensions": serializers.ListField(child=serializers.DictField()),
            "exclusions": serializers.ListField(child=serializers.DictField()),
            "pas": serializers.ListField(child=serializers.DictField()),
            "filings": serializers.ListField(child=serializers.DictField()),
        },
    ),
)
@api_view(["GET"])
@permission_classes([AllowAny])
def dimension_list(request):
    public, version = public_fields()
    options = dashboard.filter_options()
    picker = dashboard.pa_picker({})
    groups = picker["groups"]
    pa_values = [p["code"] for g in groups for p in g["pas"]] + ([picker["statewide"]["code"]] if picker["statewide"] else [])
    values = options | {"pa": pa_values, "pa_type": [g["type"] for g in groups if g["type"]]}
    dims = [
        {
            "code": d.code,
            "label": d.label,
            "column": d.column or None,
            "view_by": d.code in dq.VIEW_BY,
            "values": values.get(d.code),
        }
        for d in public_dimensions(public)
    ]
    exclusions = [
        {
            "code": e.code,
            "label": e.label,
            "column": e.column,
            "values": list(e.values),
            "excluded_by_default_in_query": e.code in dq.DEFAULT_EXCLUDE,
            "excluded_by_default_in_records": False,
        }
        for e in public_exclusions(public)
    ]
    pas = [
        {"type": g["type"], "label": g["label"], "pas": [{"code": p["code"], "former_codes": p["former"]} for p in g["pas"]]}
        for g in groups
    ]
    return Response(
        {
            "dictionary": version,
            "datasets": [{"code": c, "label": lbl} for c, lbl in Dataset.choices],
            "dimensions": dims,
            "exclusions": exclusions,
            "pas": pas,
            "filings": [{"value": v, "label": lbl} for v, lbl in dashboard.filings()],
        }
    )


@extend_schema(
    operation_id="data_query",
    summary="Aggregated data",
    description="The dashboards' query: the metrics asked for, summed by the dimensions in `by` over the records the filters, years and "
    "exclusions keep, from the dataset's release. The same query on a dashboard page or its download gives the same numbers. Rows are "
    "ordered by the dimensions and paged; `totals` are over every row. Each row and the totals carry each metric as shown (scaled to "
    "its unit), `sums` (each canonical field's sum, unscaled) and `records`. By default pension and benefits programs and codes and "
    "standards are left out, as on the dashboards; `stamp.excluded` says what was. `metric` may repeat; it defaults to tsb.",
    parameters=[
        _p("metric", "Metric codes (GET /api/v1/data/metrics) or bases; default tsb", many=True),
        _p("by", "Dimension codes to group by, in order; none = one total row", many=True),
        _p("gross", "1: a base metric's gross form"),
        _p("lifecycle", "1: a base metric's lifecycle form"),
        *COMMON,
        *_exclusions(dq.DEFAULT_EXCLUDE),
    ],
    responses={
        200: inline_serializer(
            "DataQueryResult",
            {
                "stamp": Stamp,
                "metrics": serializers.ListField(child=serializers.DictField()),
                "by": serializers.ListField(child=serializers.CharField()),
                **Paging,
                "rows": serializers.ListField(child=serializers.DictField()),
                "totals": serializers.DictField(),
            },
        ),
        400: OpenApiResponse(Detail, description="A parameter that cannot be read, or that names data that is not public"),
    },
)
@api_view(["GET"])
@permission_classes([AllowAny])
def aggregate(request):
    public, version = public_fields()
    try:
        q = _query(request.query_params, public)
        limit, offset = _paging(request.query_params)
        result = dq.run(q)
    except (Refused, dq.QueryError) as e:
        return _refusal(e)
    stamp = result.stamp()
    for k in ("metrics", "by"):
        stamp.pop(k, None)
    stamp |= {"dictionary": version, "generated_at": timezone.now(), "notes": result.notes}
    metrics = result.metric_list

    def row(r: dict) -> dict:
        return (
            {d: _value(r.get(d)) for d in q.by}
            | {m.code: _value(r.get(m.code)) for m in metrics}
            | {"sums": {f: _value(v) for f, v in r.get("_sums", {}).items()}, "records": r.get("_rows", 0)}
        )

    totals = row(result.totals) if result.rows else {m.code: None for m in metrics} | {"sums": {}, "records": 0}
    for d in q.by:
        totals.pop(d, None)
    return Response(
        {
            "stamp": stamp,
            "metrics": [_metric_json(m) for m in metrics],
            "by": list(q.by),
            **_pages(request, len(result.rows), limit, offset),
            "rows": [row(r) for r in result.rows[offset : offset + limit]],
            "totals": totals,
        }
    )


@extend_schema(
    operation_id="data_columns",
    summary="The record columns",
    description="The columns a records read can return for a dataset (and filing or release), in the data dictionary's groups, each "
    "with its definition, and which are the key columns returned by default. Only the platform's three source columns (FiledBy, "
    "Filing, CountedPA) and columns the data dictionary marks public; a group's `slug` is what `colgroup` takes.",
    parameters=[p for p in COMMON if p.name in COLUMN_PARAMS],
    responses={
        200: inline_serializer(
            "DataColumns",
            {
                "dataset": serializers.CharField(),
                "release": serializers.IntegerField(allow_null=True),
                "dictionary": serializers.CharField(),
                "key": serializers.ListField(child=serializers.CharField()),
                "groups": serializers.ListField(child=serializers.DictField()),
            },
        ),
        400: Detail,
    },
)
@api_view(["GET"])
@permission_classes([AllowAny])
def column_list(request):
    public, version = public_fields()
    try:
        _check_names(request.query_params, RECORD_PARAMS)  # a records read's query string is accepted; only these are read
        params = {k: request.query_params.getlist(k) for k in COLUMN_PARAMS if k in request.query_params}
        q = dq.from_params(params, exclude=())
        _check_release(q)
    except (Refused, dq.QueryError) as e:
        return _refusal(e)
    files = dq.files_of(q)
    cat = public_catalog(records.catalog(files), public)
    release = Release.objects.filter(dataset=q.dataset, number=q.release).first() if q.release else snapshots.current_release(q.dataset)
    return Response(
        {
            "dataset": q.dataset,
            "release": release.number if release else None,
            "dictionary": version,
            "key": cat.key,
            "groups": [
                {"label": g.label, "slug": g.slug, "columns": [{"name": c.name, "definition": c.definition} for c in g.columns]}
                for g in cat.groups
            ],
        }
    )


@extend_schema(
    operation_id="data_records",
    summary="Record-level data, a page at a time",
    description="The published measure-level records the filters, years and exclusions keep, with the columns chosen, in CEInputID "
    "order: the record downloads' query and columns, read a page at a time. Records leave nothing out unless asked (each exclusion "
    "code takes exclude), and with no parameter at all a read is the current program year. Columns: `columns` sets the start (key, "
    "the default; all; none), `colgroup` adds a whole group by its slug, `col` adds one column and `drop` takes one off (GET "
    "/api/v1/data/columns). A column that is not public, or that these records do not have, is refused. `totals` are the metrics asked "
    "for (`metric`, default tsb) over every matching record; `stamp.pas` counts the records each PA holds.",
    parameters=[
        _p("columns", "key (default), all or none: the columns to start from", enum=list(records.PRESETS)),
        _p("colgroup", "Add a column group by its slug", many=True),
        _p("col", "Add a column", many=True),
        _p("drop", "Take a column off", many=True),
        _p("metric", "The metrics in `totals`; default tsb", many=True),
        *COMMON,
        *_exclusions(()),
    ],
    responses={
        200: inline_serializer(
            "DataRecords",
            {
                "stamp": Stamp,
                "columns": serializers.ListField(child=serializers.CharField()),
                **Paging,
                "rows": serializers.ListField(child=serializers.DictField(), help_text="One object per record, by column name"),
                "totals": serializers.DictField(),
            },
        ),
        400: OpenApiResponse(Detail, description="A parameter that cannot be read, or that names data that is not public"),
    },
)
@api_view(["GET"])
@permission_classes([AllowAny])
def record_list(request):
    public, version = public_fields()
    try:
        rq, given = _record_query(request.query_params, public)
        limit, offset = _paging(request.query_params)
        result = dq.run(rq.query)
        cat = public_catalog(records.catalog(result.files), public)
        for slug in records._many(given, "colgroup"):
            if cat.group(slug) is None:
                raise Refused(f"no public column group {slug!r} in these records (see /api/v1/data/columns)")
        columns, unknown = records.chosen(cat, rq.params)
        if unknown:
            raise Refused(f"not a public column of these records: {', '.join(unknown)} (see /api/v1/data/columns)")
    except (Refused, dq.QueryError) as e:
        return _refusal(e)
    rows: list[list] = []
    if result.files and result.totals.get("_rows") and columns:
        sql, args = records._sql(rq.query, result.files, cat, columns)
        con = dq.connect()
        try:
            rows = [list(r) for r in con.execute(sql + " LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()]
        finally:
            con.close()
    page = records.Page(result, cat, columns, [], rows, offset, limit)
    stamp = records.stamp(page)
    stamp |= {"dictionary": version, "generated_at": timezone.now()}
    for k in ("kind", "columns"):
        stamp.pop(k, None)
    totals = {m.code: _value(result.totals.get(m.code)) for m in result.metric_list}
    return Response(
        {
            "stamp": stamp,
            "columns": columns,
            **_pages(request, page.count, limit, offset),
            "rows": [{c: _value(v) for c, v in zip(columns, r, strict=True)} for r in rows],
            "totals": totals | {"records": page.count},
        }
    )
