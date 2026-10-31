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

    page = dashboard.PAGES.get(slug)
    if page is None:
        raise Http404
    error = None
    try:
        view = dashboard.parse(page, request.GET)
    except query.QueryError as e:
        error = f"That view could not be shown: {e}."
        view = dashboard.parse(page, {})
    comparison = dashboard.compare(view)
    options = dashboard.filter_options()
    selected = view.query.filter_map
    metric = view.metric
    by_label = query.DIMENSIONS[view.by].label
    caption = f"{metric.label}{f' ({metric.unit})' if metric.unit else ''} by {by_label.lower()}"
    context = {
        "page": page,
        "pages": dashboard.PAGES.values(),
        "view": view,
        "error": error,
        "comparison": comparison,
        "chart": dashboard.chart(comparison),
        "metric": metric,
        "by_label": by_label,
        "caption": caption,
        "series_labels": [(s, comparison.series_label(s)) for s in comparison.results],
        "series_choices": [(code, label) for code, (label, _) in dashboard.SERIES.items()],
        "filings": dashboard.filings(),
        "view_by": [(d, query.DIMENSIONS[d].label) for d in query.VIEW_BY],
        "metric_choices": [(m, metrics.resolve(m).label.split(",")[0]) for m in page.metrics],
        "gross": request.GET.get("gross") in ("1", "true", "on"),
        "lifecycle": request.GET.get("lifecycle") in ("1", "true", "on"),
        "exclusions": [(code, e.label, code in view.query.exclude) for code, e in query.EXCLUSIONS.items()],
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
