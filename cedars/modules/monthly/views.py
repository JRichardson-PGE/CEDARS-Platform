import csv
import mimetypes
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.core import storage
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.refdata.models import PackVersion
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, ReopenRequest, Submission, SubmissionVersion

from . import forms, service
from . import upload as up
from .models import COMMITMENT_FIELDS, INCENTIVE_FIELDS, PROGRAM_COST_FIELDS, SPEND_FIELDS, YearSetup

DEFAULT_ACCS = ["2021", "2022", "2024", "2026"]
UNVALIDATED = "Unvalidated estimate"


def accs() -> list[str]:
    """The ACC years estimates can be priced on: those with a registered electric ACC pack, else the engine's four."""
    years = (
        PackVersion.objects.filter(kind=PackVersion.Kind.ACC_ELECTRIC, acc_year__isnull=False)
        .exclude(status=PackVersion.Status.WITHDRAWN)
        .values_list("acc_year", flat=True)
    )
    return sorted({str(y) for y in years}) or DEFAULT_ACCS


def _money(x) -> str:
    if x is None:
        return ""
    d = Decimal(x)
    return f"-${-d:,.0f}" if d < 0 else f"${d:,.0f}"


def _fmt(kind: str, x) -> str:
    if x is None:
        return ""
    if kind == "money":
        return _money(x)
    if kind == "ratio":
        return f"{float(x):.2f}"
    if kind == "share":
        return f"{float(x):.0%}"
    return f"{float(x):,.0f}"


def _deadline(pk) -> Deadline:
    d = get_object_or_404(Deadline.objects.select_related("period"), pk=pk, module=service.MODULE)
    if service.setup_for(d) is None:
        raise Http404
    return d


def _organisation(request, deadline: Deadline) -> Organisation | None:
    """Whose report the page shows: ?org=CODE for those who may read it (CPUC staff), else the organisation the user acts for."""
    code = request.GET.get("org") or request.POST.get("org")
    if code:
        org = get_object_or_404(Organisation, code=code)
        if not service.can_read_submission(request.user, Submission(deadline=deadline, organisation=org)):
            raise Http404
        return org
    return request.organisation


def _pricing_run(v: SubmissionVersion) -> str | None:
    """The run started for the version since its files last changed, while it has not finished."""
    if v.calculation:
        return None
    events = AuditEvent.objects.filter(object_type="submissions.submissionversion", object_id=str(v.pk))
    last = events.filter(action__in=["submission.pricing_started", "submission.files_attached"]).order_by("-id").first()
    return (last.after or {}).get("run") if last and last.action == "submission.pricing_started" else None


def _status(v: SubmissionVersion | None) -> str:
    if v is None:
        return "Not started"
    return f"Version {v.number}: {v.get_status_display().lower()}{' (late)' if v.late else ''}"


# --- home and set-up


@login_required
def index(request):
    """Each program year's twelve reports with their due dates and the acting organisation's status."""
    org = request.organisation
    years = []
    for setup in service.years():
        rows = []
        for d in service.months(setup.year):
            latest = None
            if org is not None:
                sub = Submission.objects.filter(deadline=d, organisation=org).first()
                latest = submissions.latest(sub) if sub else None
            rows.append({"deadline": d, "latest": latest, "status": _status(latest), "past_due": submissions.past_due(d)})
        years.append({"setup": setup, "months": rows})
    return render(request, "monthly/index.html", {"years": years, "org": org, "sysadmin": access.is_system_admin(request.user)})


@login_required
def year_new(request):
    if not access.is_system_admin(request.user):
        raise Http404
    form = forms.YearForm(request.POST or None, accs=accs())
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            setup = service.set_up_year(
                request.user,
                year=d["year"],
                acc_version=d["acc_version"],
                due_day=d["due_day"],
                allocation_version=d["allocation_version"],
                notes=d["notes"],
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"{setup.year}'s twelve monthly reports are set up.")
            return redirect("monthly:index")
    return render(request, "monthly/year_new.html", {"form": form})


