from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from rest_framework.throttling import BaseThrottle

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import forms, service
from .models import Feedback


def form(request):
    """The form behind every page's Feedback link: anyone may send, signed in or not."""
    page = service.safe_page(request.POST.get("page") if request.method == "POST" else request.GET.get("from", ""))
    f = forms.FeedbackForm(request.POST or None, initial={"page": page})
    if request.method == "POST" and f.is_valid():
        try:
            service.submit(
                page=page,
                message=f.cleaned_data["message"],
                user=request.user,
                organisation=getattr(request, "organisation", None),
                address=BaseThrottle().get_ident(request),
            )
        except service.FeedbackRefused as e:
            f.add_error("message", str(e))
        else:
            messages.success(request, "Thank you: your feedback was sent to the CPUC's CEDARS team.")
            return redirect(page)
    return render(request, "feedback/form.html", {"form": f, "page": page})


def _require_admin(request):
    if not access.is_system_admin(request.user):
        raise Http404


@login_required
def inbox(request):
    _require_admin(request)
    status = request.GET.get("status", "new")
    qs = Feedback.objects.select_related("user", "organisation")
    if status in Feedback.Status.values:
        qs = qs.filter(status=status)
    else:
        status = "all"
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    counts = {s: Feedback.objects.filter(status=s).count() for s in Feedback.Status.values}
    return render(request, "feedback/inbox.html", {"page": page, "status": status, "counts": counts})


@login_required
def detail(request, pk):
    _require_admin(request)
    fb = get_object_or_404(Feedback.objects.select_related("user", "organisation", "closed_by"), pk=pk)
    return render(
        request,
        "feedback/detail.html",
        {"fb": fb, "close_form": forms.CloseForm(auto_id="close_%s"), "reopen_form": forms.ReopenForm(auto_id="reopen_%s")},
    )


@login_required
@require_POST
def close(request, pk):
    _require_admin(request)
    fb = get_object_or_404(Feedback, pk=pk)
    f = forms.CloseForm(request.POST)
    try:
        service.close(request.user, fb, f.cleaned_data["note"] if f.is_valid() else "")
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("feedback:detail", pk)
    messages.success(request, "Feedback closed.")
    return redirect("feedback:inbox")


@login_required
@require_POST
def reopen(request, pk):
    _require_admin(request)
    fb = get_object_or_404(Feedback, pk=pk)
    f = forms.ReopenForm(request.POST)
    try:
        service.reopen(request.user, fb, f.cleaned_data["reason"] if f.is_valid() else "")
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("feedback:detail", pk)
    messages.success(request, "Feedback reopened.")
    return redirect("feedback:detail", pk)
