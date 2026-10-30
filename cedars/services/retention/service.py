"""Retention (decision M): a period per record class, legal holds, and a daily job that deletes what has expired. Services and
modules that own a deletable class register a purger; classes kept permanently have none.

A purger is `find(cutoff) -> iterable of (object_ref, delete)`: the records whose period started before `cutoff`, each with a
callable that deletes it. The job skips held records and writes one audit event per class."""

from collections.abc import Callable, Iterable
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import LegalHold, PurgeRun, RetentionPolicy

Purger = Callable[[object], Iterable[tuple[str, Callable[[], None]]]]
_purgers: dict[str, Purger] = {}


def register_purger(record_class: str, find: Purger) -> None:
    _purgers[record_class] = find


def purgers() -> dict[str, Purger]:
    return dict(_purgers)


def _require_admin(actor):
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator changes retention periods and legal holds.")


@transaction.atomic
def set_period(actor, policy: RetentionPolicy, keep_days: int | None, reason: str) -> RetentionPolicy:
    _require_admin(actor)
    if not reason.strip():
        raise AccessDenied("Give the reason for changing a retention period (for example the records schedule it follows).")
    policy = RetentionPolicy.objects.select_for_update().get(pk=policy.pk)
    before = audit.snapshot(policy, ["keep_days"])
    policy.keep_days = keep_days
    policy.save()
    audit.record(
        "retention.period_changed", obj=policy, before=before, after=audit.snapshot(policy, ["keep_days"]), reason=reason, actor=actor
    )
    return policy


@transaction.atomic
def place_hold(actor, record_class: str, reason: str, object_ref: str = "") -> LegalHold:
    _require_admin(actor)
    if not RetentionPolicy.objects.filter(record_class=record_class).exists():
        raise AccessDenied(f"{record_class} is not a record class.")
    if not reason.strip():
        raise AccessDenied("Give the reason for the hold (the matter or request it serves).")
    hold = LegalHold.objects.create(record_class=record_class, object_ref=object_ref.strip(), reason=reason, placed_by=actor)
    audit.record(
        "retention.hold_placed", obj=hold, after={"record_class": record_class, "object_ref": hold.object_ref}, reason=reason, actor=actor
    )
    return hold


@transaction.atomic
def release_hold(actor, hold: LegalHold, reason: str) -> LegalHold:
    _require_admin(actor)
    hold = LegalHold.objects.select_for_update().get(pk=hold.pk)
    if hold.released_at:
        raise AccessDenied("That hold is already released.")
    if not reason.strip():
        raise AccessDenied("Give the reason for releasing the hold.")
    hold.released_by, hold.released_at, hold.release_reason = actor, timezone.now(), reason
    hold.save()
    audit.record("retention.hold_released", obj=hold, reason=reason, actor=actor)
    return hold


def held(record_class: str, object_ref: str) -> bool:
    active = LegalHold.objects.filter(record_class=record_class, released_at__isnull=True)
    return active.filter(object_ref="").exists() or active.filter(object_ref=str(object_ref)).exists()


def purge(*, dry_run: bool = False, now=None) -> PurgeRun:
    """Delete every expired record of every class with a period and a purger, except held ones. Each record is deleted in its own
    transaction, so one failure does not undo the rest."""
    now = now or timezone.now()
    counts = {}
    for policy in RetentionPolicy.objects.filter(keep_days__isnull=False):
        find = _purgers.get(policy.record_class)
        if find is None:
            continue
        cutoff = now - timedelta(days=policy.keep_days)
        c = {"deleted": 0, "held": 0, "failed": 0}
        whole_class_held = LegalHold.objects.filter(record_class=policy.record_class, object_ref="", released_at__isnull=True).exists()
        for ref, delete in find(cutoff):
            if (
                whole_class_held
                or LegalHold.objects.filter(record_class=policy.record_class, object_ref=str(ref), released_at__isnull=True).exists()
            ):
                c["held"] += 1
                continue
            if not dry_run:
                try:
                    with transaction.atomic():
                        delete()
                except Exception:
                    c["failed"] += 1
                    continue
            c["deleted"] += 1
        counts[policy.record_class] = c
        if not dry_run and (c["deleted"] or c["failed"]):
            audit.record(
                "retention.purged",
                object_type="retention.retentionpolicy",
                object_id=str(policy.pk),
                object_repr=policy.label,
                after={**c, "cutoff": cutoff.isoformat()},
            )
    return PurgeRun.objects.create(dry_run=dry_run, counts=counts)
