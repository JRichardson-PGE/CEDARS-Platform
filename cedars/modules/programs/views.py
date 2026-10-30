import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from cedars.services import audit
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry import service as registry
from cedars.services.registry.models import AllocationEntry, Program

from . import forms

PER_PAGE = 50
STATUS_LABELS = {
    "new": "New",
    "active": "Active",
    "closed_to_new_commitments": "Closed to new commitments",
    "closure_pending": "Closure pending",
    "closed": "Closed",
}
SEGMENT_LABELS = {
    "resource_acquisition": "Resource acquisition",
    "market_support": "Market support",
    "equity": "Equity",
    "codes_and_standards": "Codes and standards",
    "evaluation_measurement_verification": "EM&V",
}
CSV_COLUMNS = [
    "PA",
    "PrgID",
    "ProgramName",
    "ProgramStatus",
    "ProgramSegment",
    "PrimarySector",
    "BPSector",
    "ProgramImplementer",
    "ProgramCategory",
    "ImplementationContractor",
    "StartYear",
    "EndYear",
    "StatewideProgram",
    "ParentProgram",
    "ValidFrom",
    "ValidTo",
]


def status_label(code: str) -> str:
    return STATUS_LABELS.get(code, code.replace("_", " ").capitalize() if code else "Not given")


def segment_label(code: str) -> str:
    return SEGMENT_LABELS.get(code, code.replace("_", " ").capitalize() if code else "")


def pa_code(program: Program) -> str:
    return "SW" if program.statewide or program.administrator is None else program.administrator.code


def _filtered(form: forms.CatalogFilter):
    """The catalog rows (program, version) the filter keeps, in PrgID order."""
    f = form.cleaned_data if form.is_valid() else {}
    q = (f.get("q") or "").strip().lower()
    pa, state, segment = f.get("pa") or "", f.get("state") or "", f.get("segment") or ""
    rows = []
    for p, v in registry.catalog(f.get("year")):
        if v is None:
            continue
        if q and q not in p.prg_id.lower() and q not in v.name.lower() and q not in v.commercial_name.lower():
            continue
        if pa and pa_code(p) != pa:
            continue
        if state == "open" and v.status == "closed" or state == "closed" and v.status != "closed":
            continue
        if segment and v.segment != segment:
            continue
        rows.append((p, v))
    return rows


@login_required
def catalog(request):
    """Every program, searchable by name or PrgID, closed programs included (BR-13.5); the version shown is the one for the chosen program
    year, or the one in force today."""
    form = forms.CatalogFilter(request.GET or None)
    rows = _filtered(form)
    page = Paginator(rows, PER_PAGE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "programs/catalog.html",
        {
            "form": form,
            "page": page,
            "rows": [(p, v, pa_code(p), status_label(v.status), segment_label(v.segment)) for p, v in page.object_list],
            "total": len(rows),
            "query": query.urlencode(),
        },
    )


@login_required
def catalog_csv(request):
    """The filtered catalog as a file, from the same filter (decision H), in CEDARS' Programs list column names."""
    rows = _filtered(forms.CatalogFilter(request.GET or None))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="programs.csv"'
    w = csv.writer(response, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for p, v in rows:
        sectors = ", ".join(f"{k}: {share:.6f}" for k, share in (v.sectors or {}).items())
        w.writerow(
            [
                pa_code(p),
                p.prg_id,
                v.name,
                v.status,
                v.segment,
                v.primary_sector,
                sectors,
                v.implementer,
                v.category,
                v.implementation_contractor,
                v.start_year or "",
                v.end_year or "",
                v.statewide_program,
                v.parent_program,
                v.valid_from.isoformat(),
                v.valid_to.isoformat() if v.valid_to else "",
            ]
        )
    return response


def _program_or_404(prg_id: str) -> Program:
    p = get_object_or_404(Program.objects.select_related("administrator"), prg_id=prg_id)
    if p.portfolio_cost:
        raise Http404  # portfolio-level cost IDs are not programs (BR-02.6)
    return p


def allocation_history(program: Program) -> list[dict]:
    """The program's statewide allocation factors in every version that has it, newest version first (BR-06.7, BR-13.8)."""
    out = []
    entries = (
        AllocationEntry.objects.filter(program_id=program.prg_id)
        .select_related("version", "version__filing_cycle")
        .prefetch_related("shares")
        .order_by("-version__confirmed_at", "-version__created_at", "year")
    )
    for e in entries:
        shares = {s.funding_pa: s for s in e.shares.all()}
        out.append({"version": e.version, "year": e.year, "percent_electric": e.percent_electric, "shares": shares})
    return out


@login_required
def program(request, prg_id):
    p = _program_or_404(prg_id)
    current = registry.current_version(p) or p.versions.order_by("-valid_from").first()
    history = list(reversed(registry.history(p)))
    allocation = allocation_history(p)
    pas = sorted({pa for row in allocation for pa in row["shares"]})
    return render(
        request,
        "programs/program.html",
        {
            "p": p,
            "pa": pa_code(p),
            "v": current,
            "status": status_label(current.status) if current else "",
            "segment": segment_label(current.segment) if current else "",
            "flags": sorted(k.replace("_Flag", "").replace("_", " ") for k, val in (current.flags or {}).items() if _truthy(val))
            if current
            else [],
            "history": history,
            "upcoming": [v for v, _ in history if current and v.valid_from > current.valid_from],
            "allocation": [(row, [row["shares"].get(pa) for pa in pas]) for row in allocation],
            "allocation_pas": pas,
            "can_edit": registry.can_edit(request.user, p),
            "events": audit.changes(p).select_related("actor")[:20],
        },
    )


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1", "y")


@login_required
def change(request, prg_id):
    """A new effective-dated version of the program (BR-13.3): what changes, from when, and why. Versions in force are never edited."""
    p = _program_or_404(prg_id)
    if not registry.can_edit(request.user, p):
        raise Http404
    latest = p.versions.order_by("-valid_from").first()
    form = forms.ProgramChangeForm(request.POST or None, latest=latest)
    if request.method == "POST" and form.is_valid():
        changes = form.changes()
        if not changes:
            form.add_error(None, "Nothing was changed.")
        else:
            try:
                registry.change_program(
                    request.user, p, effective=form.cleaned_data["effective"], reason=form.cleaned_data["reason"], **changes
                )
            except AccessDenied as e:
                form.add_error(None, str(e))
            else:
                names = ", ".join(form.fields[k].label.lower() for k in changes)
                messages.success(request, f"Saved: {names}, from {form.cleaned_data['effective']:%Y-%m-%d}.")
                return redirect("programs:program", prg_id=p.prg_id)
    return render(request, "programs/change.html", {"p": p, "latest": latest, "form": form})
