import csv
import mimetypes
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.core import letters, storage
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.legacy import service as legacy
from cedars.services.refdata.models import PackVersion
from cedars.services.registry.models import FilingCycle
from cedars.services.submissions import corrections
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, ReopenRequest, Submission, SubmissionVersion

from . import forms, jobfile, service, shifts
from .models import PROGRAM_COST_FIELDS, ChangeLetter

DEFAULT_ACCS = ["2021", "2022", "2024", "2026"]


def accs() -> list[str]:
    """The ACC years a filing can be priced on: those with a registered electric ACC pack, else the engine's four."""
    years = (
        PackVersion.objects.filter(kind=PackVersion.Kind.ACC_ELECTRIC, acc_year__isnull=False)
        .exclude(status=PackVersion.Status.WITHDRAWN)
        .values_list("acc_year", flat=True)
    )
    return sorted({str(y) for y in years}) or DEFAULT_ACCS


def _deadline(pk) -> Deadline:
    d = get_object_or_404(Deadline.objects.select_related("cycle", "budget_setup__allocation_version"), pk=pk, module=service.MODULE)
    if not hasattr(d, "budget_setup"):
        raise Http404
    return d


def _organisation(request, deadline: Deadline) -> Organisation | None:
    """The organization whose filing the page shows: ?org=CODE for those who may read it (CPUC staff), else the one the user acts for."""
    code = request.GET.get("org") or request.POST.get("org")
    if code:
        org = access.by_code_or_404(code)
        sub = Submission(deadline=deadline, organisation=org)
        if not service.can_read_submission(request.user, sub):
            raise Http404
        return org
    return request.organisation


def _money(x) -> str:
    if x is None:
        return ""
    d = Decimal(x)
    return f"-${-d:,.0f}" if d < 0 else f"${d:,.0f}"


def _diff(kind: str, x) -> str:
    """A difference, signed (b minus a)."""
    if x is None:
        return ""
    if kind == "ratio":
        return f"{x:+.2f}"
    text = _fmt(kind, abs(x))
    return text if round(x) == 0 else ("+" if x > 0 else "-") + text


def _fmt(kind: str, x) -> str:
    if x is None:
        return ""
    if kind == "money":
        return _money(x)
    if kind == "ratio":
        return f"{float(x):.2f}"
    return f"{float(x):,.0f}"


# --- home and set-up


@login_required
def index(request):
    """Budget filings by cycle: each filing's due date and the acting organization's status, and the budgets that stand for every PA."""
    org = request.organisation
    cycles = []
    for cycle in FilingCycle.objects.filter(deadlines__module=service.MODULE).distinct().order_by("-first_year"):
        rows = []
        for d in service.filings(cycle).order_by("due_at"):
            latest = None
            if org is not None:
                sub = Submission.objects.filter(deadline=d, organisation=org).first()
                latest = submissions.latest(sub) if sub else None
            rows.append({"deadline": d, "latest": latest, "past_due": submissions.past_due(d)})
        standing = []
        for o in Organisation.objects.filter(submissions__deadline__module=service.MODULE, submissions__deadline__cycle=cycle).distinct():
            v = service.standing(o, cycle)
            if v is not None:
                standing.append(v)
        imported = legacy.imported_deadlines(r["deadline"] for r in rows)
        for r in rows:
            r["legacy"] = r["deadline"].pk in imported
        cycles.append({"cycle": cycle, "filings": rows, "standing": standing})
    return render(
        request,
        "budgets/index.html",
        {"cycles": cycles, "org": org, "sysadmin": access.is_system_admin(request.user)},
    )


@login_required
def filing_new(request):
    if not access.is_system_admin(request.user):
        raise Http404
    form = forms.FilingForm(request.POST or None, accs=accs())
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            deadline = service.add_filing(
                request.user,
                first_year=d["first_year"],
                kind=d["kind"],
                due_at=service.due_at(d["due"]),
                opens_at=service.due_at(d["opens"]) if d["opens"] else None,
                acc_version=d["acc_version"],
                allocation_version=d["allocation_version"],
                cost_test_set=d["cost_test_set"],
                notes=d["notes"],
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"{service.label(deadline)} is set up.")
            return redirect("budgets:filing", deadline.pk)
    return render(request, "budgets/filing_new.html", {"form": form})


