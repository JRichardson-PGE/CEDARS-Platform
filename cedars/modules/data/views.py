"""Public downloads: confirmed data only, no sign-in (Jake, 2026-10-31). The dashboard pages link here with their own query string,
so a download is the page's own query."""

import csv
import io

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import dashboard, downloads, metrics, query


@require_GET
def download(request, fmt):
    if fmt not in downloads.FORMATS:
        raise Http404
    # several "dataset" values = one file holding each series (a dashboard showing forecast and claims together)
    datasets = list(dict.fromkeys(d.strip() for d in request.GET.getlist("dataset") if d.strip()))
    try:
        q = query.from_params(request.GET)
        if len(datasets) > 1:
            results = [query.run(downloads.for_dataset(q, d)) for d in datasets]
        else:
            results = [query.run(q)]
    except query.QueryError as e:
        return HttpResponse(f"That download could not be made: {e}", status=400, content_type="text/plain; charset=utf-8")
    count = sum(len(r.rows) for r in results)
    if count > settings.DATA_DOWNLOAD_MAX_ROWS:
        return HttpResponse(
            f"That download has {count:,} rows, more than the {settings.DATA_DOWNLOAD_MAX_ROWS:,} a download holds for now. "
            "Narrow the filters or group by fewer dimensions.",
            status=413,
            content_type="text/plain; charset=utf-8",
        )
    if len(results) > 1:
        body, name = downloads.render_combined(results, fmt), downloads.combined_filename(results, fmt)
    else:
        body, name = downloads.render(results[0], fmt), downloads.filename(results[0], fmt)
    response = HttpResponse(body, content_type=downloads.FORMATS[fmt])
    response["Content-Disposition"] = f'attachment; filename="{name}"'
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


@require_GET
def front_page(request):
    """The site's front page (Jake, 2026-10-31): what CEDARS is, the program cycle at a glance (front.py) and the way to Help."""
    from django.shortcuts import render

    from cedars.core import help as site_help

    from . import front

    summary = front.summary(request.GET.get("cycle", ""))
    return render(
        request,
        "data/front.html",
        {
            "summary": summary,
            "guides": site_help.GUIDES,
            "support": site_help.support(),
        },
    )


def _metric_format(metric):
    """A chart value in words as the table shows it, with the metric's unit ($ before the number, others after)."""
    if metric.unit == "$":
        return lambda v: f"{'-' if v < 0 else ''}${abs(v):,.{metric.decimals}f}"
    unit = f" {metric.unit}" if metric.unit else ""
    return lambda v: f"{v:,.{metric.decimals}f}{unit}"


def _dollars(v):
    return f"{'-' if v < 0 else ''}${abs(v):,.0f}"


def _goal_format(goal_metric):
    """A goal-page chart value as its table shows it (`data_tags.number`): whole dollars for TSB, two decimals for savings."""
    if goal_metric.unit == "$":
        return lambda v: f"{'-' if v < 0 else ''}${abs(v):,.0f}"
    return lambda v: f"{v:,.2f} {goal_metric.unit}"


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
    from urllib.parse import urlencode

    from django.shortcuts import render

    from . import budget_view, goal_view, saved_pages

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
                "part": "budget",
                "s": spending,
                "chart": dashboard.describe(budget_view.chart(spending), _dollars),
                "caption": "Expenditure against the operating budget, by year ($ nominal)",
            }
        )
    elif page.kind == "goals":
        period = "cycle" if request.GET.get("period") == "cycle" else "annual"
        goals = goal_view.compare(view, period)
        blocks.append(
            {
                "kind": "goals",
                "part": "goals",
                "comparison": goals,
                "chart": dashboard.describe(goal_view.chart(goals), _goal_format(goals.metric)),
                "caption": _goal_caption(goals),
            }
        )
    else:
        sections = page.sections or (dashboard.Section(view.by, f"by {query.DIMENSIONS[view.by].in_sentence}"),)
        for n, sec in enumerate(sections, 1):
            caption = f"{metric.label}{unit} {sec.title}"
            if sec.columns:
                m = dashboard.matrix(view, sec.by, sec.columns)
                blocks.append(
                    {
                        "kind": "matrix",
                        "part": f"s{n}",
                        "matrix": m,
                        "caption": f"{caption}, {m.label.lower()}",
                        "by_label": query.DIMENSIONS[sec.by].label,
                    }
                )
                continue
            c = dashboard.compare(view, sec.by, sec.drill)
            totals = totals or c
            blocks.append(
                {
                    "kind": "compare",
                    "part": f"s{n}",
                    "comparison": c,
                    "chart": dashboard.describe(dashboard.chart(c, sec.limit), _metric_format(metric)) if sec.chart else None,
                    "caption": caption,
                    "by_label": query.DIMENSIONS[sec.by].label,
                    "series_labels": [(sr, dashboard.SERIES[sr][0]) for sr in c.results],
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
        "record_downloads": _record_downloads(first),
        "sign_in": _sign_in(request),
        "max_rows": settings.DATA_DOWNLOAD_MAX_ROWS,
        "metric": metric,
        # the series' names in the tiles and table headers, and when each was published said once above them (UI review 2, #19)
        "series_labels": [(sr, dashboard.SERIES[sr][0]) for sr in first.results] if first else [],
        "as_of": [(dashboard.SERIES[sr][0], first.results[sr].as_of) for sr in first.results] if first else [],
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
            if d not in query.PA_SELECTION
        ],
        "pa_picker": dashboard.pa_picker(selected),
    }
    context |= saved_pages.page_context(request, page.slug, None if error else urlencode(view.params, doseq=True))
    return render(request, "data/dashboard.html", context)


