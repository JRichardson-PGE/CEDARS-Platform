from datetime import datetime, time

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry import service as registry

from . import service
from .models import Deadline, ReopenRequest, WorkflowPolicy

MODULES = [("budgets", "Budgets"), ("claims", "Claims"), ("monthly", "Monthly reports")]


class DeadlineForm(forms.Form):
    module = forms.ChoiceField(label="Module", choices=MODULES)
    kind = forms.CharField(label="What is due", max_length=80, help_text="e.g. quarterly claim, TUAL, monthly report")
    year = forms.IntegerField(label="Program year", min_value=2000, max_value=2100)
    period = forms.ChoiceField(
        label="Period",
        choices=[("year", "The year")] + [(f"q{q}", f"Quarter {q}") for q in range(1, 5)] + [(f"m{m}", f"Month {m}") for m in range(1, 13)],
    )
    due = forms.DateField(label="Due date", widget=forms.DateInput(attrs={"type": "date"}))


def _period(year: int, code: str):
    periods = {(p.kind, p.number): p for p in registry.ensure_periods(year)}
    if code == "year":
        return periods[("year", 0)]
    return periods[("quarter" if code[0] == "q" else "month", int(code[1:]))]


def calendar(request):
    """The filing calendar: what each module expects and when (BR-09.4). Public (BR-01.5); a CPUC system administrator adds deadlines
    here."""
    sysadmin = access.is_system_admin(request.user)
    form = DeadlineForm(request.POST or None)
    if request.method == "POST" and sysadmin and form.is_valid():
        d = form.cleaned_data
        due = timezone.make_aware(datetime.combine(d["due"], time(23, 59, 59)))
        try:
            service.add_deadline(request.user, module=d["module"], kind=d["kind"], period=_period(d["year"], d["period"]), due_at=due)
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Deadline added.")
        return redirect("submissions:calendar")
    deadlines = Deadline.objects.select_related("period", "cycle").filter(due_at__gte=timezone.now() - timezone.timedelta(days=400))
    policies = {p.module: p for p in WorkflowPolicy.objects.all()}
    return render(
        request,
        "submissions/calendar.html",
        {
            "deadlines": deadlines,
            "form": form if sysadmin else None,
            "policies": [(code, label, policies.get(code)) for code, label in MODULES],
        },
    )


@login_required
def reopen_requests(request):
    """Requests to correct filings after their due date, for approvers and their delegates."""
    pending = []
    for r in ReopenRequest.objects.filter(status=ReopenRequest.Status.PENDING).select_related(
        "submission__deadline", "submission__organisation", "requested_by"
    ):
        approver = service.approver_for(request.user, r.submission.module)
        if approver is not None:
            pending.append((r, approver))
    decided = ReopenRequest.objects.exclude(status=ReopenRequest.Status.PENDING).select_related("submission__organisation", "decided_by")[
        :50
    ]
    return render(request, "submissions/reopen_requests.html", {"pending": pending, "decided": decided})


@login_required
@require_POST
def decide(request, pk):
    req = get_object_or_404(ReopenRequest, pk=pk)
    approve = request.POST.get("decision") == "approve"
    try:
        service.decide_reopen(request.user, req, approve, request.POST.get("note", ""))
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{'Approved' if approve else 'Declined'}: {req.submission}.")
    return redirect("submissions:reopen_requests")
