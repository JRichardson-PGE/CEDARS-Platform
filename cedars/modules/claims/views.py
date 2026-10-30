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

from . import forms, service, spec
from .models import PROGRAM_COST_FIELDS, ChangeLetter, ClaimYear
from .upload import UploadError

DEFAULT_ACCS = ["2021", "2022", "2024", "2026"]


def accs() -> list[str]:
    """The ACC years claims can be priced on: those with a registered electric ACC pack, else the engine's four."""
    years = (
        PackVersion.objects.filter(kind=PackVersion.Kind.ACC_ELECTRIC, acc_year__isnull=False)
        .exclude(status=PackVersion.Status.WITHDRAWN)
        .values_list("acc_year", flat=True)
    )
    return sorted({str(y) for y in years}) or DEFAULT_ACCS


def _deadline(pk) -> Deadline:
    d = get_object_or_404(
        Deadline.objects.select_related("period"), pk=pk, module=service.MODULE, kind__in=service.KINDS, period__isnull=False
    )
    if not ClaimYear.objects.filter(year=d.period.year).exists():
        raise Http404
    return d


def _organisation(request, deadline: Deadline) -> Organisation | None:
    """The organisation whose claims the page shows: ?org=CODE for those who may read it (CPUC staff), else the one the user acts for."""
    code = request.GET.get("org") or request.POST.get("org")
    if code:
        org = get_object_or_404(Organisation, code=code)
        if not service.can_read_submission(request.user, Submission(deadline=deadline, organisation=org)):
            raise Http404
        return org
    return request.organisation


def _pricing_run(v: SubmissionVersion) -> str | None:
    """The run started for the version since its records last changed, while it has not finished."""
    if v.calculation:
        return None
    events = AuditEvent.objects.filter(object_type="submissions.submissionversion", object_id=str(v.pk))
    last = (
        events.filter(action__in=["submission.pricing_started", "submission.files_attached", "submission.validated"])
        .order_by("-id")
        .first()
    )
    return (last.after or {}).get("run") if last and last.action == "submission.pricing_started" else None


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
    return f"{float(x):,.0f}"


# --- home and set-up


@login_required
def index(request):
    """Claim years, newest first: each confirmation's due date and the acting organisation's status."""
    org = request.organisation
    years = []
    for cy in ClaimYear.objects.select_related("allocation_version"):
        rows = []
        for d in service.periods(cy.year):
            latest = None
            if org is not None:
                sub = Submission.objects.filter(deadline=d, organisation=org).first()
                latest = submissions.latest(sub) if sub else None
            rows.append({"deadline": d, "latest": latest, "past_due": submissions.past_due(d)})
        years.append({"cy": cy, "periods": rows})
    return render(request, "claims/index.html", {"years": years, "org": org, "sysadmin": access.is_system_admin(request.user)})