def _record_downloads(first) -> list[dict]:
    """Each series' record-level download, beside the download of its rows: the same query, one record per row."""
    from . import records

    if not first:
        return []
    return [records.behind(r, dashboard.SERIES[s][0], max_rows=settings.DATA_DOWNLOAD_MAX_ROWS) for s, r in first.results.items()]


def _sign_in(request) -> str:
    from django.urls import reverse
    from django.utils.http import urlencode

    return reverse("account_login") + "?" + urlencode({"next": request.get_full_path()})


def _metric_label(page, code):
    if page.kind == "goals":
        g = dashboard.GOAL_METRICS[code]
        return f"{g.label} ({g.unit})"
    return metrics.resolve(code).label.split(",")[0]


def _goal_caption(c):
    when = "by cycle" if c.period == "cycle" else "by year"
    return f"{c.metric.label} ({c.metric.unit}) against the goal, {when}"


RECORDS_PER_PAGE = 100
FORM_SKIP = ("csrfmiddlewaretoken", "page")


@require_http_methods(["GET", "POST"])
def records_page(request):
    """Record-level data (BR-07.5): filters, a column picker, the first records and their downloads. The form posts here and is sent
    on to the short GET address of what it chose, so every choice has a link that reproduces it (and fits in a URL)."""
    from django.shortcuts import redirect, render

    from . import records, saved, saved_pages

    error, page, number, own_columns = None, None, 1, False
    default = saved.default_columns(request.user)
    try:
        if request.method == "POST":
            rq = records.from_params({k: request.POST.getlist(k) for k in request.POST if k not in FORM_SKIP})
            cat = records.catalog(query.files_of(rq.query))
            columns, _ = records.chosen(cat, rq.params)
            return redirect(f"{request.path}?{records.query_string(rq.query, cat, columns)}")
        rq = records.from_params(request.GET, default_columns=default)
        own_columns = rq.default_columns
        if request.GET.get("page", "").isdigit():
            number = max(int(request.GET["page"]), 1)
        page = records.page(rq, limit=RECORDS_PER_PAGE, offset=(number - 1) * RECORDS_PER_PAGE)
    except query.QueryError as e:
        error = f"Those records could not be shown: {e}."
    context = _records_context(request, page, number, error)
    context |= saved_pages.page_context(request, saved.RECORDS, context.get("job_query"))
    context["default_columns"] = {"on": own_columns and page is not None, "set": default is not None}
    return render(request, "data/records.html", context)


def _records_context(request, page, number, error) -> dict:
    from django.urls import reverse
    from django.utils.http import urlencode

    from . import records

    q = page.result.query if page else records.from_params({}).query
    options = dashboard.filter_options()
    selected = q.filter_map
    context = {
        "error": error,
        "q": q,
        "series": records.DATASETS.get(q.dataset, q.dataset.capitalize()),
        "datasets": list(records.DATASETS.items()),
        "filings": dashboard.filings(),
        "exclusions": [(code, e.label, code in q.exclude) for code, e in query.EXCLUSIONS.items()],
        "pa_picker": dashboard.pa_picker(selected),
        "text_filters": [
            {"code": d, "label": query.DIMENSIONS[d].label, "value": ",".join(selected.get(d, ())), "help": DASHBOARD_HELP[d]}
            for d in ("program", "measure")
        ],
        "more_filters": [
            {"code": d, "label": query.DIMENSIONS[d].label, "options": options.get(d, []), "selected": selected.get(d, ())}
            for d in ("sector", "segment", "implementer_type", "measure_type", "use_category")
        ],
        "hidden": [(d, v) for d, vs in q.filters if d not in records.FORM_FILTERS for v in vs]
        + [("metric", m) for m in q.metrics]
        + ([("release", str(q.release))] if q.release else []),
        "applied": [{"label": query.DIMENSIONS[d].label, "value": v} for d, vs in q.filters for v in vs],
        "max_rows": settings.DATA_DOWNLOAD_MAX_ROWS,
        "xlsx_max_rows": records.XLSX_MAX_ROWS,
    }
    if page is None:
        return context
    chosen = set(page.columns)
    base = records.query_string(q, page.catalog, page.columns)

    def at(n):
        return "?" + base + "&" + urlencode({"page": n})

    context |= {
        "page": page,
        "as_of": page.result.as_of,
        "notes": page.result.notes,
        "metric_list": page.result.metric_list,
        "totals": page.result.totals,
        "groups": [
            {
                "label": g.label,
                "slug": g.slug,
                "count": len(g.columns),
                "chosen": sum(c.name in chosen for c in g.columns),
                "columns": [(c, c.name in chosen) for c in g.columns],
            }
            for g in page.catalog.groups
        ],
        "column_count": len(page.catalog.columns),
        "presets": [
            ("Key columns only", "?" + records.query_string(q, preset="key")),
            ("Every column", "?" + records.query_string(q, preset="all")),
            ("No columns", "?" + records.query_string(q, preset="none")),
        ],
        "first": page.offset + 1,
        "last": page.offset + len(page.rows),
        "previous": at(number - 1) if number > 1 else "",
        "next": at(number + 1) if page.offset + len(page.rows) < page.count else "",
        "downloads": [(fmt.upper(), label, f"/data/records/download.{fmt}?{base}") for fmt, label in records.FORMAT_LABELS.items()],
        "too_many": page.count > settings.DATA_DOWNLOAD_MAX_ROWS,
        "job_query": base,
        "sign_in": reverse("account_login") + "?" + urlencode({"next": request.get_full_path()}),
        "xlsx_too_many": page.count > records.XLSX_MAX_ROWS,
    }
    return context


