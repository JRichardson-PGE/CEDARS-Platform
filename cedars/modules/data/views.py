"""Public downloads: confirmed data only, no sign-in (Jake, 2026-10-31). The dashboard pages link here with their own query string,
so a download is the page's own query."""

import csv
import io

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET

from . import downloads, metrics, query


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
