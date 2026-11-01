"""Release notes (BR-12.1, BR-12.5): the system owner announces each specification, rule, limit or system change before it takes effect.

A CPUC system administrator drafts a note (title, the platform release it ships in if any, a summary, the date it takes effect) and
its changes, then publishes it. Publishing needs at least one change and a date after today, and at least CEDARS_RELEASE_NOTICE_DAYS
days of notice unless a reason for the shorter notice is given (the note shows it). Publishing is audited ("release.published"), which
the notifications service delivers to everyone subscribed to release notes; a published note never changes (database trigger) and is
withdrawn with a reason, never deleted. The public pages are /releases/ (every note, coming and in effect), /limits/ (current limits
and the coming changes to them) and each guide's release (cedars/core/revisions.py)."""

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from cedars.core import revisions
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import limits
from .models import Change, ReleaseNote

AREAS = {
    "everyone": "All of CEDARS",
    "accounts": "Accounts, sign-in and access",
    "data": "Published data, dashboards and downloads",
    "programs": "Programs",
    "budgets": "Budgets and fund shifts",
    "claims": "Claims",
    "monthly": "Monthly reports",
    "calculations": "Calculations and impact profiles",
    "documents": "Documents and EM&V studies",
    "reference": "Rules, data dictionary and reference data",
    "api": "The API",
}
NOTE_FIELDS = ["title", "version", "summary", "effective_on", "short_notice_reason"]


class ReleaseError(ValueError):
    """The note cannot be saved or published as it is; the message says why."""


def notice_days() -> int:
    return settings.CEDARS_RELEASE_NOTICE_DAYS


def today() -> date:
    return timezone.localdate()


def _require_admin(user) -> None:
    if not access.is_system_admin(user):
        raise AccessDenied("Only a CPUC system administrator writes release notes.")


def _check_fields(title: str, version: str, summary: str) -> None:
    if not title.strip():
        raise ReleaseError("Give the note a title.")
    if not summary.strip():
        raise ReleaseError("Say in a few sentences what changes and why.")
    if version and not revisions.version_key(version):
        raise ReleaseError(f"{version} is not a release number like 0.3.0.")
    if version and ReleaseNote.objects.filter(version=version, withdrawn_at__isnull=True).exists():
        raise ReleaseError(f"Release {version} already has a note; add the changes to it.")


# --- drafting


@transaction.atomic
def create_draft(user, *, title: str, summary: str, effective_on: date, version: str = "") -> ReleaseNote:
    _require_admin(user)
    _check_fields(title, version.strip(), summary)
    note = ReleaseNote.objects.create(
        title=title.strip(), version=version.strip(), summary=summary.strip(), effective_on=effective_on, created_by=user
    )
    audit.record("release.drafted", obj=note, after=audit.snapshot(note, NOTE_FIELDS), actor=user)
    return note


def _draft(note: ReleaseNote) -> ReleaseNote:
    """The note as it stands in the database (locked), if it is still a draft."""
    note = ReleaseNote.objects.select_for_update().get(pk=note.pk)
    if not note.is_draft:
        raise ReleaseError("A published note does not change. Withdraw it and publish a new one.")
    return note


@transaction.atomic
def update_draft(user, note: ReleaseNote, **values) -> ReleaseNote:
    _require_admin(user)
    note = _draft(note)
    before = audit.snapshot(note, NOTE_FIELDS)
    title = values.get("title", note.title).strip()
    version = values.get("version", note.version).strip()
    summary = values.get("summary", note.summary).strip()
    if version != note.version:
        _check_fields(title, version, summary)
    else:
        _check_fields(title, "", summary)
    note.title, note.version, note.summary = title, version, summary
    note.effective_on = values.get("effective_on", note.effective_on)
    note.save()
    audit.record("release.changed", obj=note, before=before, after=audit.snapshot(note, NOTE_FIELDS), actor=user)
    return note