@login_required
def setup(request, year):
    s = get_object_or_404(YearSetup, year=year)
    if not access.is_system_admin(request.user):
        raise Http404
    form = forms.SetupForm(
        request.POST or None, accs=accs(), initial={"acc_version": s.acc_version, "allocation_version": s.allocation_version}
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            service.change_setup(
                request.user, s, acc_version=d["acc_version"], allocation_version=d["allocation_version"], reason=d["reason"]
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Saved. Reports already priced keep what they were priced with.")
            return redirect("monthly:index")
    return render(request, "monthly/setup.html", {"setup": s, "form": form})


# --- a PA's report for a month


@login_required
def report(request, pk):
    """One month's report for one organisation: its versions and the next step (upload, fix, price, confirm, un-confirm, ask to
    reopen)."""
    deadline = _deadline(pk)
    org = _organisation(request, deadline)
    sub = Submission.objects.filter(deadline=deadline, organisation=org).first() if org else None
    if sub is not None and not service.can_read_submission(request.user, sub):
        raise Http404
    v = submissions.latest(sub) if sub else None
    can = {a: org is not None and access.has_permission(request.user, org, f"{service.MODULE}.{a}") for a in ("submit", "confirm")}
    acting = org is not None and org == request.organisation
    ctx = {
        "deadline": deadline,
        "label": service.label(deadline),
        "setup": service.setup_for(deadline),
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
        "pending_reopen": sub.reopen_requests.filter(status=ReopenRequest.Status.PENDING).first() if sub else None,
        "sysadmin": access.is_system_admin(request.user),
        "commitment_fields": COMMITMENT_FIELDS,
        "cost_fields": PROGRAM_COST_FIELDS,
        "incentive_fields": INCENTIVE_FIELDS,
    }
    if v is not None:
        service.ensure_lines(v)
        ctx["pricing_run"] = _pricing_run(v)
        ctx["priced"] = service.priced(v)
        ctx["file"] = service.report_file(v)
        if ctx["priced"]:
            ctx["tiles"] = _tiles(service.report(v))
    return render(request, "monthly/report.html", ctx)


def _tiles(r) -> list[tuple[str, str]]:
    t = r.total
    out = [
        ("Spend this month", _money(t.get("MonthExpenditures")) if r.month_known else "not known yet"),
        ("Spend, year to date", _money(t.get("YTDExpenditures"))),
    ]
    if r.budget:
        out.append(("Share of the operating budget spent", _fmt("share", t.spent_share)))
    out += [
        ("Rebates and incentives, year to date", _money(t.get("RebatesAndIncentives"))),
        ("Funds committed, year to date", _money(t.get("YTDCommittedFunds"))),
    ]
    if r.estimated:
        out.append(("Estimated TSB, year to date (unvalidated)", _money(t.estimate("TotalSystemBenefit"))))
    return out


def _latest(deadline, org) -> SubmissionVersion:
    sub = Submission.objects.filter(deadline=deadline, organisation=org).first()
    v = submissions.latest(sub) if sub else None
    if v is None:
        raise AccessDenied("Upload the report first.")
    return v


def _action(request, pk, fn, done: str):
    deadline = _deadline(pk)
    if request.organisation is None:
        raise Http404
    try:
        out = fn(deadline, request.organisation)
    except (AccessDenied, up.UploadError, Submission.DoesNotExist) as e:
        messages.error(request, str(e) or "Nothing to do.")
    else:
        messages.success(request, done if not callable(done) else done(out))
    return redirect("monthly:report", pk)


@login_required
@require_POST
def upload(request, pk):
    form = forms.UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose the report's .zip or .xlsx file.")
        return redirect("monthly:report", pk)
    f = form.cleaned_data["file"]

    def done(v):
        s = v.findings or {}
        if v.status == SubmissionVersion.Status.VALIDATED:
            msg = f"Checked {s.get('records', 0):,} records: none rejected"
            return msg + (f", {s['warnings']:,} with warnings." if s.get("warnings") else ".") + " Price it next."
        return f"{s.get('rejected', 0):,} records are rejected. Fix them and upload the file again."

    return _action(request, pk, lambda d, o: service.upload(request.user, d, o, f.read(), f.name), done)


@login_required
@require_POST
def price(request, pk):
    return _action(
        request,
        pk,
        lambda d, o: service.price(request.user, _latest(d, o)),
        lambda run: (
            "Nothing to estimate: the Measure table has no rows. You can confirm the report."
            if run == "none"
            else "Pricing has started. This page shows the estimates when the run finishes; you will also get a notice."
        ),
    )


@login_required
@require_POST
def confirm(request, pk):
    return _action(
        request, pk, lambda d, o: service.confirm(request.user, _latest(d, o)), "Confirmed and published. The report is open to every user."
    )


@login_required
@require_POST
def unconfirm(request, pk):
    reason = request.POST.get("reason", "")
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
        return submissions.request_reopen(request.user, Submission.objects.get(deadline=deadline, organisation=org), reason)

    return _action(
        request,
        pk,
        do,
        lambda out: (
            f"Version {out.number} is open as a draft." if isinstance(out, SubmissionVersion) else "Your request is with a CPUC approver."
        ),
    )


# --- a version by program


def _version(request, pk) -> SubmissionVersion:
    v = get_object_or_404(
        SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation"),
        pk=pk,
        submission__deadline__module=service.MODULE,
    )
    if not service.can_read(request.user, v):
        raise Http404
    return v


COLUMNS = [
    ("operating", "Operating budget", "money"),
    ("MonthExpenditures", "Spend this month", "money"),
    ("YTDExpenditures", "Spend, year to date", "money"),
    ("spent_share", "Share of budget spent", "share"),
    ("RebatesAndIncentives", "Rebates and incentives, year to date", "money"),
    ("YTDCommittedFunds", "Funds committed, year to date", "money"),
]


def _cell(r, key):
    if key == "operating":
        return r.operating
    if key == "spent_share":
        return r.spent_share
    return r.get(key)


@login_required
def version(request, pk):
    """A report by program: spend and commitments against the operating budget, and the estimates (labelled unvalidated)."""
    v = _version(request, pk)
    r = service.report(v)
    rows = []
    for row in r.rows + [r.total]:
        rows.append(
            {
                "r": row,
                "cells": [_fmt(kind, _cell(row, key)) for key, _, kind in COLUMNS],
                "estimates": [_fmt(kind, row.estimate(key)) for key, _, kind in service.ESTIMATES],
                "flags": [] if row is r.total else (["not in the BCA inputs"] if not row.reported else []),
            }
        )
    return render(
        request,
        "monthly/version.html",
        {
            "v": v,
            "label": service.label(v.submission.deadline),
            "report": r,
            "rows": rows,
            "columns": [c[1] for c in COLUMNS],
            "estimates": [e[1] for e in service.ESTIMATES],
            "tiles": _tiles(r),
            "files": [(lbl, i) for i, (lbl, _, _) in enumerate(service.published_files(v))],
            "public": v.status == SubmissionVersion.Status.PUBLISHED,
            "calc": v.calculation or {},
            "commitment_fields": COMMITMENT_FIELDS,
            "cost_fields": PROGRAM_COST_FIELDS,
            "unvalidated": UNVALIDATED,
        },
    )


@login_required
def version_csv(request, pk):
    """The report by program as a file: every spend and commitment field, the budgets and the estimates."""
    v = _version(request, pk)
    r = service.report(v)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    name = f"{v.submission.organisation.code}_{v.submission.deadline.period}_v{v.number}"
    response["Content-Disposition"] = f'attachment; filename="monthly_report_{name}.csv"'
    w = csv.writer(response, lineterminator="\n")
    fields = ["MonthExpenditures"] + SPEND_FIELDS + [f for f, _ in COMMITMENT_FIELDS]
    w.writerow(
        ["PrgID", "ProgramName", "InBCAInputs", "ApprovedBudget", "OperatingBudget"]
        + fields
        + [f"Estimated{k}" if kind != "ratio" else f"Estimated{k}Ratio" for k, _, kind in service.ESTIMATES]
    )
    for row in r.rows + [r.total]:
        w.writerow(
            [
                row.prg_id,
                row.name,
                row.reported,
                "" if row.approved is None else row.approved,
                "" if row.operating is None else row.operating,
            ]
            + ["" if row.get(f) is None else row.get(f) for f in fields]
            + ["" if row.estimate(k) is None else row.estimate(k) for k, *_ in service.ESTIMATES]
        )
    return response


@login_required
def findings_csv(request, pk):
    v = _version(request, pk)
    if not service.can_read_submission(request.user, v.submission):
        raise Http404
    response = HttpResponse(service.findings_csv(v), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="monthly_findings_v{v.number}.csv"'
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


# --- the year in one view (BR-14.1)


def _year_org(request) -> tuple[Organisation | None, bool, list[Organisation]]:
    """The PA whose year is shown (?org=CODE, else the acting organisation), whether only its published reports may be shown, and the
    PAs with monthly reports to choose from."""
    choices = list(Organisation.objects.filter(submissions__deadline__module=service.MODULE).distinct().order_by("code"))
    code = request.GET.get("org")
    org = get_object_or_404(Organisation, code=code) if code else request.organisation
    if org is None:
        return None, True, choices
    readable = service._org_reader(request.user, org) or service._cpuc_reader(request.user)
    return org, not readable, choices


@login_required
def year(request, year):
    """A PA's year of monthly reports in one place, with the accuracy check of the year-to-date spend (BR-14.1)."""
    get_object_or_404(YearSetup, year=year)
    org, published_only, choices = _year_org(request)
    months = service.year_view(org, year, published_only=published_only) if org else []
    latest = next((m for m in reversed(months) if m.version is not None), None)
    rows = []
    for m in months:
        t = m.totals
        rows.append(
            {
                "m": m,
                "status": _status(m.version),
                "cells": [_money(m.spend)]
                + [_money(t.get(k)) if t else "" for k in ("YTDExpenditures", "RebatesAndIncentives", "YTDCommittedFunds")]
                + [_money(t.estimate("TotalSystemBenefit")) if t else ""],
            }
        )
    return render(
        request,
        "monthly/year.html",
        {
            "year": year,
            "org": org,
            "choices": choices,
            "published_only": published_only,
            "rows": rows,
            "latest": latest,
            "inconsistent": [m for m in months if not m.consistent],
            "csv_query": f"?org={org.code}" if org else "",
        },
    )


@login_required
def year_csv(request, year):
    get_object_or_404(YearSetup, year=year)
    org, published_only, _ = _year_org(request)
    if org is None:
        raise Http404
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="monthly_reports_{org.code}_{year}.csv"'
    w = csv.writer(response, lineterminator="\n")
    fields = SPEND_FIELDS + [f for f, _ in COMMITMENT_FIELDS]
    w.writerow(["YearMonth", "Version", "Status", "MonthExpenditures", *fields, "YTDFell", "EstimatedTotalSystemBenefit"])
    for m in service.year_view(org, year, published_only=published_only):
        t = m.totals
        w.writerow(
            [
                f"{year}{m.deadline.period.number:02d}",
                m.version.number if m.version else "",
                m.version.status if m.version else "not started",
                "" if m.spend is None else m.spend,
            ]
            + ["" if t is None or t.get(f) is None else t.get(f) for f in fields]
            + [not m.consistent]
            + ["" if t is None or t.estimate("TotalSystemBenefit") is None else t.estimate("TotalSystemBenefit")]
        )
    return response
