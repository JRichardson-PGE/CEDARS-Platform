import csv
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.notifications import service as notifications
from cedars.services.registry import field_guide
from cedars.services.registry import service as registry
from cedars.services.registry.models import AllocationEntry, FilingCycle, Program, ProgramChangeRequest, ProgramVersion

from . import filters, forms

PER_PAGE = 50
STATUS_LABELS = filters.STATUS_LABELS
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


def _filtered(form: forms.CatalogFilter, query) -> tuple[list, list, dict, list[str]]:
    """The catalog rows (program, version) the filter keeps, in PrgID order; the rows the main filters alone keep (the Other filters
    panel offers their values); the other filters applied; and a note for each value that could not be read."""
    f = form.cleaned_data if form.is_valid() else {}
    q = (f.get("q") or "").strip().lower()
    state, segment = f.get("state") or "", f.get("segment") or ""
    pas = access.picked_pa_codes(query.getlist("pa"), query.getlist("pa_type"))
    mapping = access.code_map() if pas is not None else {}
    other, notes = filters.parse(query)
    base = []
    for p, v in registry.catalog(f.get("year")):
        if v is None:
            continue
        if q and q not in p.prg_id.lower() and q not in v.name.lower() and q not in v.commercial_name.lower():
            continue
        if pas is not None and access.current_code(pa_code(p), mapping).upper() not in pas:
            continue
        if state == "open" and v.status == "closed" or state == "closed" and v.status != "closed":
            continue
        if segment and v.segment != segment:
            continue
        base.append((p, v))
    rows = [(p, v) for p, v in base if filters.matches(other, p, v)] if other else base
    return rows, base, other, notes


PA_TYPE_WORDS = {code: label for code, (label, _) in access.PA_GROUP_LABELS.items()}


def _main_chips(form: forms.CatalogFilter, query) -> list[tuple]:
    """(parameter, label, value shown[, value]) for each main filter applied: the PA picker's choices one chip each (the value, when
    given, is the one its Remove link takes away), then the form's fields."""
    out = [("pa_type", "PA", f"All {PA_TYPE_WORDS.get(t.upper(), t)}", t) for t in query.getlist("pa_type") if t.strip()]
    out += [("pa", "PA", "Statewide programs" if c.upper() == "SW" else c, c) for c in query.getlist("pa") if c.strip()]
    if not form.is_valid():
        return out
    for name, field in form.fields.items():
        value = form.cleaned_data.get(name)
        if value in (None, ""):
            continue
        shown = dict(field.choices).get(value, value) if hasattr(field, "choices") else value
        out.append((name, field.label, str(shown)))
    return out


def catalog(request):
    """Every program, searchable by name or PrgID, closed programs included (BR-13.5); the version shown is the one for the chosen program
    year, or the one in force today. Any other program attribute can be filtered on too (filters.py), and every filter is in the link.
    Public (BR-01.5, BR-07.7): the registry holds published program definitions only."""
    form = forms.CatalogFilter(request.GET or None)
    rows, base, other, notes = _filtered(form, request.GET)
    page = Paginator(rows, PER_PAGE).get_page(request.GET.get("page"))
    query = filters.clean_query(request.GET)
    return render(
        request,
        "programs/catalog.html",
        {
            "form": form,
            "page": page,
            "rows": [(p, v, pa_code(p), status_label(v.status), segment_label(v.segment)) for p, v in page.object_list],
            "total": len(rows),
            "query": query.urlencode(),
            "applied": filters.chips(request.GET, _main_chips(form, request.GET), other),
            "pa_picker": access.pa_picker(
                request.GET.getlist("pa"), request.GET.getlist("pa_type"), statewide=Program.objects.filter(statewide=True).exists()
            ),
            "other": filters.panel(request.GET, other, base),
            "notes": notes,
        },
    )


def catalog_csv(request):
    """The filtered catalog as a file, from the same filter (decision H), in CEDARS' Programs list column names."""
    rows = _filtered(forms.CatalogFilter(request.GET or None), request.GET)[0]
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


