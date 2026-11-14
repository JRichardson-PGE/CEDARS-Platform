"""The filing specification pages: the list of specifications and their versions, a version's page (its tables and fields, the rules
about each field in sentences, its built-in checks), the comparison of two versions, and the downloads made from a version. Published
and withdrawn versions are public, as CEDARS' specification pages are; proposed versions are for anyone signed in, drafts for CPUC
system administrators. Anyone signed in comments on a proposed version while its comment period is open, and CPUC staff answer."""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.core import nav
from cedars.services.accounts.service import AccessDenied

from . import comments, downloads, editor, forms, historic, service, transfer
from .models import SpecComment, Specification, SpecificationVersion


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
        specs.append(
            {
                "spec": s,
                "current": current,
                "coming": service.coming(s),
                "versions": [(v, len(service.in_use(v))) for v in versions],
                "historic": [(v, historic.years_of(v)) for v in historic.versions(s)],
            }
        )
    nav_menu = "filings" if "member" in nav.roles_of(request.user) else "published"
    open_for_comment = [v for s in specs for v, _ in s["versions"] if v.open_for_comment]
    return render(
        request,
        "specifications/index.html",
        {"specs": specs, "nav_menu": nav_menu, "manage": editor.can_manage(request.user), "open_for_comment": open_for_comment},
    )


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
    manage = editor.can_manage(request.user)
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
            "manage": manage,
            "draft": editor.draft_of(v.specification) if manage else None,
            "start_form": forms.StartDraftForm(auto_id="start_%s") if manage else None,
            "coming": v.status == "published" and v.effective_on is not None and v.effective_on > timezone.localdate(),
            "provenance": transfer.provenance(v),
            **_comments_context(request, v),
            **_historic_context(v),
        },
    )


def _historic_context(v) -> dict:
    """A legacy CEDARS layout's page: its years and the legacy years imported from downloads in its layout."""
    if not v.historic:
        return {}
    from cedars.services.legacy import service as legacy

    return {"years": historic.years_of(v), "imports": legacy.imports_in_layouts(v.legacy_layouts)}


def _comments_context(request, v) -> dict:
    """The comments part of a version's page: the thread (signed in only), the form while comments are open, staff answers."""
    signed_in = request.user.is_authenticated
    has = v.proposals > 0
    org = comments.side(request.user, getattr(request, "organisation", None))
    return {
        "comments_shown": has,
        "thread": comments.thread(v) if has and signed_in else [],
        "comment_counts": comments.counts(v) if has else {},
        "comment_form": forms.CommentForm(topics=comments.topics(v), auto_id="comment_%s")
        if v.open_for_comment and org is not None
        else None,
        "comment_side": org,
        "comment_staff": comments.is_cpuc_staff(request.user),
        "max_chars": comments.max_chars(),
        "can_answer": comments.can_answer(request.user, v),
        "reply_form": forms.ReplyForm(),
        "period_form": forms.CommentPeriodForm(auto_id="period_%s") if v.status == "proposed" and editor.can_manage(request.user) else None,
    }


def _back(request, v, anchor: str = "comments-h"):
    if request.POST.get("next") == "edit" and editor.can_manage(request.user):
        return redirect(reverse("specifications:edit_comments", args=[v.specification.code, v.slug]) + f"#{anchor}")
    return redirect(reverse("specifications:version", args=[v.specification.code, v.slug]) + f"#{anchor}")


@login_required
@require_POST
def post_comment(request, code: str, slug: str):
    v = _version(request, code, slug)
    form = forms.CommentForm(request.POST, topics=comments.topics(v))
    if not form.is_valid():
        messages.error(request, "Write the comment and say what it is about.")
        return _back(request, v, "comment-form-h")
    try:
        c = comments.post(
            request.user,
            v,
            form.cleaned_data["text"],
            about=form.cleaned_data["about"],
            organisation=getattr(request, "organisation", None),
        )
    except (AccessDenied, comments.CommentRefused) as e:
        messages.error(request, str(e))
        return _back(request, v, "comment-form-h")
    messages.success(request, f"Your comment on {c.about} is posted for {c.organisation.code}. CPUC staff will answer it here.")
    return _back(request, v, f"comment-{c.pk}")


@login_required
@require_POST
def reply_comment(request, code: str, slug: str, comment: int):
    v = _version(request, code, slug)
    parent = get_object_or_404(SpecComment, pk=comment, version=v)
    form = forms.ReplyForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Write the answer.")
        return _back(request, v, f"comment-{parent.pk}")
    try:
        c = comments.reply(request.user, parent, form.cleaned_data["text"])
    except (AccessDenied, comments.CommentRefused) as e:
        messages.error(request, str(e))
        return _back(request, v, f"comment-{parent.pk}")
    messages.success(request, f"Answered {parent.author_name}'s comment; they are told.")
    return _back(request, v, f"comment-{c.pk}")


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
    if v.historic:  # a legacy CEDARS download layout is no upload layout
        raise Http404("A legacy CEDARS layout has no upload template, examples or schema.")
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
    if v.historic:  # a legacy CEDARS download layout is no upload layout
        raise Http404("A legacy CEDARS layout has no upload template, examples or schema.")
    t = v.tables.filter(name=table).first()
    if t is None:
        raise Http404("No such table.")
    body = json.dumps(downloads.json_schema(v, t), indent=1, ensure_ascii=False)
    return _file(body.encode("utf-8"), f"{downloads.file_stem(v)}_{t.name}.schema.json", "application/schema+json")


def example(request, code: str, slug: str, example: str, kind: str):
    if kind not in ("xlsx", "zip"):
        raise Http404("No such file.")
    v = _version(request, code, slug)
    if v.historic:  # a legacy CEDARS download layout is no upload layout
        raise Http404("A legacy CEDARS layout has no upload template, examples or schema.")
    e = v.examples.filter(slug=example).first()
    if e is None:
        raise Http404("No such example.")
    stem = f"{downloads.file_stem(v)}_example_{e.slug}"
    if kind == "zip":
        return _file(downloads.example_zip(v, e), f"{stem}.zip", "application/zip")
    return _file(downloads.example_xlsx(v, e), f"{stem}.xlsx", XLSX)
