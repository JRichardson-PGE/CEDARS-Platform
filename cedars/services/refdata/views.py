from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from cedars.services.accounts.service import AccessDenied

from . import cost_tests, etrm, service
from .models import PackVersion, TieOutRequirement


@login_required
def index(request):
    """Every data pack, by kind: what runs can use now, drafts, and withdrawn packs (BR-11.7)."""
    kind = request.GET.get("kind", "")
    packs = PackVersion.objects.all()
    if kind in PackVersion.Kind.values:
        packs = packs.filter(kind=kind)
    in_force = [(label, service.in_force(code)) for code, label in PackVersion.Kind.choices]
    return render(
        request,
        "refdata/index.html",
        {"packs": packs, "kinds": PackVersion.Kind.choices, "kind": kind, "in_force": [(label, p) for label, p in in_force if p]},
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
    requirements = [(r, latest.get(r.name)) for r in TieOutRequirement.objects.filter(kind=pack.kind).order_by("name")]
    ctx = {}
    if pack.kind == etrm.KIND:
        ctx = _permutation(pack, request.GET.get("id", "").strip())
    if pack.kind == cost_tests.KIND:
        ctx = {"cost_set": cost_tests.header(pack), "cost_years": cost_tests.years_text(pack), "cost_sha": cost_tests.sha256(pack)}
    return render(
        request,
        "refdata/detail.html",
        {
            "pack": pack,
            "manager": manager,
            "files": sorted(pack.manifest.items())[:200],
            "requirements": requirements,
            "outstanding": service.outstanding(pack),
            "tie_outs": pack.tie_outs.select_related("recorded_by"),
            **ctx,
        },
    )


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
    """The cost-test definition sets (BR-06.8, BR-06.14, D14): which tests runs and filings price, by program year. CPUC system
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
                messages.success(request, f"{pack.label} {pack.version} registered as a draft. Record its engine check, then publish it.")
                return redirect("refdata:detail", pk=pack.pk)
    packs = PackVersion.objects.filter(kind=cost_tests.KIND).order_by("label", "-registered_at")
    rows = [(p, cost_tests.header(p), cost_tests.years_text(p)) for p in packs]
    in_force = cost_tests.for_year(None)
    return render(request, "refdata/cost_tests.html", {"rows": rows, "form": form, "in_force": in_force})