@transaction.atomic
def add_change(
    user, note: ReleaseNote, *, kind: str, area: str, text: str, action: str = "", limit: str = "", limit_value: str = ""
) -> Change:
    _require_admin(user)
    note = _draft(note)
    if kind not in Change.Kind.values:
        raise ReleaseError("Choose what kind of change it is.")
    if area not in AREAS:
        raise ReleaseError("Choose the part of CEDARS it changes.")
    if not text.strip():
        raise ReleaseError("Say what changes.")
    limit, limit_value = limit.strip(), limit_value.strip()
    if kind == Change.Kind.LIMIT:
        if limit not in limits.BY_KEY:
            raise ReleaseError("Choose the limit that changes.")
        if not limit_value:
            raise ReleaseError("Give the limit's new value, as people will see it (e.g. 200 MB).")
    else:
        limit, limit_value = "", ""
    last = note.changes.order_by("-position").first()
    change = Change.objects.create(
        note=note,
        kind=kind,
        area=area,
        text=text.strip(),
        action=action.strip(),
        limit=limit,
        limit_value=limit_value,
        position=(last.position + 1) if last else 1,
    )
    audit.record(
        "release.change_added",
        obj=note,
        after={"kind": kind, "area": area, "text": change.text, "limit": limit, "limit_value": limit_value},
        actor=user,
    )
    return change


@transaction.atomic
def remove_change(user, change: Change) -> None:
    _require_admin(user)
    _draft(change.note)
    audit.record("release.change_removed", obj=change.note, before={"kind": change.kind, "text": change.text}, actor=user)
    change.delete()


@transaction.atomic
def discard_draft(user, note: ReleaseNote) -> None:
    _require_admin(user)
    note = _draft(note)
    audit.record("release.discarded", obj=note, before=audit.snapshot(note, NOTE_FIELDS), actor=user)
    note.delete()


# --- publishing


def notice_problem(note: ReleaseNote, on: date | None = None) -> str:
    """Why the note cannot be published on `on` as it stands ("" = it can). A short notice is a problem only without a reason."""
    on = on or today()
    if not note.changes.exists():
        return "List at least one change."
    if note.effective_on <= on:
        return "A note is published before its changes take effect: choose a date after today."
    days = (note.effective_on - on).days
    if days < notice_days() and not note.short_notice_reason.strip():
        return (
            f"It takes effect in {days} day{'s' if days != 1 else ''}, less than the usual {notice_days()} days' notice: say why it "
            "cannot wait."
        )
    return ""


@transaction.atomic
def publish(user, note: ReleaseNote, *, short_notice_reason: str = "") -> ReleaseNote:
    _require_admin(user)
    note = _draft(note)
    if short_notice_reason.strip():
        note.short_notice_reason = short_notice_reason.strip()
    problem = notice_problem(note)
    if problem:
        raise ReleaseError(problem)
    if (note.effective_on - today()).days >= notice_days():
        note.short_notice_reason = ""  # the full notice: nothing to explain
    note.published_at, note.published_by = timezone.now(), user
    note.save()
    audit.record(
        "release.published",
        obj=note,
        after={
            **audit.snapshot(note, NOTE_FIELDS),
            "changes": [
                {"kind": c.kind, "area": c.area, "text": c.text, "limit": c.limit, "limit_value": c.limit_value} for c in note.changes.all()
            ],
        },
        reason=note.short_notice_reason,
        actor=user,
    )
    return note


@transaction.atomic
def withdraw(user, note: ReleaseNote, reason: str) -> ReleaseNote:
    _require_admin(user)
    note = ReleaseNote.objects.select_for_update().get(pk=note.pk)
    if note.is_draft:
        raise ReleaseError("A draft is discarded, not withdrawn.")
    if note.is_withdrawn:
        raise ReleaseError("This note is already withdrawn.")
    if not reason.strip():
        raise ReleaseError("Say why it is withdrawn; the note shows it.")
    note.withdrawn_at, note.withdrawn_by, note.withdrawn_reason = timezone.now(), user, reason.strip()
    note.save(update_fields=["withdrawn_at", "withdrawn_by", "withdrawn_reason"])
    audit.record("release.withdrawn", obj=note, reason=note.withdrawn_reason, actor=user)
    return note


# --- reading


def _with_changes(qs):
    return qs.prefetch_related(Prefetch("changes", queryset=Change.objects.order_by("position", "pk")))


