from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.services.accounts.service import AccessDenied

from . import forms, service, value_lists
from .models import Code, Field, Usage, ValueList

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
        {"catalog": v, "page": page, "q": q, "kind": kind, "tables": tables, "query": query.urlencode(), "total": fields.count()},
    )


def detail(request, name):
    v = _version_or_404()
    f = Field.objects.filter(version=v, name=name).prefetch_related("aliases", "usages").first()
    if f is None:
        raise Http404
    return render(request, "dictionary/field.html", {"catalog": v, "f": f})


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


# --- value lists (code sets), kept by CPUC system administrators as versions (BR-12.6)


def _shipped_or_empty(name: str) -> list[dict]:
    try:
        return value_lists.shipped(name)
    except (value_lists.EngineMissing, value_lists.ValueListError):
        return []


def lists(request):
    """Every value list the rules and the input editor check against: the version in force, or the engine's list as shipped."""
    try:
        names = value_lists.engine_lists()
        engine = True
    except value_lists.EngineMissing:
        names, engine = [], False
    kept = {vl.name: vl for vl in ValueList.objects.all()}
    rows = []
    for name in sorted(set(names) | set(kept)):
        vl = kept.get(name)
        v = value_lists.in_force(vl) if vl else None
        rows.append(
            {
                "name": name,
                "label": vl.label if vl else value_lists.label_for(name),
                "version": v,
                "codes": v.codes.count() if v else len(_shipped_or_empty(name)),
                "draft": value_lists.draft_of(vl) if vl else None,
                "later": vl.versions.filter(status="published", effective_from__gt=timezone.localdate()).first() if vl else None,
            }
        )
    return render(request, "dictionary/lists.html", {"rows": rows, "engine": engine, "release": value_lists.engine_release()})


def _list_or_404(name: str) -> ValueList:
    try:
        return value_lists.get_list(name)
    except (value_lists.ValueListError, value_lists.EngineMissing) as e:
        raise Http404(str(e)) from e


def _filtered(request, rows):
    """Codes filtered by words, a claim year and active / retired, from the query string."""
    q, year, state = request.GET.get("q", "").strip().lower(), request.GET.get("year", "").strip(), request.GET.get("state", "")
    y = int(year) if year.isdigit() else None

    def active(r):
        if y is None:
            return r["end_year"] is None
        return (r["start_year"] is None or r["start_year"] <= y) and (r["end_year"] is None or r["end_year"] >= y)

    out = [r for r in rows if not q or q in r["code"].lower() or q in (r["description"] or "").lower()]
    if state == "active":
        out = [r for r in out if active(r)]
    elif state == "retired":
        out = [r for r in out if not active(r)]
    query = request.GET.copy()
    query.pop("page", None)
    return out, {"q": request.GET.get("q", ""), "year": year, "state": state, "query": query.urlencode()}


def _code_rows(version) -> list[dict]:
    return [
        {
            "pk": c.pk,
            "code": c.code,
            "description": c.description,
            "start_year": c.start_year,
            "end_year": c.end_year,
            "new": c.added_in_draft,
        }
        for c in version.codes.all()
    ]


def value_list(request, name):
    """One list: its codes (the version in force, another version, or the engine's list), every version with its date and reason."""
    vl = _list_or_404(name)
    versions = list(vl.versions.filter(status="published").order_by("-number"))
    current = value_lists.in_force(vl)
    pick = request.GET.get("version", "")
    shown = next((v for v in versions if str(v.number) == pick), None) if pick and pick != "shipped" else None
    if shown is None and pick != "shipped":
        shown = current
    rows = _code_rows(shown) if shown else [dict(r, pk=None, new=False) for r in _shipped_or_empty(vl.name)]
    rows, filters = _filtered(request, rows)
    if request.GET.get("format") == "csv":
        tag = f"v{shown.number}" if shown else "shipped"
        return _download(value_lists.codes_csv(rows), f"value_list_{vl.name}_{tag}.csv", "text/csv; charset=utf-8")
    can = value_lists.can_edit(request.user)
    return render(
        request,
        "dictionary/list.html",
        {
            "vl": vl,
            "versions": versions,
            "current": current,
            "shown": shown,
            "page": Paginator(rows, 100).get_page(request.GET.get("page")),
            "total": len(rows),
            "draft": value_lists.draft_of(vl) if can else None,
            "can_edit": can,
            "release": value_lists.engine_release(),
            "changes": value_lists.changes_in(shown) if shown else None,
            **filters,
        },
    )


def _require_editor(request):
    if not value_lists.can_edit(request.user):
        raise PermissionDenied("Only a CPUC system administrator changes value lists.")


def _draft_or_redirect(request, vl):
    d = value_lists.draft_of(vl)
    if d is None:
        messages.error(request, f"{vl} has no draft: start a new version first.")
    return d


@login_required
@require_POST
def start_version(request, name):
    _require_editor(request)
    vl = _list_or_404(name)
    try:
        d = value_lists.start_draft(request.user, vl)
    except (AccessDenied, value_lists.ValueListError, value_lists.EngineMissing) as e:
        messages.error(request, str(e))
        return redirect("dictionary:list", name=vl.name)
    messages.success(request, f"Started {d}, a copy of {d.based_on}. Change it, then publish it with the date it takes effect.")
    return redirect("dictionary:draft", name=vl.name)


