"""The reference data service's parts of Your work (core/yourwork.py, UI review 3, U5), on the Reference data page: the cost-test sets
the person is building, and the draft data packs they registered and have not yet published."""

from __future__ import annotations

from django.urls import reverse

from cedars.core.yourwork import SHOWN, Line, Part, day

from . import builder
from .models import CostTestBuild, PackVersion


def cost_test_sets(request, page):
    mine = builder.mine(request.user).filter(owner=request.user).exclude(status=CostTestBuild.Status.REGISTERED)
    shown = list(mine[:SHOWN])
    if not shown:
        return None
    lines = []
    for b in shown:
        problems = b.open_problems
        detail = f"changed {day(b.updated_at).isoformat()}"
        if problems:
            detail += f" · {problems} problem{'s' if problems != 1 else ''} to fix"
        name = f"{b}{f': {b.label}' if b.label else ''}"
        lines.append(Line(name, reverse("refdata:build_step", args=[b.pk, "review"]), "Continue", b.get_status_display(), detail))
    return Part(
        "cost_test_builds",
        "Cost-test sets you are building",
        lines,
        total=mine.count(),
        more_url=reverse("refdata:cost_tests"),
        more_label="All cost-test sets",
    )


def draft_packs(request, page):
    mine = PackVersion.objects.filter(registered_by=request.user, status=PackVersion.Status.DRAFT).order_by("-registered_at")
    shown = list(mine[:SHOWN])
    if not shown:
        return None
    lines = [
        Line(
            f"{p.label}{f' {p.version}' if p.version else ''}",
            reverse("refdata:detail", args=[p.pk]),
            "Review",
            "Draft",
            f"{p.get_kind_display()} · registered {day(p.registered_at).isoformat()}",
        )
        for p in shown
    ]
    return Part(
        "draft_packs",
        "Data packs you registered, not yet published",
        lines,
        total=mine.count(),
        more_url=reverse("refdata:index"),
        more_label="All data packs",
    )
