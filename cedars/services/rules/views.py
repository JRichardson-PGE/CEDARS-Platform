from collections import Counter

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import service
from .models import Rule, RuleSet

MAX_UPLOAD = 200 * 1024 * 1024
SHOWN = 200


def index(request):
    """The rule library, public (CEDARS publishes its rules): every set and version, which one is in force, what differs between them."""
    sets = RuleSet.objects.all()
    in_force = {code: service.in_force(code) for code in sets.values_list("code", flat=True).distinct()}
    return render(
        request,
        "rules/index.html",
        {
            "sets": sets,
            "in_force": {v.pk for v in in_force.values() if v},
            "sysadmin": request.user.is_authenticated and access.is_system_admin(request.user),
        },
    )


def detail(request, pk):
    rs = get_object_or_404(RuleSet, pk=pk)
    rules = rs.rules.all()
    severity, table, q = request.GET.get("severity", ""), request.GET.get("table", ""), request.GET.get("q", "").strip()
    if severity:
        rules = rules.filter(severity=severity)
    if table:
        rules = rules.filter(table=table)
    if q:
        rules = rules.filter(field__icontains=q) | rules.filter(message__icontains=q).filter(rule_set=rs)
    if request.GET.get("format") == "csv":
        import csv
        import io

        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["rule_id", "severity", "table", "field", "message", "suggested_fix", "kind", "modules", "difference", "source"])
        for r in rules:
            w.writerow([r.rule_id, r.severity, r.table, r.field, r.message, r.fix, r.kind, " ".join(r.modules), r.difference, r.source])
        resp = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="rules_{rs.code}_{rs.effective_from:%Y%m%d}.csv"'
        return resp
    counts = Counter(rs.rules.values_list("severity", flat=True))
    return render(
        request,
        "rules/detail.html",
        {
            "rs": rs,
            "rules": rules,
            "counts": counts,
            "tables": sorted(set(rs.rules.values_list("table", flat=True))),
            "severity": severity,
            "table": table,
            "q": q,
            "sysadmin": request.user.is_authenticated and access.is_system_admin(request.user),
        },
    )


@login_required
@require_POST
def import_rules(request):
    try:
        rs = service.import_from_engine(request.user, request.POST.get("code", ""))
    except (AccessDenied, service.EngineMissing, ValueError) as e:
        messages.error(request, str(e))
        return redirect("rules:index")
    messages.success(request, f"Imported {rs} as a draft: review it, then publish it.")
    return redirect("rules:detail", pk=rs.pk)


@login_required
@require_POST
def publish(request, pk):
    rs = get_object_or_404(RuleSet, pk=pk)
    try:
        service.publish(request.user, rs)
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Published {rs}. It is in force from {rs.effective_from:%Y-%m-%d} until a newer version.")
    return redirect("rules:detail", pk=rs.pk)


def differences(request):
    sets = list(RuleSet.objects.all())
    a = RuleSet.objects.filter(pk=request.GET.get("a") or 0).first()
    b = RuleSet.objects.filter(pk=request.GET.get("b") or 0).first()
    diff = service.differences(a, b) if a and b else None
    return render(request, "rules/differences.html", {"sets": sets, "a": a, "b": b, "diff": diff})


@login_required
def check(request):
    """The pre-upload checker (BR-03.4): the rules CEDARS will apply, run on a file before it is filed. Nothing uploaded is kept."""
    sets = [rs for rs in (service.in_force(c) for c in RuleSet.objects.values_list("code", flat=True).distinct()) if rs]
    ctx = {"sets": sets}
    if request.method == "POST":
        rs = next((s for s in sets if str(s.pk) == request.POST.get("rule_set")), None)
        upload = request.FILES.get("file")
        table = request.POST.get("table", "Measure")
        year = request.POST.get("filing_year") or None
        if rs is None or upload is None or table not in ("Measure", "ProgramCost"):
            messages.error(request, "Choose the rules, the table and a file.")
            return redirect("rules:check")
        if upload.size > MAX_UPLOAD:
            messages.error(request, "That file is over 200 MB; check it in parts, or with the checker on your own computer.")
            return redirect("rules:check")
        try:
            df = service.read_table(upload.read())
            findings, notes = service.check(rs, table, df, filing_year=int(year) if year else None)
        except service.EngineMissing as e:
            messages.error(request, str(e))
            return redirect("rules:check")
        except Exception as e:  # noqa: BLE001 - a file we cannot read is reported, not a server error
            messages.error(request, f"The file could not be read as a {table} table: {e}")
            return redirect("rules:check")
        if request.POST.get("download"):
            resp = HttpResponse(service.findings_csv(findings), content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="findings_{table}.csv"'
            return resp
        records = len(df)
        rejected = len({f.record_id or f.row for f in findings if f.severity == "reject"})
        warned = len({f.record_id or f.row for f in findings if f.severity == "warning"})
        ctx |= {
            "rs": rs,
            "table": table,
            "findings": findings[:SHOWN],
            "total": len(findings),
            "notes": notes,
            "records": records,
            "rejected": rejected,
            "warned": warned,
            "by_rule": Counter((f.rule_id, f.severity, f.message.split(", but is")[0]) for f in findings).most_common(20),
            "filename": upload.name,
        }
    return render(request, "rules/check.html", ctx)


def rule(request, pk, rule_id):
    r = get_object_or_404(Rule, rule_set_id=pk, rule_id=rule_id)
    if r is None:
        raise Http404
    return render(request, "rules/rule.html", {"r": r})