@login_required
def setup(request, pk):
    deadline = _deadline(pk)
    if not access.is_system_admin(request.user):
        raise Http404
    s = deadline.budget_setup
    form = forms.SetupForm(
        request.POST or None,
        accs=accs(),
        initial={"acc_version": s.acc_version, "allocation_version": s.allocation_version, "cost_test_set": s.cost_test_set},
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            service.change_setup(
                request.user,
                s,
                acc_version=d["acc_version"],
                allocation_version=d["allocation_version"],
                reason=d["reason"],
                cost_test_set=d["cost_test_set"],
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Saved. Versions already priced keep what they were priced with.")
            return redirect("budgets:filing", pk)
    return render(request, "budgets/setup.html", {"deadline": deadline, "form": form, "label": service.label(deadline)})


# --- a PA's filing


@login_required
def filing(request, pk):
    """One filing for one organization: its versions and the next step (upload, fix, price, confirm, un-confirm, ask to reopen)."""
    deadline = _deadline(pk)
    org = _organisation(request, deadline)
    sub = Submission.objects.filter(deadline=deadline, organisation=org).first() if org else None
    if sub is not None and not service.can_read_submission(request.user, sub):
        raise Http404
    v = submissions.latest(sub) if sub else None
    can = {a: org is not None and access.has_permission(request.user, org, f"budgets.{a}") for a in ("submit", "confirm")}
    acting = org is not None and org == request.organisation
    if v is not None:
        service.ensure_lines(v)
    ctx = {
        "deadline": deadline,
        "label": service.label(deadline),
        "setup": deadline.budget_setup,
        "org": org,
        "acting": acting,
        "sub": sub,
        "v": v,
        "versions": list(sub.versions.select_related("confirmed_by").order_by("-number")) if sub else [],
        "past_due": submissions.past_due(deadline),
        "opens_later": deadline.opens_at and timezone.now() < deadline.opens_at,
        "can_submit": can["submit"] and acting,
        "can_confirm": can["confirm"] and acting,
        "upload_form": forms.UploadForm(),
        "reason_form": forms.ReasonForm(),
        "pending_reopen": sub.reopen_requests.filter(status=ReopenRequest.Status.PENDING).first() if sub else None,
        "sysadmin": access.is_system_admin(request.user),
        "legacy": legacy.imported(v) if v is not None and v.imported else None,
    }
    if acting and (v is None or not v.locked):
        ctx["shifts_in_effect"] = shifts.operating_budget(org, deadline.cycle).shifts
    if v is not None:
        ctx["pricing_run"] = submissions.pricing_run(v)
        ctx["priced"] = service.priced(v)
        ctx["file"] = service.job_file(v)
        if ctx["priced"]:
            t = service.table(v)
            ctx["tiles"] = _tiles(t)
        ctx["can_correct"] = acting and ctx["can_submit"] and not v.locked and not corrections.refusal(request.user, v)
    return render(request, "budgets/filing.html", ctx)


def _tiles(t) -> list[tuple[str, str]]:
    return [
        ("Budget", _money(t.total.budget)),
        ("Total system benefit", _fmt("money", t.total.value("TotalSystemBenefit"))),
        ("TRC", _fmt("ratio", t.total.value("TRC"))),
        ("PAC", _fmt("ratio", t.total.value("PAC"))),
        ("Programs", f"{len(t.rows):,}"),
    ]


def _action(request, pk, fn, done: str):
    deadline = _deadline(pk)
    org = request.organisation
    if org is None:
        raise Http404
    try:
        fn(deadline, org)
    except (AccessDenied, jobfile.JobError) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, done)
    return redirect("budgets:filing", pk)


def _latest(deadline, org) -> SubmissionVersion:
    sub = Submission.objects.filter(deadline=deadline, organisation=org).first()
    v = submissions.latest(sub) if sub else None
    if v is None:
        raise AccessDenied("Upload the filing first.")
    return v


@login_required
@require_POST
def upload(request, pk):
    form = forms.UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose the filing's .zip or .xlsx file, or its files.")
        return redirect("budgets:filing", pk)
    files = [(f.name, f.read()) for f in form.cleaned_data["file"]]

    def do(deadline, org):
        c = jobfile.combine(files)
        v = service.upload(request.user, deadline, org, c.data, c.filename, sources=c.sources)
        s = v.findings or {}
        if v.status == SubmissionVersion.Status.VALIDATED:
            msg = f"Checked {s.get('records', 0):,} records: none rejected"
            msg += f", {s['warnings']:,} with warnings." if s.get("warnings") else "."
            messages.info(request, msg + " Price it next.")
        else:
            messages.warning(request, f"{s.get('rejected', 0):,} records are rejected. Fix them and upload the file again.")

    deadline = _deadline(pk)
    if request.organisation is None:
        raise Http404
    try:
        do(deadline, request.organisation)
    except (AccessDenied, jobfile.JobError) as e:
        messages.error(request, str(e))
    return redirect("budgets:filing", pk)


@login_required
@require_POST
def price(request, pk):
    return _action(
        request,
        pk,
        lambda d, o: service.price(request.user, _latest(d, o)),
        "Pricing has started. This page shows the results when the run finishes; you will also get a notice.",
    )


@login_required
@require_POST
def confirm(request, pk):
    return _action(
        request,
        pk,
        lambda d, o: service.confirm(request.user, _latest(d, o)),
        "Confirmed and published. Its inputs and results are public.",
    )


@login_required
@require_POST
def unconfirm(request, pk):
    form = forms.ReasonForm(request.POST)
    reason = form.data.get("reason", "")
    return _action(
        request,
        pk,
        lambda d, o: submissions.unconfirm(request.user, _latest(d, o), reason),
        "Un-confirmed: the version is back to validated and no longer published.",
    )


@login_required
@require_POST
def reopen(request, pk):
    reason = request.POST.get("reason", "")

    def do(deadline, org):
        sub = Submission.objects.get(deadline=deadline, organisation=org)
        out = submissions.request_reopen(request.user, sub, reason)
        if isinstance(out, SubmissionVersion):
            messages.info(request, f"Version {out.number} is open as a draft.")
        else:
            messages.info(request, "Your request is with a CPUC approver.")

    deadline = _deadline(pk)
    if request.organisation is None:
        raise Http404
    try:
        do(deadline, request.organisation)
    except (AccessDenied, Submission.DoesNotExist) as e:
        messages.error(request, str(e) or "Nothing to reopen.")
    return redirect("budgets:filing", pk)


# --- a version's results


def _version(request, pk) -> SubmissionVersion:
    v = get_object_or_404(
        SubmissionVersion.objects.select_related("submission__deadline__cycle", "submission__organisation"),
        pk=pk,
        submission__deadline__module=service.MODULE,
    )
    if not service.can_read(request.user, v):
        raise Http404
    return v


def _filters(request) -> dict:
    return {
        "years": request.GET.getlist("years"),
        "pa": request.GET.get("pa", ""),
        "hide_budget_excluded": request.GET.get("hide_budget") == "1",
        "hide_ce_excluded": request.GET.get("hide_ce") == "1",
    }


@login_required
def version(request, pk):
    """A version by program: its filed budget and priced results over the chosen years, excluded programs marked and optionally left
    out, the reconciliation of every line, and what it publishes."""
    v = _version(request, pk)
    f = _filters(request)
    t = service.table(v, **f)
    head = ["Program", "Budget"] + [m[1] for m in service.METRICS[1:]]
    rows = []
    for r in t.rows:
        flags = (["excluded from budget"] if r.excluded_budget else []) + (["excluded from C/E"] if r.excluded_ce else [])
        flags += ["portfolio cost"] if r.portfolio_cost else []
        rows.append({"r": r, "flags": flags, "cells": [_fmt(kind, r.value(key)) for key, _, kind, _ in service.METRICS]})
    total = [_fmt(kind, t.total.value(key)) for key, _, kind, _ in service.METRICS]
    query = request.GET.urlencode()
    return render(
        request,
        "budgets/version.html",
        {
            "v": v,
            "label": service.label(v.submission.deadline),
            "t": t,
            "head": head,
            "rows": rows,
            "total": total,
            "tiles": _tiles(t) if t.priced else [],
            "f": f,
            "years": [int(y) for y in f["years"] if y.isdigit()],
            "definitions": [(m[1], m[3]) for m in service.METRICS],
            "rec": {k: _money(val) for k, val in t.reconciliation.items()},
            "files": [(lbl, i) for i, (lbl, _, _) in enumerate(service.published_files(v))],
            "public": v.status == SubmissionVersion.Status.PUBLISHED,
            "csv_query": f"?{query}" if query else "",
            "calc": v.calculation or {},
            "cost_fields": PROGRAM_COST_FIELDS,
            "legacy": legacy.imported(v) if v.imported else None,
            "letter": ChangeLetter.objects.filter(version=v).first(),
        },
    )


@login_required
def version_csv(request, pk):
    """The table as shown, from the same filters (decision H), with the cost categories."""
    v = _version(request, pk)
    t = service.table(v, **_filters(request))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    name = f"{v.submission.organisation.code}_{v.submission.deadline.cycle}_v{v.number}".replace(" ", "_")
    response["Content-Disposition"] = f'attachment; filename="budget_{name}.csv"'
    w = csv.writer(response, lineterminator="\n")
    w.writerow(
        ["PrgID", "ProgramName", "ExcludedFromBudget", "ExcludedFromCE", "PortfolioCost"]
        + [k for k, _ in PROGRAM_COST_FIELDS]
        + ["RebatesandIncents", "OnBillFinancing"]
        + [key if kind != "ratio" else f"{key}Ratio" for key, _, kind, _ in service.METRICS]
    )
    for r in t.rows + [t.total]:
        w.writerow(
            [r.prg_id, r.name, r.excluded_budget, r.excluded_ce, r.portfolio_cost]
            + [r.by_field.get(k, 0) for k, _ in PROGRAM_COST_FIELDS]
            + [r.rebates, r.on_bill_financing]
            + ["" if r.value(key) is None else r.value(key) for key, *_ in service.METRICS]
        )
    return response


@login_required
def findings_csv(request, pk):
    v = _version(request, pk)
    if not service.can_read_submission(request.user, v.submission):
        raise Http404
    response = HttpResponse(service.findings_csv(v), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="budget_findings_v{v.number}.csv"'
    return response


@login_required
def appendix(request, pk):
    v = _version(request, pk)
    response = HttpResponse(service.appendix_xlsx(v), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    name = f"{v.submission.organisation.code}_{v.submission.deadline.cycle}_v{v.number}"
    response["Content-Disposition"] = f'attachment; filename="budget_appendix_{name}.xlsx"'
    return response


@login_required
def published_file(request, pk, which):
    v = _version(request, pk)
    files = service.published_files(v)
    if not which.isdigit() or int(which) >= len(files):
        raise Http404
    _, area, key = files[int(which)]
    store = storage.store(area)
    if not store.exists(key):
        raise Http404
    filename = f"v{v.number}_{key.rsplit('/', 1)[-1]}"
    url = store.download_url(key, filename)
    if url:
        return HttpResponseRedirect(url)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(store.open(key), as_attachment=True, filename=filename, content_type=content_type)


@login_required
def compare(request):
    """Any two versions the user can read, by program (BR-04.2)."""
    choices = list(service.readable_versions(request.user)[:200])
    by_pk = {str(v.pk): v for v in choices}
    a, b = by_pk.get(request.GET.get("a", "")), by_pk.get(request.GET.get("b", ""))
    years = request.GET.getlist("years")
    changed_only = request.GET.get("changed") == "1"
    rows = []
    if a and b:
        for row in service.compare(a, b, years=years, changed_only=changed_only):
            cells = []
            for (_, _, kind, _), (va, vb, diff) in zip(service.METRICS, row["cells"], strict=True):
                cells.append((_fmt(kind, va), _fmt(kind, vb), _diff(kind, diff)))
            rows.append({**row, "cells": cells})
    all_years = sorted({ln.year for v in (a, b) if v for ln in v.budget_lines.all()})
    return render(
        request,
        "budgets/compare.html",
        {
            "choices": [(v, _version_label(v)) for v in choices],
            "a": a,
            "b": b,
            "a_label": _version_label(a) if a else "",
            "b_label": _version_label(b) if b else "",
            "rows": rows,
            "metrics": [m[1] for m in service.METRICS],
            "all_years": all_years,
            "years": [int(y) for y in years if y.isdigit()],
            "changed_only": changed_only,
        },
    )


def _version_label(v: SubmissionVersion) -> str:
    return f"{v.submission.organisation.code} {service.label(v.submission.deadline)}, version {v.number} ({v.get_status_display().lower()})"


# --- change letters (BR-09.3)


def changes(request):
    """Every published budget correction's change letter, newest first, by PA and cycle. Public: the record of corrections (BR-09.3)."""
    every = service.change_letters()
    out = every
    org = access.by_code(request.GET.get("org", ""), follow=False)
    cycle = request.GET.get("cycle", "")
    if org is not None:
        out = out.filter(version__submission__organisation=org)
    if cycle.isdigit():
        out = out.filter(version__submission__deadline__cycle__first_year=int(cycle))
    return render(
        request,
        "budgets/changes.html",
        {
            "letters": out[:500],
            "orgs": sorted({c.version.submission.organisation.code for c in every}),
            "cycles": sorted({c.version.submission.deadline.cycle for c in every}, key=lambda x: -x.first_year),
            "org": org.code if org else "",
            "cycle": cycle,
        },
    )


def change_letter(request, pk):
    """One correction's change letter: when it was published, why, and each program whose budget or results changed. Public."""
    letter = get_object_or_404(service.change_letters(), pk=pk)
    v = letter.version
    c = letter.changes or {}
    return render(
        request,
        "budgets/change_letter.html",
        {
            "letter": letter,
            "v": v,
            "label": service.label(v.submission.deadline),
            "c": c,
            "priced": c.get("priced", {}),
            "caption": "Each changed program's budget and results before and after the correction, and the difference",
            "empty": "No program's budget or results changed.",
            **letters.program_rows(c, service.LETTER_FIGURES),
        },
    )
