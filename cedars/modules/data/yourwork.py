"""The Data module's parts of Your work (core/yourwork.py, UI review 3, U5), on the Published data page: the person's saved views and
their record downloads."""

from __future__ import annotations

from django.urls import reverse

from cedars.core.yourwork import SHOWN, Line, Part, day

from . import records, saved
from .models import DownloadJob, SavedView


def saved_views(request, page):
    mine = SavedView.objects.filter(owner=request.user).order_by("-updated_at", "-pk")
    shown = list(mine[:SHOWN])
    if not shown:
        return None
    lines = []
    for v in shown:
        title = saved.pages().get(v.page, v.page)
        status = "Published" if v.published else ""
        lines.append(Line(v.name, saved.url(v), "", status, f"{title} · saved {day(v.updated_at).isoformat()}"))
    return Part("saved_views", "Your saved views", lines, total=mine.count(), more_url=reverse("data:views"), more_label="All saved views")


def downloads(request, page):
    mine = DownloadJob.objects.filter(user=request.user)
    shown = list(mine[:SHOWN])
    if not shown:
        return None
    lines = []
    for j in shown:
        ready = j.status == DownloadJob.Status.DONE
        lines.append(
            Line(
                f"{records.DATASETS.get(j.dataset, j.dataset)} records, {j.fmt.upper()} ({j.records:,})",
                reverse("data:download_job", args=[j.pk]),
                "Download" if ready else "",
                j.get_status_display(),
                f"asked for {day(j.created_at).isoformat()}",
                action_url=reverse("data:download_file", args=[j.pk]) if ready else "",
            )
        )
    return Part(
        "downloads", "Your downloads", lines, total=mine.count(), more_url=reverse("data:downloads"), more_label="All your downloads"
    )
