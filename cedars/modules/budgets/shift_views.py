"""Fund shift and operating budget pages (BR-14.4, BR-04.7)."""

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry.models import FilingCycle

from . import forms, shifts
from .models import FundShift

BLANK_ROWS = 4


def _shift(request, pk) -> FundShift:
    s = get_object_or_404(FundShift.objects.select_related("organisation", "cycle", "created_by", "submitted_by"), pk=pk)
    if not shifts.can_read(request.user, s):
        raise Http404
    return s


@login_required
def shift_list(request):
    org = request.organisation
    return render(
        request,
        "budgets/shifts.html",
        {
            "org": org,
            "shifts": shifts.readable(request.user).order_by("-created_at")[:200],
            "waiting": shifts.waiting_for(org) if org and shifts.access.has_permission(request.user, org, "budgets.confirm") else [],
            "can_submit": shifts.can_submit(request.user, org),
            "cycles": shifts.cycles(),
        },
    )


@login_required
def shift_new(request):
    org = request.organisation
    if not shifts.can_submit(request.user, org):
        raise Http404
    form = forms.ShiftForm(request.POST or None, cycles=shifts.cycles())
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            s = shifts.create(request.user, org, d["cycle"], title=d["title"], reason=d["reason"])
        except (AccessDenied, shifts.ShiftInvalid) as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Fund shift started. Enter the amounts it moves.")
            return redirect("budgets:shift_lines", s.pk)
    return render(request, "budgets/shift_new.html", {"form": form, "org": org})


@login_required
def shift(request, pk):
    s = _shift(request, pk)
    org = request.organisation
    acks = {a.organisation.code: a for a in s.acknowledgements.select_related("organisation", "acknowledged_by")}
    editable = s.status == FundShift.Status.DRAFT and shifts.can_submit(request.user, s.organisation) and org == s.organisation
    return render(
        request,
        "budgets/shift.html",
        {
            "s": s,
            "lines": list(s.lines.all()),
            "totals": shifts.totals(s),
            "problems": shifts.problems(s) if s.status == FundShift.Status.DRAFT else [],
            "warnings": shifts.warnings(s) if s.status in (FundShift.Status.DRAFT, FundShift.Status.WAITING) else [],
            "editable": editable,
            "can_withdraw": s.status in (FundShift.Status.DRAFT, FundShift.Status.WAITING)
            and shifts.can_submit(request.user, s.organisation)
            and org == s.organisation,
            "can_acknowledge": shifts.can_acknowledge(request.user, s, org),
            "cofunders": [(code, items, acks.get(code)) for code, items in (s.cofunders or {}).items()],
            "org": org,
        },
    )


@login_required
def shift_lines(request, pk):
    s = _shift(request, pk)
    if not (
        s.status == FundShift.Status.DRAFT and shifts.can_submit(request.user, s.organisation) and request.organisation == s.organisation
    ):
        raise Http404
    problems = []
    if request.method == "POST":
        entered = [
            shifts.LineIn(p, y, a)
            for p, y, a in zip(request.POST.getlist("prg_id"), request.POST.getlist("year"), request.POST.getlist("amount"), strict=False)
        ]
        try:
            shifts.set_lines(request.user, s, entered)
        except shifts.ShiftInvalid as e:
            problems = e.problems
        except AccessDenied as e:
            messages.error(request, str(e))
            return redirect("budgets:shift", pk)
        else:
            if "more" in request.POST:
                return redirect(f"{request.path}?more={int(request.GET.get('more', 0) or 0) + BLANK_ROWS}")
            messages.success(request, "Lines saved.")
            return redirect("budgets:shift", pk)
        rows = [{"prg_id": ln.prg_id, "year": ln.year, "amount": ln.amount} for ln in entered if not ln.blank()]
    else:
        rows = [{"prg_id": ln.prg_id, "year": str(ln.year), "amount": f"{ln.amount}"} for ln in s.lines.all()]
    extra = BLANK_ROWS + (int(request.GET.get("more", 0)) if str(request.GET.get("more", "")).isdigit() else 0)
    rows += [{"prg_id": "", "year": "", "amount": ""}] * extra
    return render(
        request,
        "budgets/shift_lines.html",
        {
            "s": s,
            "rows": rows,
            "years": [str(y) for y in range(s.cycle.first_year, s.cycle.last_year + 1)],
            "problems": problems,
            "programs": shifts.programs_for(s),
        },
    )


def _act(request, pk, fn, done: str):
    s = _shift(request, pk)
    try:
        fn(s)
    except (AccessDenied, shifts.ShiftInvalid) as e:
        for p in getattr(e, "problems", None) or [str(e)]:
            messages.error(request, p)
    else:
        messages.success(request, done)
    return redirect("budgets:shift", pk)


@login_required
@require_POST
def shift_submit(request, pk):
    def do(s):
        s = shifts.submit(request.user, s)
        if s.status == FundShift.Status.WAITING:
            messages.info(request, f"Waiting for {', '.join(s.cofunders)} to acknowledge it.")

    return _act(request, pk, do, "Fund shift submitted.")


@login_required
@require_POST
def shift_withdraw(request, pk):
    return _act(request, pk, lambda s: shifts.withdraw(request.user, s, request.POST.get("reason", "")), "Fund shift withdrawn.")


@login_required
@require_POST
def shift_acknowledge(request, pk):
    return _act(
        request,
        pk,
        lambda s: shifts.acknowledge(request.user, s, request.organisation, request.POST.get("note", "")),
        "Acknowledged.",
    )


# --- the operating budget


def _operating(request, first_year):
    cycle = get_object_or_404(FilingCycle, first_year=first_year)
    code = request.GET.get("org")
    org = get_object_or_404(Organisation, code=code) if code else request.organisation
    if org is None or not shifts.can_read_operating(request.user, org):
        raise Http404
    return shifts.operating_budget(org, cycle)


def _year(request, ob) -> int | None:
    y = request.GET.get("year", "")
    return int(y) if y.isdigit() and int(y) in ob.years else None


@login_required
def operating(request, first_year):
    ob = _operating(request, first_year)
    year = _year(request, ob)
    rows = [r for r in ob.rows if year is None or r.year == year]
    by_year = [(y, ob.total([y])) for y in ob.years]
    return render(
        request,
        "budgets/operating.html",
        {
            "ob": ob,
            "rows": rows,
            "year": year,
            "total": ob.total([year] if year else None),
            "by_year": by_year,
            "orgs": Organisation.objects.filter(fund_shifts__cycle=ob.cycle).distinct()
            | Organisation.objects.filter(submissions__deadline__module="budgets", submissions__deadline__cycle=ob.cycle).distinct(),
        },
    )


@login_required
def operating_csv(request, first_year):
    ob = _operating(request, first_year)
    year = _year(request, ob)
    resp = HttpResponse(content_type="text/csv")
    name = f"operating_budget_{ob.organisation.code}_{ob.cycle}{'_' + str(year) if year else ''}.csv"
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    w = csv.writer(resp)
    w.writerow(["PA", "PrgID", "Program", "Year", "Approved budget", "Fund shifts", "Operating budget"])
    for r in ob.rows:
        if year is None or r.year == year:
            w.writerow([ob.organisation.code, r.prg_id, r.name, r.year, r.approved, r.shifted, r.operating])
    t = ob.total([year] if year else None)
    w.writerow([ob.organisation.code, "Total", "", year or "", t.approved, t.shifted, t.operating])
    return resp