@login_required
def draft(request, name):
    _require_editor(request)
    vl = _list_or_404(name)
    d = _draft_or_redirect(request, vl)
    if d is None:
        return redirect("dictionary:list", name=vl.name)
    rows, filters = _filtered(request, _code_rows(d))
    return render(
        request,
        "dictionary/list_draft.html",
        {
            "vl": vl,
            "draft": d,
            "changes": value_lists.changes_in(d),
            "latest": value_lists.latest_published(vl),
            "page": Paginator(rows, 100).get_page(request.GET.get("page")),
            "total": len(rows),
            "add_form": forms.CodeForm(auto_id="add_%s"),
            "upload_form": forms.UploadForm(auto_id="upload_%s"),
            "publish_form": forms.PublishForm(auto_id="publish_%s"),
            **filters,
        },
    )


def _back_to_draft(vl):
    return redirect("dictionary:draft", name=vl.name)


@login_required
@require_POST
def add_code(request, name):
    _require_editor(request)
    vl = _list_or_404(name)
    d = _draft_or_redirect(request, vl)
    if d is None:
        return redirect("dictionary:list", name=vl.name)
    form = forms.CodeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the code, and years between 1990 and 2100 if any.")
        return _back_to_draft(vl)
    c = form.cleaned_data
    try:
        code = value_lists.add_code(
            request.user, d, c["code"], description=c["description"], start_year=c["start_year"], end_year=c["end_year"], reason=c["reason"]
        )
    except (AccessDenied, value_lists.ValueListError) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Added {code.code} to the draft.")
    return _back_to_draft(vl)


@login_required
@require_POST
def upload_codes(request, name):
    _require_editor(request)
    vl = _list_or_404(name)
    d = _draft_or_redirect(request, vl)
    if d is None:
        return redirect("dictionary:list", name=vl.name)
    form = forms.UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose the file and give the reason.")
        return _back_to_draft(vl)
    try:
        out = value_lists.upload_codes(request.user, d, form.cleaned_data["file"].read(), form.cleaned_data["reason"])
    except (AccessDenied, value_lists.ValueListError) as e:
        messages.error(request, f"Nothing was changed: {e}")
    else:
        messages.success(
            request, f"From the file: {out['added']} codes added, {out['changed']} changed, {out['unchanged']} already as given."
        )
    return _back_to_draft(vl)


@login_required
def code(request, name, pk):
    _require_editor(request)
    vl = _list_or_404(name)
    c = get_object_or_404(Code.objects.select_related("version"), pk=pk, version__value_list=vl, version__status="draft")
    carried = not c.added_in_draft
    if request.method == "POST":
        form = forms.ChangeCodeForm(request.POST, carried=carried)
        if form.is_valid():
            v = form.cleaned_data
            changes = {"description": v["description"], "end_year": v["end_year"]}
            if not carried:
                changes["start_year"] = v["start_year"]
            try:
                value_lists.change_code(request.user, c, reason=v["reason"], **changes)
            except (AccessDenied, value_lists.ValueListError) as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, f"Saved {c.code}.")
                return _back_to_draft(vl)
    else:
        form = forms.ChangeCodeForm(
            initial={"description": c.description, "start_year": c.start_year, "end_year": c.end_year}, carried=carried
        )
    return render(request, "dictionary/code.html", {"vl": vl, "c": c, "form": form, "carried": carried})


@login_required
@require_POST
def drop_code(request, name, pk):
    _require_editor(request)
    vl = _list_or_404(name)
    c = get_object_or_404(Code, pk=pk, version__value_list=vl, version__status="draft")
    try:
        value_lists.drop_code(request.user, c, request.POST.get("reason", ""))
    except (AccessDenied, value_lists.ValueListError) as e:
        messages.error(request, str(e))
        return redirect("dictionary:code", name=vl.name, pk=c.pk)
    messages.success(request, f"Took {c.code} out of the draft.")
    return _back_to_draft(vl)


@login_required
@require_POST
def publish_list(request, name):
    _require_editor(request)
    vl = _list_or_404(name)
    d = _draft_or_redirect(request, vl)
    if d is None:
        return redirect("dictionary:list", name=vl.name)
    form = forms.PublishForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the date the version takes effect and why the list changes.")
        return _back_to_draft(vl)
    try:
        v = value_lists.publish(request.user, d, form.cleaned_data["effective_from"], form.cleaned_data["reason"])
    except (AccessDenied, value_lists.ValueListError) as e:
        messages.error(request, str(e))
        return _back_to_draft(vl)
    messages.success(request, f"Published {v}. It is in force from {v.effective_from:%Y-%m-%d} until a newer version.")
    return redirect("dictionary:list", name=vl.name)


@login_required
@require_POST
def discard_list_draft(request, name):
    _require_editor(request)
    vl = _list_or_404(name)
    d = _draft_or_redirect(request, vl)
    if d is not None:
        value_lists.discard_draft(request.user, d, request.POST.get("reason", ""))
        messages.success(request, f"The draft of {vl} is discarded.")
    return redirect("dictionary:list", name=vl.name)
