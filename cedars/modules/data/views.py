"""Public downloads: confirmed data only, no sign-in (Jake, 2026-10-31). The dashboard pages link here with their own query string,
so a download is the page's own query."""

import csv
import io

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET

from . import dashboard, downloads, metrics, query


@require_GET
def download(request, fmt):
    if fmt not in downloads.FORMATS:
        raise Http404
    try:
        q = query.from_params(request.GET)
        result = query.run(q)
    except query.QueryError as e:
        return HttpResponse(f"That download could not be made: {e}", status=400, content_type="text/plain; charset=utf-8")
    if len(result.rows) > settings.DATA_DOWNLOAD_MAX_ROWS:
        return HttpResponse(
            f"That download has {len(result.rows):,} rows, more than the {settings.DATA_DOWNLOAD_MAX_ROWS:,} a download holds for now. "
            "Narrow the filters or group by fewer dimensions.",
            status=413,
            content_type="text/plain; charset=utf-8",
        )
    response = HttpResponse(downloads.render(result, fmt), content_type=downloads.FORMATS[fmt])
    response["Content-Disposition"] = f'attachment; filename="{downloads.filename(result, fmt)}"'
    return response


@require_GET
def metrics_csv(request):
    rows = metrics.catalog_rows()
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    response = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="cedars_metric_catalog.csv"'
    return response


DASHBOARD_HELP = {
    "use_category": "Measure use categories, separated by commas.",
    "program": "PrgIDs, separated by commas.",
    "measure": "Measure IDs, separated by commas.",
}


@require_GET
def dashboard_index(request):
    from django.shortcuts import redirect

    return redirect("data:dashboard", next(iter(dashboard.PAGES)))


@require_GET
def dashboard_page(request, slug):
    from django.shortcuts import render

    from . import budget_view, goal_view

    page = dashboard.PAGES.get(slug)
    if page is None:
        raise Http404
    error = None
    try:
        view = dashboard.parse(page, request.GET)
    except query.QueryError as e:
        error = f"That view could not be shown: {e}."
        view = dashboard.parse(page, {})
    metric = view.metric
    unit = f" ({metric.unit})" if metric.unit else ""
    blocks, totals, goals = [], None, None
    spending = None
    if page.kind == "budget":
        spending = budget_view.compare(view)
        blocks.append(
            {
                "kind": "budget",
                "s": spending,
                "chart": budget_view.chart(spending),
                "caption": "Expenditure against the operating budget, by year ($ nominal)",
            }
        )
    elif page.kind == "goals":
        period = "cycle" if request.GET.get("period") == "cycle" else "annual"
        goals = goal_view.compare(view, period)
        blocks.append({"kind": "goals", "comparison": goals, "chart": goal_view.chart(goals), "caption": _goal_caption(goals)})
    else:
        sections = page.sections or (dashboard.Section(view.by, f"by {query.DIMENSIONS[view.by].label.lower()}"),)
        for sec in sections:
            caption = f"{metric.label}{unit} {sec.title}"
            if sec.columns:
                m = dashboard.matrix(view, sec.by, sec.columns)
                blocks.append(
                    {"kind": "matrix", "matrix": m, "caption": f"{caption}, {m.label.lower()}", "by_label": query.DIMENSIONS[sec.by].label}
                )
                continue
            c = dashboard.compare(view, sec.by, sec.drill)
            totals = totals or c
            blocks.append(
                {
                    "kind": "compare",
                    "comparison": c,
                    "chart": dashboard.chart(c, sec.limit) if sec.chart else None,
                    "caption": caption,
                    "by_label": query.DIMENSIONS[sec.by].label,
                    "series_labels": [(sr, c.series_label(sr)) for sr in c.results],
                }
            )
    first = goals or spending or totals
    options = dashboard.filter_options()
    selected = view.query.filter_map
    context = {
        "page": page,
        "pages": dashboard.PAGES.values(),
        "view": view,
        "error": error,
        "blocks": blocks,
        "totals": totals,
        "first": first,
        "notes": first.notes if first else [],
        "downloads": first.downloads() if first else [],
        "metric": metric,
        "series_labels": [(sr, first.series_label(sr)) for sr in first.results] if first else [],
        "series_choices": [(code, label) for code, (label, _) in dashboard.SERIES.items()],
        "filings": dashboard.filings(),
        "view_by": [(d, query.DIMENSIONS[d].label) for d in query.VIEW_BY] if page.view_by else [],
        "metric_choices": [(m, _metric_label(page, m)) for m in page.metrics],
        "period": request.GET.get("period") == "cycle",
        "gross": request.GET.get("gross") in ("1", "true", "on"),
        "lifecycle": request.GET.get("lifecycle") in ("1", "true", "on"),
        "basis_controls": page.kind == "compare",
        "elements": budget_view.element_metrics() if spending else [],
        "exclusions": [(code, e.label, code in view.query.exclude) for code, e in query.EXCLUSIONS.items() if not (goals and code == "cs")],
        "applied": dashboard.applied_filters(view),
        "filter_fields": [
            {
                "code": d,
                "label": query.DIMENSIONS[d].label,
                "options": options.get(d, []),
                "selected": selected.get(d, ()),
                "help": DASHBOARD_HELP.get(d, ""),
            }
            for d in dashboard.FILTERS
        ],
    }
    return render(request, "data/dashboard.html", context)


def _metric_label(page, code):
    if page.kind == "goals":
        g = dashboard.GOAL_METRICS[code]
        return f"{g.label} ({g.unit})"
    return metrics.resolve(code).label.split(",")[0]


def _goal_caption(c):
    when = "by cycle" if c.period == "cycle" else "by year"
    return f"{c.metric.label} ({c.metric.unit}) against the goal, {when}"


RECORDS_PER_PAGE = 100


@require_GET
def records_page(request):
    from django.shortcuts import render
    from django.utils.http import urlencode

    error, recs, q = None, None, None
    try:
        q = query.from_params(request.GET)
        page = max(int(request.GET.get("page", "1")) if request.GET.get("page", "1").isdigit() else 1, 1)
        recs = query.records(q, limit=RECORDS_PER_PAGE, offset=(page - 1) * RECORDS_PER_PAGE)
    except query.QueryError as e:
        error = f"Those records could not be shown: {e}."
    context = {"error": error, "series": "Claimed" if not q or q.dataset == "claims" else q.dataset.capitalize()}
    if recs is not None:
        for r in recs.rows:
            r["filed_by"] = f"{r['_organisation']} {r['_filing']}"
        base = {k: request.GET.getlist(k) for k in request.GET if k != "page"}

        def at(n):
            return "?" + urlencode(base | {"page": [str(n)]}, doseq=True)

        context |= {
            "recs": recs,
            "metric_list": recs.result.metric_list,
            "as_of": recs.result.as_of,
            "notes": recs.result.notes,
            "applied": [{"label": query.DIMENSIONS[d].label, "value": v} for d, vs in q.filters for v in vs],
            "first": recs.offset + 1,
            "last": recs.offset + len(recs.rows),
            "previous": at(page - 1) if page > 1 else "",
            "next": at(page + 1) if recs.offset + len(recs.rows) < recs.count else "",
            "download": q.query_string(),
        }
    return render(request, "data/records.html", context)