@require_GET
def records_download(request, fmt):
    """Every record the query keeps, with the chosen columns, as one file (BR-07.5)."""
    import tempfile

    from django.http import FileResponse

    from . import records

    if fmt not in records.FORMATS:
        raise Http404
    try:
        page = records.page(records.from_params(request.GET), limit=0)
    except query.QueryError as e:
        return HttpResponse(f"That download could not be made: {e}", status=400, content_type="text/plain; charset=utf-8")
    if page.count > settings.DATA_DOWNLOAD_MAX_ROWS:
        return HttpResponse(
            f"That download has {page.count:,} records, more than the {settings.DATA_DOWNLOAD_MAX_ROWS:,} made while you wait. "
            "Sign in and ask for it on the records page: it is made in the background and you are told when it is ready.",
            status=413,
            content_type="text/plain; charset=utf-8",
        )
    if fmt == "xlsx" and page.count > records.XLSX_MAX_ROWS:
        return HttpResponse(
            f"That download has {page.count:,} records, more than an Excel worksheet holds. Download it as CSV or Parquet.",
            status=413,
            content_type="text/plain; charset=utf-8",
        )
    out = tempfile.TemporaryFile()  # noqa: SIM115 - the response streams it and closes it
    records.write(page, fmt, out)
    out.seek(0)
    return FileResponse(out, as_attachment=True, filename=records.filename(page, fmt), content_type=records.FORMATS[fmt])


# --- background downloads (BR-07.2): more records than DATA_DOWNLOAD_MAX_ROWS, made by run_data_downloads, announced by notification


@login_required
@require_POST
def download_request(request):
    from urllib.parse import parse_qs

    from django.contrib import messages
    from django.shortcuts import redirect

    from . import jobs, records

    qs = request.POST.get("query", "")
    try:
        page = records.page(records.from_params(parse_qs(qs)), limit=0)
        job = jobs.request(request.user, page, request.POST.get("format", "csv"))
    except (query.QueryError, jobs.JobRefused) as e:
        messages.error(request, f"That download could not be queued: {e}")
        return redirect(f"/data/records/?{qs}")
    messages.success(request, "Your download is queued. You will be told when it is ready, on your notifications page and by email.")
    return redirect("data:download_job", job.pk)


@login_required
@require_GET
def download_list(request):
    from django.shortcuts import render

    from . import jobs
    from .models import DownloadJob

    rows = list(DownloadJob.objects.filter(user=request.user)[:100])
    return render(request, "data/downloads.html", {"jobs": rows, "keep_days": jobs.retention_days()})


def _own_job(request, pk):
    from django.shortcuts import get_object_or_404

    from .models import DownloadJob

    return get_object_or_404(DownloadJob, pk=pk, user=request.user)  # someone else's download is not found, not forbidden


@login_required
@require_GET
def download_job(request, pk):
    from django.shortcuts import render

    from . import jobs

    job = _own_job(request, pk)
    return render(
        request,
        "data/download_job.html",
        {"job": job, "ahead": jobs.queue_position(job), "keep_days": jobs.retention_days(), "series": _series(job.dataset)},
    )


@login_required
@require_GET
def download_file(request, pk):
    from django.http import FileResponse
    from django.shortcuts import redirect

    from cedars.core import storage

    from . import jobs, records
    from .models import DownloadJob

    job = _own_job(request, pk)
    if job.status != DownloadJob.Status.DONE:
        raise Http404
    store = storage.store(jobs.AREA)
    url = store.download_url(job.key, job.filename, content_type=records.FORMATS[job.fmt])
    if url:  # S3: a pre-signed address; large files never pass through a page
        return redirect(url)
    try:
        f = store.open(job.key)
    except storage.StorageError as e:
        raise Http404 from e
    return FileResponse(f, as_attachment=True, filename=job.filename, content_type=records.FORMATS[job.fmt])


def _series(dataset):
    from . import records

    return records.DATASETS.get(dataset, dataset)