def published():
    """Every published note, withdrawn ones included (they stay, marked), newest date first."""
    return _with_changes(ReleaseNote.objects.filter(published_at__isnull=False))


def drafts():
    return _with_changes(ReleaseNote.objects.filter(published_at__isnull=True))


def status(note: ReleaseNote, on: date | None = None) -> str:
    """draft | withdrawn | coming | in effect."""
    on = on or today()
    if note.is_draft:
        return "draft"
    if note.is_withdrawn:
        return "withdrawn"
    return "coming" if note.effective_on > on else "in effect"


def coming(on: date | None = None, within: int | None = None):
    """Published notes still to take effect (soonest first); `within` days only, when given."""
    on = on or today()
    qs = published().filter(withdrawn_at__isnull=True, effective_on__gt=on)
    if within is not None:
        qs = qs.filter(effective_on__lte=on + timedelta(days=within))
    return qs.order_by("effective_on", "pk")


def banner(on: date | None = None) -> list[ReleaseNote]:
    """The notes every page announces: published, not withdrawn, taking effect within the notice period (one query: every page runs it)."""
    on = on or today()
    return list(
        ReleaseNote.objects.filter(
            published_at__isnull=False,
            withdrawn_at__isnull=True,
            effective_on__gt=on,
            effective_on__lte=on + timedelta(days=notice_days()),
        ).order_by("effective_on", "pk")
    )


def coming_limits(on: date | None = None) -> dict[str, list[tuple[ReleaseNote, Change]]]:
    """{limit key: [(note, change)]}: the announced changes to each limit still to take effect, soonest first."""
    out: dict[str, list] = {}
    for note in coming(on):
        for c in note.changes.all():
            if c.kind == Change.Kind.LIMIT and c.limit:
                out.setdefault(c.limit, []).append((note, c))
    return out


def running() -> str:
    return settings.VERSION


def note_for_release(version: str) -> ReleaseNote | None:
    return published().filter(version=version, withdrawn_at__isnull=True).first()


@dataclass
class GuideRevision:
    slug: str
    title: str
    release: str  # the release it was last revised for
    behind: list[str] = field(default_factory=list)  # releases in effect that changed its part of CEDARS after it was revised


def guide_revisions(on: date | None = None) -> list[GuideRevision]:
    """Each user guide's release and the releases in effect that changed its part of CEDARS since (BR-12.5's check)."""
    from cedars.core.help import GUIDES

    in_effect = list(published().filter(withdrawn_at__isnull=True, effective_on__lte=on or today()).exclude(version=""))
    out = []
    for g in GUIDES:
        release = revisions.revised(f"guide:{g.slug}")
        covers = set(g.areas) | {"everyone"} if g.areas else set()  # a guide about no one part (accessibility) is never behind
        behind = []
        for n in in_effect:
            if not revisions.version_key(release) < revisions.version_key(n.version) <= revisions.version_key(running()):
                continue  # revised for it already, or announced for a release this site does not run yet
            if {c.area for c in n.changes.all()} & covers:
                behind.append(n.version)
        out.append(GuideRevision(g.slug, g.title, release, sorted(set(behind), key=revisions.version_key)))
    return out


def limit_mismatches(on: date | None = None) -> list[tuple[ReleaseNote, Change, str]]:
    """Limit changes in effect whose announced value is not what the site enforces now: (note, change, current value). The newest
    announcement for each limit is the one that counts."""
    on = on or today()
    latest: dict[str, tuple[ReleaseNote, Change]] = {}
    for note in published().filter(withdrawn_at__isnull=True, effective_on__lte=on).order_by("effective_on", "pk"):
        for c in note.changes.all():
            if c.kind == Change.Kind.LIMIT and c.limit in limits.BY_KEY:
                latest[c.limit] = (note, c)
    out = []
    for key, (note, c) in latest.items():
        now = limits.current(key)
        if _norm(now) != _norm(c.limit_value):
            out.append((note, c, now))
    return out


def _norm(text: str) -> str:
    return "".join(text.lower().replace(",", "").split())
