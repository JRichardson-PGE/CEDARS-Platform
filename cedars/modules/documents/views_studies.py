"""Pages of the study register (BR-15.6 to 15.8) and the work orders (BR-15.3). The register, each study, the budget summary and the work
orders are public, as the PSR is; changes need an account (studies.py says who)."""

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.services.accounts.service import AccessDenied

from . import aging, forms, service, studies
from .models import AttributeOption, Study, StudyProgram, WorkOrder

PER_PAGE = 25
REGISTER_COLUMNS = [
    "StudyID",
    "Link",
    "StudyName",
    "StudyLead",
    "StudyManager",
    "WorkOrder",
    "StudyType",
    "Sectors",
    "Group",
    "FundingCycles",
    "Budget",
    "Status",
    "NextActivity",
    "NextActivityDue",
    "CompletionDate",
    "CALMACID",
    "ProgramsEvaluated",
    "Description",
    "Objective",
    "ResearchQuestions",
    "Methods",
    "Notes",
    "LastUpdated",
]


def _labels(qs) -> str:
    return "; ".join(o.label for o in qs.all())


def _coverage_text(s: Study) -> str:
    return "; ".join(f"{c.program.prg_id} {c.years}{'' if c.whole else ' (part)'}" for c in s.coverage.all())


def _row(s: Study, base: str) -> list:
    return [
        s.study_id,
        f"{base}{s.number}/",
        s.title,
        s.lead.label,
        s.manager.code,
        s.work_order.number if s.work_order else "",
        s.study_type.label if s.study_type else "",
        _labels(s.sectors),
        s.group.label if s.group else "",
        _labels(s.funding_cycles),
        s.budget if s.budget is not None else "",
        s.status.label if s.status else "",
        s.next_activity,
        s.next_activity_due.isoformat() if s.next_activity_due else "",
        s.completion_date.isoformat() if s.completion_date else "",
        s.calmac_id,
        _coverage_text(s),
        s.description,
        s.objective,
        s.research_questions,
        s.methods,
        s.notes,
        s.updated_at.date().isoformat(),
    ]


def register(request):
    """Every study, filtered as the PSR's page filters, with its activity report (next activities due, recently updated)."""
    form = forms.RegisterForm(request.GET or None)
    rows = studies.register(form.filter())
    page = Paginator(rows, PER_PAGE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "documents/studies.html",
        {
            "form": form,
            "page": page,
            "total": page.paginator.count,
            "query": query.urlencode(),
            "upcoming": studies.upcoming(),
            "recent": studies.recently_updated(),
            "can_create": request.user.is_authenticated and studies.can_create(request.user, getattr(request, "organisation", None)),
            "show_aging": _sees_aging(request.user),
        },
    )


def register_csv(request):
    """The register as a file (BR-15.6, BR-15.8: a public data download), every public field, from the same filter."""
    rows = studies.register(forms.RegisterForm(request.GET or None).filter())
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="evaluation_studies.csv"'
    w = csv.writer(response, lineterminator="\n")
    w.writerow(REGISTER_COLUMNS)
    base = request.build_absolute_uri("/documents/studies/")
    for s in rows:
        w.writerow(_row(s, base))
    return response


def _study(number) -> Study:
    return get_object_or_404(
        Study.objects.select_related("study_type", "group", "lead", "status", "manager", "work_order", "ed_program_manager"),
        number=number,
    )


def study(request, number):
    """A study's page: its fields, the programs and years it evaluates, its documents and its history (status and budget changes)."""
    s = _study(number)
    can_edit = studies.can_edit(request.user, s) if request.user.is_authenticated else False
    contacts = studies.sees_contacts(request.user, s) if request.user.is_authenticated else False
    docs = service.readable(request.user).filter(study=s).select_related("document_type").order_by("-published_at", "-created_at")
    return render(
        request,
        "documents/study.html",
        {
            "s": s,
            "sectors": list(s.sectors.all()),
            "cycles": list(s.funding_cycles.all()),
            "coverage": list(s.coverage.select_related("program")),
            "documents": [(d, studies.stage_of(d)) for d in docs],
            "history": studies.history(s, public=not contacts),
            "can_edit": can_edit,
            "sees_contacts": contacts,
            "coverage_form": forms.CoverageForm() if can_edit else None,
        },
    )