def template(request):
    """The claims template workbook: public, like the data dictionary."""
    response = HttpResponse(spec.template_xlsx(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="claims_template.xlsx"'
    return response


@login_required
def year_new(request):
    if not access.is_system_admin(request.user):
        raise Http404
    form = forms.YearForm(request.POST or None, accs=accs())
    if request.method != "POST":
        acc = service.default_acc(form.fields["year"].initial)
        if acc in accs():
            form.fields["acc_version"].initial = acc
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            cy = service.add_year(
                request.user,
                year=d["year"],
                acc_version=d["acc_version"],
                allocation_version=d["allocation_version"],
                due={kind: service.due_at(d[forms.due_field(kind)]) for kind, _ in service.PERIODS},
                notes=d["notes"],
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"{cy.year} claims are set up.")
            return redirect("claims:index")
    return render(request, "claims/year_new.html", {"form": form, "periods": service.PERIODS})


@login_required
def setup(request, year):
    cy = get_object_or_404(ClaimYear, year=year)
    if not access.is_system_admin(request.user):
        raise Http404
    form = forms.SetupForm(
        request.POST or None, accs=accs(), initial={"acc_version": cy.acc_version, "allocation_version": cy.allocation_version}
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            service.change_setup(
                request.user, cy, acc_version=d["acc_version"], allocation_version=d["allocation_version"], reason=d["reason"]
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Saved. Versions already priced keep what they were priced with.")
            return redirect("claims:index")
    return render(request, "claims/setup.html", {"cy": cy, "form": form, "default_acc": service.default_acc(cy.year)})


# --- a PA's claims for one confirmation


@login_required
def period(request, pk):
    """One confirmation for one organisation: its versions, what its upload changes in the year to date, and the next step (upload,
    fix, price, confirm, un-confirm, ask to reopen); the earlier confirmations' compiled inputs (BR-02.5)."""
    deadline = _deadline(pk)
    org = _organisation(request, deadline)
    sub = Submission.objects.filter(deadline=deadline, organisation=org).first() if org else None
    if sub is not None and not service.can_read_submission(request.user, sub):
        raise Http404
    v = submissions.latest(sub) if sub else None
    can = {a: org is not None and access.has_permission(request.user, org, f"claims.{a}") for a in ("submit", "confirm")}
    acting = org is not None and org == request.organisation
    if v is not None:
        service.ensure_checked(v)
        v.refresh_from_db()
    cy = service.claim_year(deadline)
    earlier = []
    if org is not None:
        for d in service.periods(cy.year):
            if service.number(d) >= service.number(deadline):
                break
            earlier.append({"deadline": d, "standing": service.standing(org, d)})
    ctx = {
        "deadline": deadline,
        "label": service.label(deadline),
        "cy": cy,
        "org": org,
        "acting": acting,
        "sub": sub,
        "v": v,
        "s": (v.findings or {}) if v else {},
        "versions": _with_letters(list(sub.versions.select_related("confirmed_by").order_by("-number"))) if sub else [],
        "past_due": submissions.past_due(deadline),
        "opens_later": deadline.opens_at and timezone.now() < deadline.opens_at,
        "can_submit": can["submit"] and acting,
        "can_confirm": can["confirm"] and acting,
        "upload_form": forms.UploadForm(),
        "pending_reopen": sub.reopen_requests.filter(status=ReopenRequest.Status.PENDING).first() if sub else None,
        "sysadmin": access.is_system_admin(request.user),
        "earlier": earlier,
        "last_quarter": service.last_quarter(deadline),
        "later_locked": service.later_locked(org, deadline) if org else None,
        "tables": spec.TABLES,
    }
    if v is not None:
        ctx["pricing_run"] = _pricing_run(v)
        ctx["priced"] = service.priced(v)
        ctx["stale"] = service.stale(v)
        ctx["file"] = service.own_file(v)
        ctx["counts"] = [
            (t, ctx["s"].get("tables", {}).get(t, 0), ctx["s"].get("replaced", {}).get(t, 0), ctx["s"].get("compiled", {}).get(t, 0))
            for t in spec.TABLES
            if t in ctx["s"].get("compiled", {})
        ]
        if ctx["priced"]:
            ctx["tiles"] = _tiles(service.table(v))
    return render(request, "claims/period.html", ctx)


def _with_letters(versions: list[SubmissionVersion]) -> list[SubmissionVersion]:
    """The versions, each with .letter: its change letter, if it is a published correction."""
    letters = {c.version_id: c for c in ChangeLetter.objects.filter(version__in=versions)}
    for ver in versions:
        ver.letter = letters.get(ver.pk)
    return versions


def _tiles(t) -> list[tuple[str, str]]:
    return [
        ("Expenditures", _money(t.total.expenditures)),
        ("Total system benefit", _fmt("money", t.total.value("TotalSystemBenefit"))),
        ("TRC", _fmt("ratio", t.total.value("TRC"))),
        ("PAC", _fmt("ratio", t.total.value("PAC"))),
        ("Claims", f"{t.total.claims:,}"),
    ]


def _latest(deadline, org) -> SubmissionVersion:
    sub = Submission.objects.filter(deadline=deadline, organisation=org).first()
    v = submissions.latest(sub) if sub else None
    if v is None:
        raise AccessDenied("Upload the claims first.")
    return v


def _action(request, pk, fn, done: str):
    deadline = _deadline(pk)
    org = request.organisation
    if org is None:
        raise Http404
    try:
        fn(deadline, org)
    except (AccessDenied, UploadError) as e:
        messages.error(request, str(e))
    else:
        if done:
            messages.success(request, done)
    return redirect("claims:period", pk)


def _checked_message(request, v: SubmissionVersion) -> None:
    s = v.findings or {}
    if v.status == SubmissionVersion.Status.VALIDATED:
        msg = f"Checked {s.get('records', 0):,} records: none rejected"
        msg += f", {s['warnings']:,} with warnings." if s.get("warnings") else "."
        messages.info(request, msg + " Price the year to date next.")
    else:
        messages.warning(request, f"{s.get('rejected', 0):,} records are rejected. Fix them and upload the file again.")


@login_required
@require_POST
def upload(request, pk):
    form = forms.UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose the claims .zip or .xlsx file.")
        return redirect("claims:period", pk)
    f = form.cleaned_data["file"]
    return _action(request, pk, lambda d, o: _checked_message(request, service.upload_claims(request.user, d, o, f.read(), f.name)), "")


@login_required
@require_POST
def check(request, pk):
    return _action(request, pk, lambda d, o: _checked_message(request, service.check(request.user, _latest(d, o))), "")


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
    reason = request.POST.get("reason", "")
    return _action(
        request,
        pk,
        lambda d, o: service.unconfirm(request.user, _latest(d, o), reason),
        "Un-confirmed: the version is back to validated and no longer published.",
    )


@login_required
@require_POST
def reopen(request, pk):
    reason = request.POST.get("reason", "")

    def do(deadline, org):
        sub = Submission.objects.filter(deadline=deadline, organisation=org).first()
        if sub is None:
            raise AccessDenied("Nothing to reopen.")
        out = service.request_reopen(request.user, sub, reason)
        if isinstance(out, SubmissionVersion):
            messages.info(request, f"Version {out.number} is open as a draft.")
        else:
            messages.info(request, "Your request is with a CPUC approver.")

    return _action(request, pk, do, "")


# --- a version's results


def _version(request, pk) -> SubmissionVersion:
    v = get_object_or_404(
        SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation"),
        pk=pk,
        submission__deadline__module=service.MODULE,
        submission__deadline__kind__in=service.KINDS,
    )
    if not service.can_read(request.user, v):
        raise Http404
    return v


@login_required
def version(request, pk):
    """A version's year to date by program: what was filed and what it priced to, and what it publishes."""
    v = _version(request, pk)
    pa = request.GET.get("pa", "")
    t = service.table(v, pa=pa)
    rows = [{"r": r, "cells": [_fmt(kind, r.value(key)) for key, _, kind, _ in service.METRICS]} for r in t.rows]
    total = [_fmt(kind, t.total.value(key)) for key, _, kind, _ in service.METRICS]
    return render(
        request,
        "claims/version.html",
        {
            "v": v,
            "label": service.label(v.submission.deadline),
            "t": t,
            "pa": pa,
            "head": [m[1] for m in service.METRICS],
            "rows": rows,
            "total": total,
            "tiles": _tiles(t) if t.priced else [],
            "definitions": [(m[1], m[3]) for m in service.METRICS],
            "withdrawals": service.withdrawals(v),
            "letter": ChangeLetter.objects.filter(version=v).first(),
            "files": [(lbl, i) for i, (lbl, _, _) in enumerate(service.published_files(v))],
            "public": v.status == SubmissionVersion.Status.PUBLISHED,
            "calc": v.calculation or {},
            "s": v.findings or {},
            "csv_query": f"?pa={pa}" if pa else "",
        },
    )


@login_required
def version_csv(request, pk):
    """The table as shown, from the same filter (decision H), with the cost categories and the filed totals."""
    from .models import FILED_TOTALS

    v = _version(request, pk)
    t = service.table(v, pa=request.GET.get("pa", ""))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    name = f"{v.submission.organisation.code}_{service.label(v.submission.deadline)}_v{v.number}".replace(" ", "_")
    response["Content-Disposition"] = f'attachment; filename="claims_{name}.csv"'
    w = csv.writer(response, lineterminator="\n")
    priced = [(key, kind) for key, _, kind, _ in service.METRICS if kind in ("money", "ratio", "number") and key != "expenditures"]
    w.writerow(
        ["PrgID", "ProgramName", "PA", "Claims"]
        + [k for k, _ in PROGRAM_COST_FIELDS]
        + ["RebatesandIncents", "Expenditures", "OnBillFinancing"]
        + FILED_TOTALS
        + [key if kind != "ratio" else f"{key}Ratio" for key, kind in priced]
    )
    for r in t.rows + [t.total]:
        w.writerow(
            [r.prg_id, r.name, r.pa, r.claims]
            + [r.by_field.get(k, 0) for k, _ in PROGRAM_COST_FIELDS]
            + [r.rebates, r.expenditures, r.on_bill_financing]
            + [r.filed.get(k, 0) for k in FILED_TOTALS]
            + ["" if r.value(key) is None else r.value(key) for key, _ in priced]
        )
    return response


@login_required
def findings_csv(request, pk):
    v = _version(request, pk)
    if not service.can_read_submission(request.user, v.submission):
        raise Http404
    response = HttpResponse(service.findings_csv(v), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="claims_findings_v{v.number}.csv"'
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


# --- corrections: the change log (BR-09.3)


@login_required
def changes(request):
    """Every published correction's change letter, newest first, by PA and claim year."""
    letters = service.change_letters()
    org = Organisation.objects.filter(code=request.GET.get("org", "")).first() if request.GET.get("org") else None
    year = request.GET.get("year", "")
    if org is not None:
        letters = letters.filter(version__submission__organisation=org)
    if year.isdigit():
        letters = letters.filter(version__submission__deadline__period__year=int(year))
    every = service.change_letters()
    return render(
        request,
        "claims/changes.html",
        {
            "letters": letters[:500],
            "orgs": sorted({c.version.submission.organisation.code for c in every}),
            "years": sorted({c.version.submission.deadline.period.year for c in every}, reverse=True),
            "org": org.code if org else "",
            "year": year,
        },
    )


def _figure(kind: str, x) -> str:
    if x is None:
        return ""
    return f"{x:,}" if kind == "count" else _fmt(kind, x)


def _difference(kind: str, before, after) -> str:
    if before is None or after is None:
        return ""
    d = after - before
    if not d:
        return "No change"
    sign = "+" if d > 0 else "-"
    return sign + _figure(kind, abs(d)).lstrip("-")


@login_required
def change_letter(request, pk):
    """One correction's change letter: when it was published, why, and what it changed against the version it replaces."""
    letter = get_object_or_404(service.change_letters(), pk=pk)
    c = letter.changes or {}
    v = letter.version
    figures = service.LETTER_FIGURES

    def row(name, before, after):
        before, after = before or {}, after or {}
        return {
            "name": name,
            "cells": [
                (_figure(kind, before.get(key)), _figure(kind, after.get(key)), _difference(kind, before.get(key), after.get(key)))
                for key, _, kind in figures
            ],
        }

    programs = []
    for p in c.get("programs", []):
        r = row(p["prg_id"], p["before"], p["after"])
        r.update({"title": p.get("name", ""), "new": p["before"] is None, "gone": p["after"] is None})
        programs.append(r)
    totals = c.get("totals", {})
    claims = c.get("claims", {})
    groups = [
        ("added", "Claims added", "New ClaimIDs filed by the correction."),
        ("changed", "Claims changed", "Claims with any record (Claim, Measure, contacts or infrastructure cost) that differs."),
        ("removed", "Claims removed", "Claims the version replaced had added, which the correction does not file."),
        ("reinstated", "Withdrawals undone", "Claims the version replaced withdrew, which the correction no longer withdraws."),
    ]
    return render(
        request,
        "claims/change_letter.html",
        {
            "letter": letter,
            "v": v,
            "label": service.label(v.submission.deadline),
            "c": c,
            "claims": claims,
            "groups": [(key, title, help, claims.get(key, {})) for key, title, help in groups],
            "withdrawn": claims.get("withdrawn", {}),
            "head": [name for _, name, _ in figures],
            "programs": programs,
            "total": row("Total", totals.get("before"), totals.get("after")),
            "priced": c.get("priced", {}),
            "colspan": 3 * len(figures) + 1,
        },
    )
