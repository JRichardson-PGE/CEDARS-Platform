"""The program registry's work in each user's queue (cedars.core.queue, BR-09.5): changes to locked program attributes waiting on a CPUC
programs approver or their delegate."""

from __future__ import annotations

from cedars.core.queue import Item

from . import service
from .models import ProgramChangeRequest


def change_requests(user) -> list[Item]:
    if user is None or not user.is_authenticated:
        return []
    approver = service.approver(user)
    if approver is None:
        return []
    delegate = "" if approver == user else f" You decide as {approver.name or approver.email}'s delegate."
    return [
        Item(
            f"{r.program.prg_id}: change to " + ", ".join(sorted(r.changes or {})),
            f"/programs/requests/{r.pk}/",
            r.organisation.code if r.organisation else "",
            r.requested_at,
            None,
            f"From {r.effective:%Y-%m-%d}. Reason: {r.reason}{delegate}",
        )
        for r in ProgramChangeRequest.objects.filter(status=ProgramChangeRequest.Status.PENDING).select_related("program", "organisation")
    ]