def study_report(request, number):
    """One study as a file (the PSR's per-project report): each field on its own row, then the programs evaluated and the documents."""
    s = _study(number)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="study_{s.study_id}.csv"'
    w = csv.writer(response, lineterminator="\n")
    w.writerow(["Field", "Value"])
    for name, value in zip(REGISTER_COLUMNS, _row(s, request.build_absolute_uri("/documents/studies/")), strict=True):
        w.writerow([name, value])
    for c in s.coverage.select_related("program"):
        w.writerow(
            ["ProgramEvaluated", f"{c.program.prg_id}, {c.years}, {'whole' if c.whole else 'part'}{', ' + c.note if c.note else ''}"]
        )
    for d in service.readable(request.user).filter(study=s).select_related("document_type"):
        w.writerow(["Document", f"{d.title} ({d.document_type.label}) {request.build_absolute_uri(d.get_absolute_url())}"])
    return response


@login_required
def new_study(request):
    org = getattr(request, "organisation", None)
    if not studies.can_create(request.user, org):
        raise Http404
    admin = service.is_admin(request.user)
    form = forms.StudyForm(request.POST or None, organisation=org, admin=admin)
    if request.method == "POST" and form.is_valid():
        try:
            s = studies.create_study(request.user, org, form.data_object())
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Study {s.study_id} is added to the register.")
            return redirect(s)
    return render(request, "documents/study_form.html", {"form": form, "s": None})


@login_required
def change_study(request, number):
    s = _study(number)
    if not studies.can_edit(request.user, s):
        raise Http404
    form = forms.ChangeStudyForm(request.POST or None, study=s, admin=service.is_admin(request.user))
    if request.method == "POST" and form.is_valid():
        try:
            studies.change_study(request.user, s, form.data_object(), reason=form.cleaned_data["reason"])
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "The study is updated.")
            return redirect(s)
    return render(request, "documents/study_form.html", {"form": form, "s": s})