def fields(request):
    """The program fields guide (BR-13.7): every field of the program record with its definition (the data dictionary's where it has
    the field), how to fill it in, and its valid values, retired ones marked and each with the number of programs on record that carry
    it. Public, like the catalog."""
    return render(
        request,
        "programs/fields.html",
        {"fields": field_guide.guide(), "catalog": _catalog_label(), "today": timezone.localdate()},
    )


def _catalog_label() -> str:
    from cedars.services.dictionary import service as dictionary

    version = dictionary.current()
    return version.label if version is not None else ""


def field_links() -> dict[str, str]:
    """{field: its entry in the program fields guide}, for the labels on the program page."""
    from django.urls import reverse

    url = reverse("programs:fields")
    return {e.name: f"{url}#{e.anchor}" for e in field_guide.entries()}


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


def _year_choices(p: Program, chosen: int | None, evaluations: list[dict]) -> list[int]:
    """The program years the page offers, newest first: the years its versions span (an open-ended program through the last filing
    cycle on record, or four years ahead) and the years its evaluations cover."""
    today = timezone.localdate().year
    versions = list(p.versions.all())
    starts = [v.start_year for v in versions if v.start_year] + [e["first_year"] for e in evaluations if e.get("first_year")]
    first = min(starts) if starts else today - 10
    if not versions or any(v.end_year is None for v in versions):
        last_cycle = FilingCycle.objects.order_by("-last_year").values_list("last_year", flat=True).first()
        last = max(today + 4, last_cycle or 0)
    else:
        last = max(v.end_year for v in versions)
    last = max([last] + [e["last_year"] for e in evaluations if e.get("last_year")])
    years = set(range(max(first, last - 40), last + 1)) | ({chosen} if chosen else set())
    return sorted(years, reverse=True)


def _chosen_year(request) -> int | None:
    value = request.GET.get("year", "").strip()
    return int(value) if value.isdigit() and 1990 <= int(value) <= 2100 else None


def _covers(e: dict, year: int) -> bool:
    first, last = e.get("first_year") or e.get("last_year"), e.get("last_year") or e.get("first_year")
    return first is None or first <= year <= last


def _money(x) -> str:
    if x is None:
        return ""
    d = Decimal(str(x))
    text = f"${abs(d):,.0f}"
    return "-" + text if d < 0 else text


def _number(x) -> str:
    return "" if x is None else f"{x:,.0f}"


def _ratio(x) -> str:
    return "" if x is None else f"{x:.2f}"


def _budget_groups(rows: list[dict]) -> list[dict]:
    """The budget rows by filing cycle, newest first, each with its program years as columns."""
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(str(r["cycle"]), {"cycle": str(r["cycle"]), "rows": [], "years": set()})
        g["rows"].append(r)
        g["years"] |= set(r["years"])
    out = []
    for g in groups.values():
        years = sorted(g["years"])
        for r in g["rows"]:
            r["cells"] = [_money(r["years"].get(y)) for y in years]
            r["shown"] = {"total": _money(r["total"]), "tsb": _money(r["tsb"]), "trc": _ratio(r["trc"])}
        out.append({"cycle": g["cycle"], "years": years, "rows": g["rows"]})
    return out


def _shown(records: dict) -> dict:
    """Each section's figures as text, so the page needs no other module's template filters."""
    for r in records.get("fund_shifts") or []:
        r["shown"] = {"net": _money(r["net"]), "lines": [(ln["pa"], ln["year"], _money(ln["amount"])) for ln in r["lines"]]}
    for r in records.get("claims") or []:
        r["shown"] = {
            "expenditures": _money(r["expenditures"]),
            "net_kwh": _number(r["net_kwh"]),
            "net_therm": _number(r["net_therm"]),
            "tsb": _money(r["tsb"]),
        }
    for r in records.get("monthly") or []:
        r["shown"] = {"ytd": _money(r["ytd"]), "paid": _money(r.get("paid")), "committed": _money(r["committed"])}
    return records


