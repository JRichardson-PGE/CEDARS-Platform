from datetime import datetime, time

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.core import storage
from cedars.core import uploads as core_uploads
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry import service as registry

from . import comments, corrections, service
from .models import Attachment, Deadline, ReopenRequest, SubmissionVersion, WorkflowPolicy

MODULES = [("budgets", "Budgets"), ("claims", "Claims"), ("monthly", "Monthly reports")]


class DeadlineForm(forms.Form):
    module = forms.ChoiceField(label="Module", choices=MODULES)
    kind = forms.CharField(label="What is due", max_length=80, help_text="e.g. Q1 claim, TUAL, monthly report")
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
    deadlines = [
        {"d": d, "url": service.deadline_page(d)}
        for d in Deadline.objects.select_related("period", "cycle")
        .filter(due_at__gte=timezone.now() - timezone.timedelta(days=400))
        .order_by("due_at", "module", "period__starts", "kind")
    ]
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


@login_required
@require_POST
def comment(request, pk):
    """Post a comment, with any files, on a filing version (BR-09.5), then return to the version's page."""
    v = get_object_or_404(SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation"), pk=pk)
    if not comments.can_discuss(request.user, v.submission):
        raise Http404
    uploads = [(f.name, f) for f in request.FILES.getlist("files")]
    try:
        c = comments.post(request.user, v, request.POST.get("text", ""), uploads)
    except (AccessDenied, comments.CommentRefused) as e:
        messages.error(request, f"Your comment was not posted: {e}")
        anchor = "#comments"
    else:
        messages.success(request, "Comment posted.")
        anchor = f"#comment-{c.pk}"
    page = comments.version_page(v)
    return redirect(page + anchor) if page else redirect("core:queue")


@login_required
def attachment(request, pk):
    """A file attached to a comment, for the filing organization and CPUC staff; each download is an audit event on the version."""
    try:
        a = comments.attachment_for(request.user, pk)
    except (Attachment.DoesNotExist, AccessDenied) as e:
        raise Http404 from e
    comments.record_download(request.user, a)
    store = storage.store("uploads")
    url = store.download_url(a.key, a.name, content_type=a.content_type)
    if url:
        return HttpResponseRedirect(url)
    response = FileResponse(store.open(a.key), as_attachment=True, filename=a.name, content_type=a.content_type)
    response["X-Content-Type-Options"] = "nosniff"
    return response


# --- correcting a filing's records in place (BR-02.4)


def _correctable(request, pk) -> tuple[SubmissionVersion, str]:
    """The version, if the user may submit its filing (else 404), and why its records cannot be corrected now ("" = they can)."""
    v = get_object_or_404(SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation"), pk=pk)
    sub = v.submission
    if not access.has_permission(request.user, sub.organisation, f"{sub.deadline.module}.submit"):
        raise Http404
    e = corrections.editor_for(sub.deadline.module)
    if e is None:
        raise Http404
    if not v.locked and not v.imported:
        e.prepare(v)
        v.refresh_from_db()
    return v, corrections.refusal(request.user, v)


def _base_context(v: SubmissionVersion, why: str) -> dict:
    e = corrections.editor_for(v.submission.deadline.module)
    return {
        "v": v,
        "why": why,
        "editor": e,
        "back": e.version_page(v) if e else "",
        "tables": e.tables(v) if e and not why else [],
        "state": corrections.state(v),
        "max_rows": corrections.max_rows(),
        "list_rows": corrections.list_rows(),
        "max_mb": core_uploads.max_mb(),
    }


def _key_from(request, t) -> list[str]:
    key = request.GET.getlist("k") or request.POST.getlist("k")
    if len(key) != len(t.key) or not all(k.strip() for k in key):
        raise Http404
    return key


def _key_query(t, key) -> str:
    from urllib.parse import urlencode

    return urlencode([("k", k) for k in key])


def _outcome(request, v: SubmissionVersion, scope) -> None:
    s = v.findings or {}
    n = sum(1 for c in scope.changes if c.kind != "carry")
    what = f"Saved {n:,} record{'s' if n != 1 else ''}"
    again = (s.get("checked_again") or {}).get("records")
    checked = f"checked {again:,} record{'s' if again != 1 else ''} again" if again is not None else "checked every record again"
    if v.status == SubmissionVersion.Status.VALIDATED:
        rest = "none is rejected, so the version is validated. Price it again before confirming."
        rest += f" {s['warnings']:,} have warnings." if s.get("warnings") else ""
        messages.success(request, f"{what} and {checked}: {rest}")
    else:
        messages.warning(request, f"{what} and {checked}: {s.get('rejected', 0):,} records are still rejected.")


@login_required
def records(request, pk):
    """Find a version's records to correct, add one, or upload a partial correction (BR-02.4)."""
    v, why = _correctable(request, pk)
    ctx = _base_context(v, why)
    if not why:
        table, q = request.GET.get("table", ""), request.GET.get("q", "").strip()
        names = {t.name for t in ctx["tables"]}
        table = table if table in names else ""
        ctx.update({"table": table, "q": q, "searched": "q" in request.GET or "table" in request.GET})
        if ctx["searched"]:
            found, n = corrections.find(v, table, q)
            defs = {t.name: t for t in ctx["tables"]}
            ctx["found"] = [(f, _key_query(defs[f.table], f.key)) for f in found]
            ctx["found_count"] = n
        ctx["summary"] = corrections.summary(v)
        ctx["s"] = v.findings or {}
    return render(request, "submissions/records.html", ctx)


def _field_rows(t, values: dict, findings: list[dict], *, key_locked: bool) -> list[dict]:
    by_field: dict[str, list[dict]] = {}
    for f in findings:
        for name in str(f.get("field", "")).split(", "):
            by_field.setdefault(name, []).append(f)
    return [
        {
            "f": fd,
            "value": values.get(fd.name, ""),
            "findings": by_field.get(fd.name, []),
            "locked": key_locked and fd.name in t.key,
        }
        for fd in t.fields
    ]


@login_required
def record(request, pk, table):
    """One record on a form: its findings beside its fields; saving re-checks it and the records its checks touch."""
    v, why = _correctable(request, pk)
    if why:
        return render(request, "submissions/records.html", _base_context(v, why))
    t = corrections.tables_of(v).get(table)
    if t is None:
        raise Http404
    key = _key_from(request, t)
    found = corrections.record(v, table, key)
    if found is None:
        raise Http404
    values = dict(found.row)
    if request.method == "POST":
        posted = {fd.name: request.POST.get(f"f-{fd.name}", "") for fd in t.fields}
        if found.earlier:  # an earlier record's key stays
            posted.update({c: found.row.get(c, "") for c in t.key})
        have = set(corrections.editor(v).rows(v).get(table, corrections.Rows(table, [], [])).columns) | set(found.row)
        posted = {c: x for c, x in posted.items() if c in have or x.strip()}  # a blank column the file never had stays out of it
        try:
            v, scope = corrections.correct(
                request.user, v, table, key, posted, reason=request.POST.get("reason", "").strip(), expected=request.POST.get("state", "")
            )
        except (AccessDenied, corrections.CorrectionRefused) as e:
            messages.error(request, f"Not saved: {e}")
            values.update(posted)
        else:
            _outcome(request, v, scope)
            new = next((c.after for c in scope.changes if c.kind == "edit"), None) or found.row
            url = reverse("submissions:record", args=[v.pk, table]) + "?" + _key_query(t, [new.get(c, "") for c in t.key])
            return redirect(url)
    findings = corrections.row_findings(v, table, found.line)
    ctx = _base_context(v, "")
    ctx.update(
        {
            "t": t,
            "found": found,
            "key_query": _key_query(t, key),
            "rows": _field_rows(t, values, findings, key_locked=bool(found.earlier)),
            "findings": findings,
            "rejected": any(f.get("severity") == "reject" for f in findings),
            "mode": "edit",
        }
    )
    return render(request, "submissions/record.html", ctx)


@login_required
def record_new(request, pk, table):
    v, why = _correctable(request, pk)
    if why:
        return render(request, "submissions/records.html", _base_context(v, why))
    t = corrections.tables_of(v).get(table)
    if t is None:
        raise Http404
    values: dict = {}
    if request.method == "POST":
        values = {fd.name: request.POST.get(f"f-{fd.name}", "") for fd in t.fields}
        have = set(corrections.editor(v).rows(v).get(table, corrections.Rows(table, [], [])).columns) | set(t.key)
        posted = {c: x for c, x in values.items() if c in have or x.strip()}
        try:
            v, scope = corrections.add(
                request.user, v, table, posted, reason=request.POST.get("reason", "").strip(), expected=request.POST.get("state", "")
            )
        except (AccessDenied, corrections.CorrectionRefused) as e:
            messages.error(request, f"Not added: {e}")
        else:
            _outcome(request, v, scope)
            new = scope.changes[-1].after
            return redirect(reverse("submissions:record", args=[v.pk, table]) + "?" + _key_query(t, [new.get(c, "") for c in t.key]))
    ctx = _base_context(v, "")
    ctx.update({"t": t, "rows": _field_rows(t, values, [], key_locked=False), "findings": [], "mode": "add"})
    return render(request, "submissions/record.html", ctx)


@login_required
def record_delete(request, pk, table):
    v, why = _correctable(request, pk)
    if why:
        return render(request, "submissions/records.html", _base_context(v, why))
    t = corrections.tables_of(v).get(table)
    if t is None:
        raise Http404
    key = _key_from(request, t)
    found = corrections.record(v, table, key)
    if found is None:
        raise Http404
    if request.method == "POST":
        try:
            v, scope = corrections.delete(
                request.user, v, table, key, reason=request.POST.get("reason", "").strip(), expected=request.POST.get("state", "")
            )
        except (AccessDenied, corrections.CorrectionRefused) as e:
            messages.error(request, f"Not deleted: {e}")
        else:
            _outcome(request, v, scope)
            return redirect("submissions:records", v.pk)
    ctx = _base_context(v, "")
    ctx.update({"t": t, "found": found, "key_query": _key_query(t, key)})
    return render(request, "submissions/record_delete.html", ctx)


@login_required
@require_POST
def correction_upload(request, pk):
    """Take a partial correction's file and show what applying it would change."""
    v, why = _correctable(request, pk)
    if why:
        messages.error(request, why)
        return redirect("submissions:records", v.pk)
    f = request.FILES.get("file")
    if f is None:
        messages.error(request, "Choose the correction's .zip, .xlsx or .csv file.")
        return redirect("submissions:records", v.pk)
    if problem := core_uploads.too_large(f.name, f.size):
        messages.error(request, problem)
        return redirect("submissions:records", v.pk)
    try:
        p = corrections.preview(request.user, v, f.read(), f.name)
    except (AccessDenied, corrections.CorrectionRefused) as e:
        messages.error(request, f"The correction was not read: {e}")
        return redirect(reverse("submissions:records", args=[v.pk]) + "#partial-h")
    return redirect("submissions:correction", v.pk, p.token)


def _preview_of(request, v: SubmissionVersion, token: str):
    got = corrections.pending(v, token)
    if got is None:
        raise Http404
    data, name = got
    return corrections.preview(request.user, v, data, name)


@login_required
def correction(request, pk, token):
    """A partial correction's preview: what applying it would change, record by record; nothing is changed until it is applied."""
    v, why = _correctable(request, pk)
    if why:
        return render(request, "submissions/records.html", _base_context(v, why))
    try:
        p = _preview_of(request, v, token)
    except corrections.CorrectionRefused as e:
        messages.error(request, f"The correction no longer applies: {e}")
        return redirect("submissions:records", v.pk)
    ctx = _base_context(v, "")
    limit = corrections.list_rows()
    shown = [c for c in p.changes if c.kind != "carry"][:limit]
    ctx.update(
        {
            "p": p,
            "shown": [{"c": c, "fields": [(n, (c.before or {}).get(n, ""), (c.after or {}).get(n, "")) for n in c.fields]} for c in shown],
            "carried": sum(1 for c in p.changes if c.kind == "carry"),
            "counts": [(table, kinds) for table, kinds in p.counts.items()],
            "more": p.total - len(shown),
        }
    )
    return render(request, "submissions/correction.html", ctx)


@login_required
def correction_csv(request, pk, token):
    v, why = _correctable(request, pk)
    if why:
        raise Http404
    try:
        p = _preview_of(request, v, token)
    except corrections.CorrectionRefused as e:
        raise Http404 from e
    response = HttpResponse(corrections.changes_csv(p, corrections.tables_of(v)), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="correction_v{v.number}_{token[:12]}.csv"'
    return response


@login_required
@require_POST
def correction_apply(request, pk, token):
    v, why = _correctable(request, pk)
    if why:
        messages.error(request, why)
        return redirect("submissions:records", v.pk)
    try:
        v, scope = corrections.apply_preview(
            request.user, v, token, request.POST.get("state", ""), reason=request.POST.get("reason", "").strip()
        )
    except (AccessDenied, corrections.CorrectionRefused) as e:
        messages.error(request, f"Not applied: {e}")
        return redirect("submissions:records", v.pk)
    _outcome(request, v, scope)
    e = corrections.editor_for(v.submission.deadline.module)
    return redirect(e.version_page(v) or reverse("submissions:records", args=[v.pk]))
