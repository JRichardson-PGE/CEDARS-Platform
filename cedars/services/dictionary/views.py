from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render

from . import service
from .models import Field, Usage

TABLES = {("calculations", "Measure"), ("calculations", "ProgramCost")}


def _version_or_404():
    v = service.current()
    if v is None:
        raise Http404("No dictionary has been published yet.")
    return v


def index(request):
    """The public data dictionary (BR-08.2): every field, searchable, with its attributes one click away."""
    v = _version_or_404()
    q = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    fields = Field.objects.filter(version=v).prefetch_related("aliases")
    if q:
        fields = fields.filter(Q(name__icontains=q) | Q(definition__icontains=q) | Q(aliases__name__icontains=q)).distinct()
    if kind in ("input", "output"):
        fields = fields.filter(kind=kind)
    page = Paginator(fields.order_by("kind", "group", "name"), 100).get_page(request.GET.get("page"))
    tables = sorted({(u.module, u.table) for u in Usage.objects.filter(field__version=v)})
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "dictionary/index.html",
        {"version": v, "page": page, "q": q, "kind": kind, "tables": tables, "query": query.urlencode(), "total": fields.count()},
    )


def detail(request, name):
    v = _version_or_404()
    f = Field.objects.filter(version=v, name=name).prefetch_related("aliases", "usages").first()
    if f is None:
        raise Http404
    return render(request, "dictionary/field.html", {"version": v, "f": f})


def _download(content, filename, content_type):
    r = HttpResponse(content, content_type=content_type)
    r["Content-Disposition"] = f'attachment; filename="{filename}"'
    return r


def catalog_csv(request):
    v = _version_or_404()
    return _download(service.catalog_csv(v), f"cedars_field_catalog_{v.pk}.csv", "text/csv; charset=utf-8")


def crosswalk_csv(request):
    v = _version_or_404()
    return _download(service.crosswalk_csv(v), f"cedars_field_crosswalk_{v.pk}.csv", "text/csv; charset=utf-8")


def template(request, module, table, fmt):
    v = _version_or_404()
    if (module, table) not in TABLES and not Usage.objects.filter(field__version=v, module=module, table=table).exists():
        raise Http404
    if fmt == "csv":
        return _download(service.template_csv(module, table, v), f"{table}.csv", "text/csv; charset=utf-8")
    if fmt == "xlsx":
        return _download(
            service.template_xlsx(module, table, v), f"{table}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    if fmt == "json":
        return JsonResponse(service.json_schema(module, table, v), json_dumps_params={"indent": 1})
    raise Http404
