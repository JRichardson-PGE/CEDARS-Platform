"""The filing specification pages: the list of specifications and their versions, a version's page (its tables and fields, the rules
about each field in sentences, its built-in checks), the comparison of two versions, and the downloads made from a version. Published
and withdrawn versions are public, as CEDARS' specification pages are; proposed versions are for anyone signed in, drafts for CPUC
system administrators."""

import json

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from cedars.core import nav

from . import downloads, service
from .models import Specification, SpecificationVersion


def _version(request, code: str, slug: str) -> SpecificationVersion:
    v = (
        SpecificationVersion.objects.select_related("specification", "rule_set", "cost_test_set")
        .filter(specification__code=code, slug=slug)
        .first()
    )
    if v is None or not service.can_read(request.user, v):
        raise Http404("No such specification version.")
    return v


def index(request):
    specs = []
    for s in Specification.objects.all():
        current = service.current(s)
        versions = service.readable_versions(request.user, s)
        specs.append({"spec": s, "current": current, "versions": [(v, len(service.in_use(v))) for v in versions]})
    nav_menu = "filings" if "member" in nav.roles_of(request.user) else "published"
    return render(request, "specifications/index.html", {"specs": specs, "nav_menu": nav_menu})


def current(request, code: str):
    s = get_object_or_404(Specification, code=code)
    v = service.current(s)
    if v is None:
        raise Http404("This specification has no published version yet.")
    return redirect("specifications:version", code, v.slug)


def version(request, code: str, slug: str):
    v = _version(request, code, slug)
    by_field, by_table = service.rules_by_field(v)
    tables = []
    for t in v.tables.prefetch_related("fields"):
        fields = [{"f": f, "rules": by_field.get((t.name, f.name), [])} for f in t.fields.all()]
        tables.append({"t": t, "fields": fields, "rules": by_table.get(t.name, [])})
    labels = service.check_group_labels()
    checks: dict[str, list] = {}
    for c in v.checks.all():
        checks.setdefault(c.group, []).append(c)
    checks = [(g, labels.get(g, g), cs) for g, cs in checks.items()]
    current_version = service.current(v.specification)
    return render(
        request,
        "specifications/version.html",
        {
            "v": v,
            "tables": tables,
            "checks": checks,
            "settings": service.settings_rows(v),
            "in_use": service.in_use(v),
            "is_current": current_version is not None and current_version.pk == v.pk,
            "others": [o for o in service.readable_versions(request.user, v.specification) if o.pk != v.pk],
            "has_vault": any(x["t"].vault for x in tables),
            "rule_count": sum(len(x["rules"]) for t in tables for x in t["fields"]) + sum(len(t["rules"]) for t in tables),
        },
    )


def _pick(request, key: str):
    raw = request.GET.get(key, "")
    code, _, slug = raw.partition("/")
    if not code or not slug:
        return None
    try:
        return _version(request, code, slug)
    except Http404:
        return None


def compare(request):
    choices = [
        (f"{v.specification.code}/{v.slug}", str(v))
        for s in Specification.objects.all()
        for v in service.readable_versions(request.user, s)
    ]
    a, b = _pick(request, "a"), _pick(request, "b")
    result = service.compare(a, b) if a and b else None
    asked = bool(request.GET.get("a") or request.GET.get("b"))
    return render(
        request,
        "specifications/compare.html",
        {"choices": choices, "a": a, "b": b, "result": result, "picked": (request.GET.get("a", ""), request.GET.get("b", "")),
         "missing": asked and not result},
    )  # fmt: skip


def _file(content: bytes, name: str, content_type: str) -> HttpResponse:
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def template(request, code: str, slug: str, kind: str):
    if kind not in ("xlsx", "zip", "pii-xlsx", "pii-zip"):
        raise Http404("No such template.")
    v = _version(request, code, slug)
    vault = kind.startswith("pii")
    if vault and not v.tables.filter(vault=True).exists():
        raise Http404("This version has no PII tables.")
    stem = downloads.file_stem(v) + ("_pii_template" if vault else "_template")
    if kind.endswith("zip"):
        return _file(downloads.template_zip(v, vault=vault), f"{stem}.zip", "application/zip")
    return _file(downloads.template_xlsx(v, vault=vault), f"{stem}.xlsx", XLSX)


def fields(request, code: str, slug: str):
    v = _version(request, code, slug)
    return _file(downloads.fields_xlsx(v), f"{downloads.file_stem(v)}_fields_and_rules.xlsx", XLSX)


def document(request, code: str, slug: str):
    v = _version(request, code, slug)
    body = json.dumps(downloads.document(v, request.build_absolute_uri("/").rstrip("/")), indent=1, ensure_ascii=False)
    return _file(body.encode("utf-8"), f"{downloads.file_stem(v)}.json", "application/json")


def schema(request, code: str, slug: str, table: str):
    v = _version(request, code, slug)
    t = v.tables.filter(name=table).first()
    if t is None:
        raise Http404("No such table.")
    body = json.dumps(downloads.json_schema(v, t), indent=1, ensure_ascii=False)
    return _file(body.encode("utf-8"), f"{downloads.file_stem(v)}_{t.name}.schema.json", "application/schema+json")


def example(request, code: str, slug: str, example: str, kind: str):
    if kind not in ("xlsx", "zip"):
        raise Http404("No such file.")
    v = _version(request, code, slug)
    e = v.examples.filter(slug=example).first()
    if e is None:
        raise Http404("No such example.")
    stem = f"{downloads.file_stem(v)}_example_{e.slug}"
    if kind == "zip":
        return _file(downloads.example_zip(v, e), f"{stem}.zip", "application/zip")
    return _file(downloads.example_xlsx(v, e), f"{stem}.xlsx", XLSX)