def program(request, prg_id):
    """A program's page (BR-07.6): its definition, versions and allocation, and every record about it that the reader may see, from the
    modules through the registry service: budget filing lines by version, fund shifts, claims by confirmation, monthly reports,
    evaluations and documents, all narrowed to one program year when one is chosen. Public (BR-01.5): signed out, it leaves out who
    entered each version, any pending change request, fund shifts, and filings not yet published."""
    p = _program_or_404(prg_id)
    signed_in = request.user.is_authenticated
    year = _chosen_year(request)
    records = registry.program_records(p, request.user, year)
    all_evaluations = registry.evaluations(p)
    evaluations = [e for e in all_evaluations if _covers(e, year)] if year else all_evaluations
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
            "signed_in": signed_in,
            "can_edit": signed_in and registry.can_edit(request.user, p),
            "pending": [
                r for r in p.change_requests.filter(status=ProgramChangeRequest.Status.PENDING) if registry.can_see_request(request.user, r)
            ]
            if signed_in
            else [],
            "evaluations": evaluations,
            "year": year,
            "years": _year_choices(p, year, all_evaluations),
            "records": _shown(records),
            "budget_groups": _budget_groups(records["budgets"] or []),
            "query_year": f"?year={year}" if year else "",
            "following": notifications.follows_program(request.user, p),
            "about": field_links(),
            "placeholders": [r for r in records.get("documents") or [] if r.get("placeholder")],
        },
    )


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1", "y")


@login_required
def change(request, prg_id):
    """A new effective-dated version of the program (BR-13.3): what changes, from when, and why. Versions in force are never edited.
    A change that touches a locked attribute is sent as a request for a CPUC approver's decision (BR-13.7)."""
    p = _program_or_404(prg_id)
    if not registry.can_edit(request.user, p):
        raise Http404
    latest = p.versions.order_by("-valid_from").first()
    form = forms.ProgramChangeForm(request.POST or None, latest=latest)
    locked = [f for f in registry.locked_fields() if f in form.fields and registry.needs_approval(request.user, {f: None})]
    for f in locked:
        form.fields[f].help_text = " ".join(x for x in (str(form.fields[f].help_text), "Locked: a change needs a CPUC approver.") if x)
    if request.method == "POST" and form.is_valid():
        changes = form.changes()
        held = registry.needs_approval(request.user, changes)
        names = ", ".join(form.fields[k].label.lower() for k in changes)
        try:
            if not changes:
                raise AccessDenied("Nothing was changed.")
            if held:
                req = registry.request_change(
                    request.user, p, effective=form.cleaned_data["effective"], reason=form.cleaned_data["reason"], **changes
                )
            else:
                registry.change_program(
                    request.user, p, effective=form.cleaned_data["effective"], reason=form.cleaned_data["reason"], **changes
                )
        except AccessDenied as e:
            form.add_error(None, str(e))
        else:
            if held:
                messages.success(request, f"Sent for approval: {names}, from {form.cleaned_data['effective']:%Y-%m-%d}.")
                return redirect("programs:request", pk=req.pk)
            messages.success(request, f"Saved: {names}, from {form.cleaned_data['effective']:%Y-%m-%d}.")
            return redirect("programs:program", prg_id=p.prg_id)
    return render(
        request,
        "programs/change.html",
        {"p": p, "latest": latest, "form": form, "locked": [form.fields[f].label for f in locked], "about": field_links()},
    )


# --- change requests and the locked attributes (BR-13.7)


def field_label(name: str) -> str:
    f = forms.ProgramChangeForm.base_fields.get(name)
    return str(f.label) if f is not None else str(ProgramVersion._meta.get_field(name).verbose_name).capitalize()


def show_value(name: str, value) -> str:
    if value is None or value == "" or value == {}:
        return "(blank)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if name == "status":
        return status_label(value)
    if name == "segment":
        return segment_label(value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v:g}" if isinstance(v, (int, float)) else f"{k}: {v}" for k, v in value.items())
    return str(value)


