from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import checks, forms, service
from .models import FileCheck

STATES = [("open", "Needing review"), ("reviewed", "Reviewed"), ("clear", "Nothing found"), ("all", "All")]


def _require_admin(request):
    if not access.is_system_admin(request.user):
        raise Http404


@login_required
def files(request):
    """Every checked upload the filters select, newest first: by default the ones with findings no one has reviewed."""
    _require_admin(request)
    state = request.GET.get("state", "open")
    state = state if state in dict(STATES) else "open"
    kind, code, source = request.GET.get("kind", ""), request.GET.get("code", ""), request.GET.get("source", "")
    qs = service.listing(state=state, kind=kind, code=code, source=source)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    counts = service.counts()
    query = urlencode([(k, v) for k, v in (("state", state), ("kind", kind), ("code", code), ("source", source)) if v])
    return render(
        request,
        "accessibility/files.html",
        {
            "page": page,
            "state": state,
            "states": [(s, label, counts[s]) for s, label in STATES],
            "kind": kind,
            "code": code,
            "source": source,
            "codes": [(c, d["label"]) for c, d in checks.CODES.items()],
            "sources": FileCheck.Source.choices,
            "query": query,
            "cfg": service.settings_row(),
        },
    )


@login_required
def file(request, pk):
    _require_admin(request)
    fc = get_object_or_404(FileCheck.objects.select_related("organisation", "uploaded_by", "reviewed_by"), pk=pk)
    return render(
        request,
        "accessibility/file.html",
        {
            "fc": fc,
            "findings": [(f, checks.CODES[f.code]) for f in fc.finding_list],
            "review_form": forms.ReviewForm(auto_id="review_%s"),
            "reopen_form": forms.ReopenForm(auto_id="reopen_%s"),
        },
    )


@login_required
@require_POST
def review(request, pk):
    _require_admin(request)
    fc = get_object_or_404(FileCheck, pk=pk)
    f = forms.ReviewForm(request.POST)
    try:
        service.review(request.user, fc, f.cleaned_data["note"] if f.is_valid() else "")
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("accessibility:file", pk)
    messages.success(request, f"{fc.file_name} is marked reviewed.")
    return redirect("accessibility:files")


@login_required
@require_POST
def reopen(request, pk):
    _require_admin(request)
    fc = get_object_or_404(FileCheck, pk=pk)
    f = forms.ReopenForm(request.POST)
    try:
        service.reopen(request.user, fc, f.cleaned_data["reason"] if f.is_valid() else "")
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{fc.file_name} is back among the files needing review.")
    return redirect("accessibility:file", pk)


@login_required
def check_settings(request):
    _require_admin(request)
    if request.method == "POST":
        try:
            service.set_settings(
                request.user,
                block=request.POST.get("block") == "on",
                blocking=request.POST.getlist("blocking"),
                reason=request.POST.get("reason", ""),
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Settings saved. They apply to the next upload; files already stored are not checked again.")
        return redirect("accessibility:settings")
    cfg = service.settings_row()
    return render(
        request,
        "accessibility/settings.html",
        {"cfg": cfg, "codes": [(c, d, c in cfg.blocking) for c, d in checks.CODES.items()]},
    )
