"""The Calculations module's parts of Your work (core/yourwork.py, UI review 3, U5): the person's last runs, on the front page and the
Calculations page, and the Edit inputs drafts they worked on last, on the Calculations page."""

from __future__ import annotations

from django.urls import reverse

from cedars.core.yourwork import FRONT, SHOWN, Line, Part, day

from . import drafts, service


def runs(request, page):
    mine = service.visible_runs(request.user).filter(owner=request.user)
    shown = list(mine[:SHOWN])
    if not shown and page == FRONT:  # the front page shows the part only to someone who has started runs
        return None
    lines = []
    for r in shown:
        when = r.finished_at or r.started_at or r.created_at
        what = "finished" if r.ended else "started" if r.started_at else "asked for"
        who = r.organisation.code if r.organisation else "your own"
        finished = r.status == r.Status.SUCCEEDED
        lines.append(
            Line(
                r.name,
                reverse("calculations:run", args=[r.pk]),
                "Results" if finished else "",
                r.get_status_display(),
                f"{who} · {what} {day(when).isoformat()}",
                action_url=reverse("calculations:results", args=[r.pk]) if finished else "",
            )
        )
    can_start = service.default_release() is not None
    return Part(
        "runs",
        "Your recent runs",
        lines,
        total=mine.count() if shown else 0,
        more_url=reverse("calculations:runs"),
        more_label="All runs",
        empty="You have not started a run yet." + (" New run prices a job on the ReCET engine." if can_start else ""),
    )


def edit_inputs(request, page):
    mine = drafts.visible(request.user).filter(owner=request.user)
    shown = list(mine[:SHOWN])
    if not shown:
        return None
    lines = [
        Line(
            d.name,
            reverse("calculations:draft", args=[d.pk]),
            "Continue",
            "",
            f"{d.measure_count} measure{'s' if d.measure_count != 1 else ''} · changed {day(d.updated_at).isoformat()}",
        )
        for d in shown
    ]
    return Part(
        "drafts", "Your Edit inputs drafts", lines, total=mine.count(), more_url=reverse("calculations:drafts"), more_label="All drafts"
    )