@login_required
@require_POST
def add_coverage(request, number):
    s = _study(number)
    form = forms.CoverageForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "Give a program in the registry and its first and last years. "
            + " ".join(str(e) for errors in form.errors.values() for e in errors),
        )
        return redirect(s)
    d = form.cleaned_data
    try:
        studies.set_coverage(request.user, s, d["program"], d["first_year"], d["last_year"], whole=d["whole"], note=d["note"])
    except (service.DocumentError, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{d['program'].prg_id} is listed as evaluated by this study.")
    return redirect(s)


@login_required
@require_POST
def remove_coverage(request, number, pk):
    s = _study(number)
    row = get_object_or_404(StudyProgram, pk=pk, study=s)
    try:
        studies.remove_coverage(request.user, row, reason=request.POST.get("reason", ""))
    except (service.DocumentError, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{row.program.prg_id} is no longer listed.")
    return redirect(s)


def summary(request):
    """The EM&V plan's study and budget tables for a funding cycle (BR-15.8)."""
    cycles = AttributeOption.objects.filter(attribute_list__code="portfolio_cycle")
    cycle = None
    if request.GET.get("cycle"):
        cycle = cycles.filter(pk=request.GET["cycle"]).first() if request.GET["cycle"].isdigit() else None
        if cycle is None:
            raise Http404
    result = studies.summary(cycle)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        name = cycle.label if cycle else "all"
        response["Content-Disposition"] = f'attachment; filename="study_budgets_{name}.csv"'
        w = csv.writer(response, lineterminator="\n")
        w.writerow(["FundingCycle", "By", "Value", "Studies", "Budget", "CompletedStudies", "CompletedBudget"])
        for table in result["tables"].values():
            for key, row in table["rows"]:
                w.writerow([name, table["title"], key, row["studies"], row["budget"], row["completed"], row["completed_budget"]])
        t = result["total"]
        w.writerow([name, "Total", "", t["studies"], t["budget"], t["completed"], t["completed_budget"]])
        return response
    return render(request, "documents/summary.html", {"cycles": cycles, "cycle": cycle, **result})


# --- work orders


def work_orders(request):
    rows = WorkOrder.objects.select_related("firm").prefetch_related("tasks")
    admin = request.user.is_authenticated and service.is_admin(request.user)
    return render(
        request,
        "documents/work_orders.html",
        {"rows": rows, "admin": admin, "form": forms.WorkOrderForm() if admin else None},
    )


@login_required
@require_POST
def add_work_order(request):
    form = forms.WorkOrderForm(request.POST)
    try:
        if not form.is_valid():
            raise service.DocumentError("Give the work order number.")
        wo = studies.add_work_order(request.user, **form.cleaned_data)
    except (service.DocumentError, AccessDenied) as e:
        messages.error(request, str(e))
        return redirect("documents:work_orders")
    messages.success(request, f"Work order {wo.number} is added.")
    return redirect(wo)


def work_order(request, pk):
    wo = get_object_or_404(WorkOrder.objects.select_related("firm"), pk=pk)
    docs = service.readable(request.user).filter(work_order=wo).select_related("document_type", "task").order_by("-published_at")
    admin = request.user.is_authenticated and service.is_admin(request.user)
    return render(
        request,
        "documents/work_order.html",
        {
            "wo": wo,
            "tasks": list(wo.tasks.all()),
            "studies": list(wo.studies.select_related("status")),
            "documents": list(docs),
            "admin": admin,
        },
    )


@login_required
@require_POST
def work_order_change(request, pk, action):
    wo = get_object_or_404(WorkOrder, pk=pk)
    try:
        if action == "task":
            t = studies.add_task(request.user, wo, request.POST.get("name", ""))
            messages.success(request, f"Task {t.name} is added.")
        elif action in ("cancel", "reinstate"):
            studies.set_cancelled(request.user, wo, action == "cancel", reason=request.POST.get("reason", ""))
            messages.success(request, f"Work order {wo.number} is {'cancelled' if action == 'cancel' else 'reinstated'}.")
        else:
            raise Http404
    except (service.DocumentError, AccessDenied) as e:
        messages.error(request, str(e))
    return redirect(wo)


# --- the aging review (Jake, 2026-10-30)


def _sees_aging(user) -> bool:
    return user.is_authenticated and (service.is_admin(user) or service._cpuc_has(user, *service.ANY))


@login_required
def aging_review(request):
    """For CPUC staff: what each study manager (and each PA asked to respond) owes, and for how long."""
    if not _sees_aging(request.user):
        raise Http404
    stale = request.GET.get("stale", "")
    stale_days = int(stale) if stale.isdigit() and 0 < int(stale) <= 3650 else aging.STALE_DAYS
    day = timezone.localdate()
    found = aging.items(day, stale_days)
    kind = request.GET.get("kind", "")
    shown = [i for i in found if i.kind == kind] if kind in aging.KINDS else found
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="emv_aging_{day:%Y%m%d}.csv"'
        w = csv.writer(response, lineterminator="\n")
        w.writerow(["Owes", "Item", "StudyID", "What", "Since", "DaysWaiting", "AgeBand", "Link"])
        for i in shown:
            w.writerow(
                [
                    i.owner.code,
                    i.kind_label,
                    i.study.study_id if i.study else "",
                    i.what,
                    i.since.isoformat(),
                    i.age(day),
                    aging.bucket(i.age(day)),
                    request.build_absolute_uri(i.url),
                ]
            )
        return response
    return render(
        request,
        "documents/aging.html",
        {
            "rows": aging.by_owner(found, day),
            "items": [(i, i.age(day), aging.bucket(i.age(day))) for i in sorted(shown, key=lambda i: i.since)],
            "kinds": list(aging.KINDS.items()),
            "kind": kind,
            "buckets": [label for _, _, label in aging.BUCKETS],
            "stale_days": stale_days,
            "query": request.GET.urlencode(),
        },
    )
