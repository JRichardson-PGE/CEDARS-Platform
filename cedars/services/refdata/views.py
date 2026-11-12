import tempfile
from pathlib import Path

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from cedars.core import storage, uploads
from cedars.services.accounts.service import AccessDenied

from . import cost_tests, etrm, service
from .models import PackVersion, TieOutRequirement


@login_required
def index(request):
    """Every data pack, by kind: what runs can use now, drafts, and withdrawn packs."""
    kind = request.GET.get("kind", "")
    packs = PackVersion.objects.all()
    if kind in PackVersion.Kind.values:
        packs = packs.filter(kind=kind)
    in_force = [(label, service.in_force(code)) for code, label in PackVersion.Kind.choices]
    return render(
        request,
        "refdata/index.html",
        {
            "packs": packs,
            "kinds": PackVersion.Kind.choices,
            "kind": kind,
            "in_force": [(label, p) for label, p in in_force if p],
            "can_load_etrm": service.can_manage(request.user, etrm.KIND),
        },
    )


@login_required
def detail(request, pk):
    pack = get_object_or_404(PackVersion, pk=pk)
    manager = service.can_manage(request.user, pack.kind)
    if request.method == "POST" and manager:
        action, reason = request.POST.get("action"), request.POST.get("reason", "")
        try:
            if action == "publish":
                service.publish_pack(request.user, pack)
                messages.success(request, f"{pack.label} published. Filing runs can use it now.")
            elif action == "withdraw":
                service.withdraw_pack(request.user, pack, reason)
                messages.success(request, f"{pack.label} withdrawn. Runs that used it still name it.")
            elif action == "engine_check" and pack.kind == cost_tests.KIND:
                t = cost_tests.engine_check(request.user, pack)
                if t is None:
                    messages.error(request, "The engine is not installed on this server; record the engine check by hand.")
                else:
                    (messages.success if t.passed else messages.error)(
                        request, f"Engine check {'passed' if t.passed else 'failed'}. {t.summary}"
                    )
            elif action == "discard":
                service.discard_draft(request.user, pack, reason)
                messages.success(request, "Draft discarded.")
                return redirect("refdata:index")
        except AccessDenied as e:
            messages.error(request, str(e))
        return redirect("refdata:detail", pk=pack.pk)
    pack.refresh_from_db()
    latest = {}
    for t in pack.tie_outs.order_by("recorded_at", "id"):
        latest[t.name] = t
    required = service.required_checks(pack)
    requirements = [(r, latest.get(r.name)) for r in required]
    ctx = {}
    if pack.kind == etrm.KIND:
        ctx = _permutation(pack, request.GET.get("id", "").strip())
    if pack.kind == cost_tests.KIND:
        words, problem = cost_tests.in_words(pack)
        ctx = {
            "cost_set": cost_tests.header(pack),
            "cost_years": cost_tests.years_text(pack),
            "cost_in_force": cost_tests.for_year(None, workstream=pack.label.split("-", 1)[0]) == pack,
            "cost_sha": cost_tests.sha256(pack),
            "cost_file": cost_tests.download_name(pack),
            "cost_words": words,
            "cost_problem": problem,
            "engine_here": cost_tests.engine_installed(),
            "cost_grid": cost_tests.grid(pack),
        }
    return render(
        request,
        "refdata/detail.html",
        {
            "pack": pack,
            "manager": manager,
            "files": sorted(pack.manifest.items())[:200],
            "requirements": requirements,
            "outstanding": service.outstanding(pack),
            "report_form": ReportForm(checks=required) if manager and required and pack.status == PackVersion.Status.DRAFT else None,
            "tie_outs": pack.tie_outs.select_related("recorded_by"),
            **ctx,
        },
    )


