"""The publication scan's work in each user's queue (cedars.core.queue, BR-09.5; navigation review D7): held publications, for the
filing organization's confirmers and the CPUC system administrators."""

from __future__ import annotations

from cedars.core.queue import Item

from . import service


def holds(user) -> list[Item]:
    return [
        Item(
            f"Publication held: {service.label(scan.snapshot.version)}",
            f"/publication/holds/{scan.snapshot.pk}/",
            scan.snapshot.organisation.code,
            scan.scanned_at,
            None,
            service.summary(scan).splitlines()[0] if service.summary(scan) else "",
        )
        for scan in service.held_for(user)
    ]
