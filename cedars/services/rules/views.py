import json
from collections import Counter

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cedars.core import uploads
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import forms, precheck, service
from .models import Rule, RuleSet

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
            "version_form": forms.NewVersionForm(auto_id="version_%s"),
            "drafts": RuleSet.objects.filter(code=rs.code, status=RuleSet.Status.DRAFT).exclude(pk=rs.pk),
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


def _check_choices():
    """Each module's published rule-set versions, the one in force first and selected by default."""
    out = []
    for c in precheck.checkers():
        current = service.in_force(c.code)
        versions = [rs for rs in precheck.versions() if rs.code == c.code]
        versions.sort(key=lambda rs: (current is None or rs.pk != current.pk, -rs.effective_from.toordinal()))
        if versions:
            out.append({"checker": c, "versions": versions, "in_force": current})
    return out


@login_required
def check(request):
    """The pre-upload checker (BR-03.4): the rule-set step of a module's validation, run on a file before it is filed, against the
    version in force or any other published one (rules.precheck: the same code the upload runs). Nothing uploaded is kept."""
    choices = _check_choices()
    ctx = {"choices": choices, "max_mb": uploads.max_mb()}
    if request.method == "POST":
        rs = RuleSet.objects.filter(pk=request.POST.get("rule_set") or 0, status=RuleSet.Status.PUBLISHED).first()
        upload = request.FILES.get("file")
        table = request.POST.get("table", "Measure")
        year = request.POST.get("filing_year", "").strip()
        if rs is None or upload is None or table not in precheck.TABLES or (year and not year.isdigit()):
            messages.error(request, "Choose the rules, the table and a file.")
            return redirect("rules:check")
        problem = uploads.too_large(upload.name, upload.size)
        if problem:
            messages.error(request, f"{problem} Check the table in parts.")
            return redirect("rules:check")
        try:
            result = precheck.check(None, table, upload.read(), rule_set=rs, filing_year=int(year) if year else None)
        except service.EngineMissing as e:
            messages.error(request, str(e))
            return redirect("rules:check")
        except precheck.CheckError as e:
            messages.error(request, str(e))
            return redirect("rules:check")
        except Exception as e:  # noqa: BLE001 - a file we cannot read is reported, not a server error
            messages.error(request, f"The file could not be read as a {table} table: {e}")
            return redirect("rules:check")
        findings = result.findings
        if request.POST.get("download"):
            resp = HttpResponse(service.findings_csv(findings), content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="findings_{table}.csv"'
            return resp
        ctx |= {
            "rs": rs,
            "result": result,
            "table": table,
            "filing_year": year,
            "findings": findings[:SHOWN],
            "total": len(findings),
            "notes": result.notes,
            "records": result.records,
            "rejected": result.rejected,
            "warned": result.warned,
            "by_rule": Counter((f.rule_id, f.severity, f.message.split(", but is")[0]) for f in findings).most_common(20),
            "filename": upload.name,
        }
    return render(request, "rules/check.html", ctx)


def rule_set_json(request, pk):
    """A published rule set as JSON with how each rule is checked: the downloadable form of the pre-upload checker, for systems that
    check files without CEDARS' code (BR-03.4). Public, as the rule library is."""
    rs = get_object_or_404(RuleSet, pk=pk, status=RuleSet.Status.PUBLISHED)
    body = json.dumps(precheck.rule_set_json(rs, request.build_absolute_uri("/").rstrip("/")), indent=1, ensure_ascii=False)
    resp = HttpResponse(body, content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="rules_{rs.code}_{rs.effective_from:%Y%m%d}.json"'
    return resp


def rule(request, pk, rule_id):
    r = get_object_or_404(Rule.objects.select_related("rule_set"), rule_set_id=pk, rule_id=rule_id)
    sysadmin = request.user.is_authenticated and access.is_system_admin(request.user)
    replaced = None
    if r.replaces and r.rule_set.based_on_id:
        replaced = Rule.objects.filter(rule_set_id=r.rule_set.based_on_id, rule_id=r.replaces).first()
    return render(request, "rules/rule.html", {"r": r, "sysadmin": sysadmin, "replaced": replaced})


# --- administrators change the rules as new versions (BR-12.6)


def _admin(request):
    if not access.is_system_admin(request.user):
        raise PermissionDenied("Only a CPUC system administrator changes the rules.")


@login_required
@require_POST
def new_version(request, pk):
    _admin(request)
    rs = get_object_or_404(RuleSet, pk=pk)
    form = forms.NewVersionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the date the new version takes effect and why the rules change.")
        return redirect("rules:detail", pk=rs.pk)
    try:
        new = service.new_version(request.user, rs, form.cleaned_data["effective_from"], form.cleaned_data["reason"])
    except (AccessDenied, service.RuleError) as e:
        messages.error(request, str(e))
        return redirect("rules:detail", pk=rs.pk)
    messages.success(request, f"Started a draft of {new}: change its rules, then publish it.")
    return redirect("rules:detail", pk=new.pk)


@login_required
@require_POST
def discard(request, pk):
    _admin(request)
    rs = get_object_or_404(RuleSet, pk=pk)
    try:
        service.discard_draft(request.user, rs, request.POST.get("reason", ""))
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("rules:detail", pk=rs.pk)
    messages.success(request, f"The draft {rs} is discarded.")
    return redirect("rules:index")


def _rule_page(request, rs, form, r=None):
    return render(request, "rules/rule_form.html", {"rs": rs, "form": form, "r": r})


@login_required
def add_rule(request, pk):
    _admin(request)
    rs = get_object_or_404(RuleSet, pk=pk)
    if rs.status != RuleSet.Status.DRAFT:
        messages.error(request, "A published rule set never changes: start a new version and add the rule to it.")
        return redirect("rules:detail", pk=rs.pk)
    if request.method != "POST":
        return _rule_page(
            request, rs, forms.RuleForm(initial={"kind": "rule", "severity": "reject", "modules": service.MODULES_FOR.get(rs.code, [])})
        )
    form = forms.RuleForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            r = service.add_rule(
                request.user,
                rs,
                kind=d["kind"],
                severity=d["severity"],
                table=d["table"],
                field=d["field"],
                definition=d["definition"],
                message=d["message"],
                fix=d["fix"],
                modules=d["modules"],
                reason=d["reason"],
            )
        except (AccessDenied, service.RuleError) as e:
            form.add_error(None, str(e))
        else:
            if d["difference"]:
                service.edit_rule(request.user, r, difference=d["difference"], reason=d["reason"])
            messages.success(request, f"Added rule {r.rule_id}.")
            return redirect("rules:rule", pk=rs.pk, rule_id=r.rule_id)
    return _rule_page(request, rs, form)


@login_required
def edit_rule(request, pk, rule_id):
    _admin(request)
    r = get_object_or_404(Rule.objects.select_related("rule_set"), rule_set_id=pk, rule_id=rule_id)
    rs = r.rule_set
    if rs.status != RuleSet.Status.DRAFT:
        messages.error(request, "A published rule set never changes: start a new version and change the rule there.")
        return redirect("rules:rule", pk=rs.pk, rule_id=r.rule_id)
    if request.method != "POST":
        return _rule_page(request, rs, forms.RuleForm(rule=r), r)
    form = forms.RuleForm(request.POST, rule=r)
    if form.is_valid():
        d = form.cleaned_data
        test_changed = any(
            v != getattr(r, k)
            for k, v in (
                ("kind", d["kind"]),
                ("severity", d["severity"]),
                ("table", d["table"]),
                ("field", d["field"]),
                ("definition", d["definition"]),
            )
        )
        wording = {k: d[k] for k in ("message", "fix")}
        if test_changed:  # a blank message or fix is written again from the new test; an unchanged one is too
            wording = {k: (v if v.strip() and v != getattr(r, k) else None) for k, v in wording.items()}
        try:
            r = service.edit_rule(
                request.user,
                r,
                severity=d["severity"],
                table=d["table"],
                field=d["field"],
                kind=d["kind"],
                definition=d["definition"],
                difference=d["difference"],
                modules=d["modules"],
                reason=d["reason"],
                **wording,
            )
        except (AccessDenied, service.RuleError) as e:
            form.add_error(None, str(e))
        else:
            messages.success(
                request,
                f"Saved rule {r.rule_id}."
                + (f" Its test changed, so it replaces {r.replaces}." if r.replaces and r.replaces != rule_id else ""),
            )
            return redirect("rules:rule", pk=rs.pk, rule_id=r.rule_id)
    return _rule_page(request, rs, form, r)


@login_required
@require_POST
def remove_rule(request, pk, rule_id):
    _admin(request)
    r = get_object_or_404(Rule, rule_set_id=pk, rule_id=rule_id)
    try:
        service.remove_rule(request.user, r, request.POST.get("reason", ""))
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("rules:rule", pk=pk, rule_id=rule_id)
    messages.success(request, f"Removed rule {rule_id} from this draft.")
    return redirect("rules:detail", pk=pk)
