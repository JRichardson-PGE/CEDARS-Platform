from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from cedars.services.accounts.service import AccessDenied

from . import etrm, service
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