@login_required
def cost_test_file(request, pk):
    """A cost-test set's own file, checked against the pack's SHA-256 on the way out. Anyone signed in may start an analysis run on a
    registered set, so anyone signed in may read it: to see exactly what a run was priced on, or to start the next set from a copy."""
    pack = get_object_or_404(PackVersion, pk=pk, kind=cost_tests.KIND)
    try:
        data = cost_tests.file_bytes(pack)
    except storage.StorageError as e:
        raise Http404(str(e)) from None
    response = HttpResponse(data, content_type="application/yaml; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{cost_tests.download_name(pack)}"'
    return response


def _permutation(pack: PackVersion, mdid: str) -> dict:
    """An eTRM permutations pack's lookup: the permutation a MeasDetailID names, each value with the claims field it fills."""
    out = {"etrm": True, "mdid": mdid, "permutations": pack.etrm_permutations.count()}
    if not mdid:
        return out
    p = etrm.lookup(pack, [mdid]).get(mdid.lower())
    out["permutation"] = p
    if p is not None:
        fills = {}
        for table, fields in (("Claim", etrm.CLAIM_FIELDS), ("Measure", etrm.MEASURE_FIELDS), ("Measure", etrm.MEASURE_DESCRIPTIONS)):
            for name, col in fields.items():
                fills.setdefault(col, []).append(f"{table}.{name}")
        out["values"] = [(col, value, ", ".join(fills.get(col, []))) for col, value in sorted(p.values.items())]
    return out


class CostTestSetForm(forms.Form):
    file = forms.FileField(
        label="Set file (definition.yaml)",
        help_text="A cost-test definition set as ReCET reads it (ReCET's COST_TESTS.md); registered as a draft",
    )
    source = forms.CharField(label="Where it came from (optional)", max_length=200, required=False, help_text="e.g. ReCET 0.1.85")


@login_required
def cost_test_sets(request):
    """The cost-test definition sets: which tests runs and filings price, by program year. CPUC system
    administrators register a set file as a draft here, then record its engine check and publish it on the pack's page."""
    manager = service.can_manage(request.user, cost_tests.KIND)
    form = CostTestSetForm(request.POST or None, request.FILES or None) if manager else None
    if request.method == "POST" and manager and form.is_valid():
        f = form.cleaned_data["file"]
        if f.size > 1 << 20:
            form.add_error("file", "A set file is a few kilobytes; this one is over 1 MB.")
        else:
            try:
                pack = cost_tests.register_set(request.user, f.read(), source=form.cleaned_data["source"] or f.name)
            except (cost_tests.SetError, AccessDenied) as e:
                form.add_error("file", str(e))
            else:
                check = pack.tie_outs.filter(name="engine_check").order_by("-recorded_at", "-id").first()
                if check is None:
                    messages.success(
                        request, f"{pack.label} {pack.version} registered as a draft. Record its engine check, then publish it."
                    )
                elif check.passed:
                    messages.success(request, f"{pack.label} {pack.version} registered as a draft, and the engine check passed.")
                else:
                    messages.error(
                        request, f"{pack.label} {pack.version} registered as a draft, but the engine check failed: {check.summary}"
                    )
                return redirect("refdata:detail", pk=pack.pk)
    packs = PackVersion.objects.filter(kind=cost_tests.KIND).order_by("label", "-registered_at")
    rows = [(p, cost_tests.header(p), cost_tests.years_text(p)) for p in packs]
    in_force = cost_tests.for_year(None)
    from . import builder

    builds = list(builder.mine(request.user))
    offered = list(builder.offered()) if builder.is_manager(request.user) else None
    ctx = {"rows": rows, "form": form, "in_force": in_force, "builds": builds, "offered": offered}
    return render(request, "refdata/cost_tests.html", ctx)


# --- the acceptance checks each kind of pack needs, and recording a check's result


class CheckForm(forms.Form):
    kind = forms.ChoiceField(label="Kind of pack", choices=PackVersion.Kind.choices)
    name = forms.CharField(
        label="Check name", max_length=40, help_text="Lower-case letters, digits and underscores, e.g. source_workbook. Never reused."
    )
    description = forms.CharField(label="What it shows", widget=forms.Textarea(attrs={"rows": 2}))
    reason = forms.CharField(label="Reason for adding it", max_length=500)


@login_required
def checks(request):
    """The acceptance checks each kind of pack must pass before it is published. CPUC system administrators add a check or retire one;
    a check is never edited, and a published pack keeps the checks it was published under."""
    manager = service.can_manage_checks(request.user)
    form = CheckForm(request.POST or None) if manager else None
    if request.method == "POST" and manager and form.is_valid():
        d = form.cleaned_data
        try:
            r = service.add_requirement(request.user, kind=d["kind"], name=d["name"], description=d["description"], reason=d["reason"])
        except AccessDenied as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Check {r.words} added. Drafts of that kind need it before they are published.")
            return redirect("refdata:checks")
    rows = TieOutRequirement.objects.select_related("added_by", "retired_by").order_by("kind", "retired_at", "name")
    by_kind = []
    for code, label in PackVersion.Kind.choices:
        mine = [r for r in rows if r.kind == code]
        by_kind.append((code, label, [r for r in mine if r.active], [r for r in mine if not r.active]))
    return render(request, "refdata/checks.html", {"by_kind": by_kind, "form": form, "manager": manager})


@login_required
def retire_check(request, pk):
    r = get_object_or_404(TieOutRequirement, pk=pk)
    if request.method != "POST":
        return redirect("refdata:checks")
    try:
        service.retire_requirement(request.user, r, request.POST.get("reason", ""))
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Check {r.words} retired. Drafts no longer need it; published packs keep it with their results.")
    return redirect("refdata:checks")


class ReportForm(forms.Form):
    check = forms.ChoiceField(label="Check")
    report = forms.FileField(
        label="The check's report (JSON)",
        validators=[uploads.check_size],
        help_text=uploads.limit_text(
            'The JSON a reconciliation script or the engine\'s tie-out writes: "passed" (true or false) or "status" ("PASS" or "FAIL"), '
            "with its details.",
            False,
        ),
    )

    def __init__(self, *args, checks=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["check"].choices = [(r.name, r.words) for r in checks]


@login_required
def record_check(request, pk):
    pack = get_object_or_404(PackVersion, pk=pk)
    if request.method != "POST" or not service.can_manage(request.user, pack.kind):
        return redirect("refdata:detail", pk=pack.pk)
    form = ReportForm(request.POST, request.FILES, checks=service.required_checks(pack))
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect("refdata:detail", pk=pack.pk)
    f = form.cleaned_data["report"]
    try:
        t = service.record_report(request.user, pack, check=form.cleaned_data["check"], data=f.read(), file_name=f.name)
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        (messages.success if t.passed else messages.error)(request, f"{t.words} recorded as {'passed' if t.passed else 'failed'}.")
    return redirect("refdata:detail", pk=pack.pk)


# --- uploading an eTRM permutations download


class EtrmUploadForm(forms.Form):
    files = uploads.MultipleFileField(
        label="eTRM permutations downloads (CSV)", help_text="One or more CSV files as eTRM's permutations download writes them."
    )
    label = forms.CharField(label="Label", max_length=120, initial="eTRM published permutations")
    version = forms.CharField(label="Version", max_length=60, help_text="e.g. the download date, 2026-11-10")
    valid_from = forms.DateField(
        label="In force from (optional)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The first claim date the pack applies to, once published.",
    )
    source = forms.CharField(label="Where it came from (optional)", max_length=200, required=False)

    def clean_files(self):
        files = self.cleaned_data["files"]
        for f in files:
            if not f.name.lower().endswith((".csv", ".txt")):
                raise forms.ValidationError(f"{f.name} is not a CSV file.")
        return files


@login_required
def etrm_upload(request):
    """Load eTRM permutations downloads as a draft pack (the load_etrm_permutations command, from the page). The ACC and profile-library
    packs stay registered from storage by command: they are gigabytes and are already stored where the workers read them."""
    if not service.can_manage(request.user, etrm.KIND):
        raise Http404
    form = EtrmUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for f in d["files"]:
                path = Path(tmp) / Path(f.name).name
                with path.open("wb") as out:
                    for chunk in f.chunks():
                        out.write(chunk)
                paths.append(path)
            try:
                pack = etrm.load(
                    request.user,
                    paths,
                    label=d["label"].strip(),
                    version=d["version"].strip(),
                    valid_from=d["valid_from"],
                    source=d["source"].strip(),
                )
            except (etrm.EtrmError, AccessDenied) as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, f"{pack} loaded as a draft: {pack.notes}. Look up a permutation, then publish it.")
                return redirect("refdata:detail", pk=pack.pk)
    latest = PackVersion.objects.filter(kind=etrm.KIND).order_by("-registered_at")[:10]
    return render(request, "refdata/etrm_upload.html", {"form": form, "packs": latest})