def _rows(req: ProgramChangeRequest) -> list[tuple[str, str, str]]:
    return [(field_label(k), show_value(k, req.before.get(k)), show_value(k, v)) for k, v in req.changes.items()]


@login_required
def requests(request):
    """Change requests to locked program attributes: every request for CPUC approvers, the organization's own for its members."""
    shown = request.GET.get("status", ProgramChangeRequest.Status.PENDING)
    if shown not in ProgramChangeRequest.Status.values and shown != "all":
        shown = ProgramChangeRequest.Status.PENDING
    qs = registry.visible_requests(request.user)
    if shown != "all":
        qs = qs.filter(status=shown)
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    return render(
        request,
        "programs/requests.html",
        {
            "page": page,
            "rows": [(r, ", ".join(field_label(k) for k in r.changes)) for r in page.object_list],
            "shown": shown,
            "statuses": [*ProgramChangeRequest.Status.choices, ("all", "All")],
            "approver": registry.approver(request.user) is not None,
        },
    )


@login_required
def change_request(request, pk):
    req = get_object_or_404(ProgramChangeRequest.objects.select_related("program", "requested_by", "decided_by", "version"), pk=pk)
    if not registry.can_see_request(request.user, req):
        raise Http404
    pending = req.status == ProgramChangeRequest.Status.PENDING
    can_decide = pending and registry.approver(request.user) is not None and req.requested_by_id != request.user.pk
    can_withdraw = pending and (req.requested_by_id == request.user.pk or registry.can_edit(request.user, req.program))
    decision, withdraw = forms.DecisionForm(), forms.WithdrawForm()
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in ("approve", "decline") and can_decide:
                decision = forms.DecisionForm(request.POST)
                if decision.is_valid():
                    registry.decide_change(request.user, req, action == "approve", decision.cleaned_data["note"])
                    messages.success(
                        request, "Approved: the new version is in the program's versions." if action == "approve" else "Declined."
                    )
                    return redirect("programs:request", pk=req.pk)
            elif action == "withdraw" and can_withdraw:
                withdraw = forms.WithdrawForm(request.POST)
                if withdraw.is_valid():
                    registry.withdraw_change(request.user, req, withdraw.cleaned_data["reason"])
                    messages.success(request, "Withdrawn.")
                    return redirect("programs:request", pk=req.pk)
            else:
                raise Http404
        except AccessDenied as e:
            (decision if action != "withdraw" else withdraw).add_error(None, str(e))
    return render(
        request,
        "programs/request.html",
        {
            "r": req,
            "rows": _rows(req),
            "can_decide": can_decide,
            "can_withdraw": can_withdraw,
            "decision": decision,
            "withdraw": withdraw,
            "events": audit.changes(req).select_related("actor"),
        },
    )


@login_required
def locked(request):
    """Which attributes are locked now, the policies before and after, and (for CPUC system administrators) a new policy."""
    can_set = access.is_system_admin(request.user)
    current = registry.locked_fields()
    choices = [(f, field_label(f)) for f in registry.VERSION_FIELDS]
    form = forms.LockedFieldsForm(
        request.POST or None, choices=choices, initial={"fields_locked": current, "effective": timezone.localdate()}
    )
    if request.method == "POST":
        if not can_set:
            raise Http404
        if form.is_valid():
            try:
                registry.set_locked_fields(
                    request.user,
                    form.cleaned_data["fields_locked"],
                    effective=form.cleaned_data["effective"],
                    reason=form.cleaned_data["reason"],
                )
            except AccessDenied as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, f"Saved: the locked attributes from {form.cleaned_data['effective']:%Y-%m-%d}.")
                return redirect("programs:locked")
    policies = [(pol, [field_label(f) for f in registry.VERSION_FIELDS if f in pol.locked_fields]) for pol in registry.policies()]
    return render(
        request,
        "programs/locked.html",
        {
            "current": [field_label(f) for f in current],
            "policies": policies,
            "form": form,
            "can_set": can_set,
            "today": timezone.localdate(),
        },
    )
