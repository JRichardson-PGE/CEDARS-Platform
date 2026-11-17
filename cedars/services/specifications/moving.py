"""Which specification version a filing set uses: chosen when it is set up, and moved to a later version while it is open.

A filing set (a claim year, a budget filing, a year of monthly reports) is set up on the version a CPUC system administrator chooses
from the published versions in effect (`choices`; the newest is offered), and every filing in it carries that version for all its
uploads, checks and corrections, on any date. The set-up checks that the version fits the set's pricing (`setup_problems`): the engine
release it names must offer the ACC the set is priced on.

A mistake in a version that is in use is fixed by publishing the next version and moving the filing set to it (`move`), with a reason.
Before the move is confirmed, the impact test shows how the set's confirmed filings would fare on the new version (`preview`). The move
re-pins every filing in the set, checks every open version again against the new version (its findings then current; a calculation it
had is void, since the version names its own engine release and cost-test set), and leaves confirmed versions as they are, still
recorded against the version they were checked with. Filers hear of it through the filing calendar's notifications.

Each filing module registers how its open versions are checked again and which ACCs a filing set is priced on
(`register_filing_set`)."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from cedars.services.accounts.service import AccessDenied

from . import impact, service


@dataclass(frozen=True)
class FilingSetHooks:
    """How a module's filing sets are moved: `recheck(v)` checks open submission version `v` again against its deadline's version (as
    CEDARS, not a filer); `accs(deadline)` gives the ACCs the filing set of `deadline` is priced on."""

    recheck: object
    accs: object


_hooks: dict[str, FilingSetHooks] = {}


def register_filing_set(module: str, *, recheck, accs) -> None:
    """Called by a filing module in its AppConfig.ready(): how its open versions are checked again after their filing set moves to
    another specification version, and the ACCs a filing set is priced on (checked against the version's engine release)."""
    _hooks[module] = FilingSetHooks(recheck, accs)


# --- choosing a version


def choices(module: str) -> list:
    """The versions a filing set of `module` may be set up with: the published versions of its specification in effect, newest first
    (the first is the current one, offered by default)."""
    _, Version = service._models()
    qs = Version.objects.filter(specification__module=module, status=Version.Status.PUBLISHED).select_related("specification")
    return list(service._in_effect(qs).order_by("-published_at", "-pk"))


def _release_accs(version) -> tuple[str, list[str] | None]:
    """(the release that prices filings on `version`, the ACCs it offers; None when the platform cannot tell)."""
    from .editor import engine_releases

    releases = engine_releases()
    if not releases:
        return "", None
    name = service.engine_release_of(version)
    rec = next((r for r in releases if r["release"] == name), None) if name else next((r for r in releases if r.get("default")), None)
    if rec is None:
        return name, None
    return rec["release"], [str(a) for a in rec.get("accs") or []]


def setup_problems(version, module: str, accs=()) -> list[str]:
    """Why a filing set of `module` priced on `accs` cannot use `version` ([] = it can): a published version of the module's
    specification in effect, whose engine release offers each ACC."""
    if version is None:
        return []
    out = []
    if version.specification.module != module:
        return [f"{version} is not a version of the {module} specification."]
    if version.status != version.Status.PUBLISHED:
        out.append(f"{version} is not published: a filing set uses a published version.")
    elif version not in choices(module):
        when = f"takes effect on {version.effective_on:%Y-%m-%d}" if version.effective_on else "is not in effect"
        out.append(f"{version} {when}: a filing set can use it from then.")
    release, offered = _release_accs(version)
    if offered is not None:
        for acc in accs:
            if acc and str(acc) not in offered:
                out.append(
                    f"ReCET {release}, the engine release {version.name} names, does not have the {acc} ACC the filings are priced on: "
                    "choose another version or ACC, or add the ACC to the release."
                )
    return out


# --- moving a filing set


def accs_of(deadlines) -> list[str]:
    out = []
    for d in deadlines:
        hooks = _hooks.get(d.module)
        for acc in (hooks.accs(d) if hooks else []) or []:
            if acc and acc not in out:
                out.append(acc)
    return out


def current_of(deadlines) -> list:
    """The versions the filing set's filings are pinned to now (one, unless a filing was pinned on its own)."""
    out = []
    for d in deadlines:
        v = d.specification_version
        if v is not None and v not in out:
            out.append(v)
    return out


def later(deadlines) -> list:
    """The versions a filing set may move to: those it could be set up with that were published after every version its filings are
    pinned to now."""
    deadlines = list(deadlines)
    if not deadlines:
        return []
    pinned = current_of(deadlines)
    newest = max((v.published_at for v in pinned if v.published_at), default=None)
    return [v for v in choices(deadlines[0].module) if v not in pinned and (newest is None or v.published_at > newest)]


def confirmed(deadlines) -> list:
    """For each organization, the latest confirmed version among the filing set's filings: what the move's impact test checks."""
    from cedars.services.submissions.models import SubmissionVersion

    published = (
        SubmissionVersion.objects.filter(
            status=SubmissionVersion.Status.PUBLISHED,
            origin=SubmissionVersion.Origin.FILED,
            submission__deadline__in=list(deadlines),
        )
        .select_related("submission__deadline__period", "submission__deadline__cycle", "submission__organisation", "specification_version")
        .order_by("submission__organisation__code", "-submission__deadline__due_at", "-submission__deadline__pk", "-number")
    )
    out, seen = [], set()
    for v in published:
        if v.submission.organisation_id not in seen:
            seen.add(v.submission.organisation_id)
            out.append(v)
    return out


def preview(version, deadlines) -> dict:
    """The impact test of moving the filing set to `version`: its confirmed filings under the version they were checked with and under
    `version`. Nothing changes."""
    return impact.report_for(version, confirmed(deadlines))


@transaction.atomic
def move(actor, deadlines, version, reason: str) -> dict:
    """Move a filing set to a later published version: every filing in it is pinned to `version`, every open version is checked again
    against it (a calculation it had is void) and confirmed versions keep their record. Returns {moved, rechecked, notes}."""
    from cedars.services.submissions import service as submissions

    deadlines = list(deadlines)
    if not deadlines:
        raise AccessDenied("The filing set has no filings to move.")
    if version not in later(deadlines):
        raise AccessDenied(f"{version} is not a later published version in effect that the filing set can move to.")
    problems = setup_problems(version, deadlines[0].module, accs_of(deadlines))
    if problems:
        raise AccessDenied(problems[0])
    open_versions = submissions.move_specification(actor, deadlines, version, reason)
    hooks = _hooks.get(deadlines[0].module)
    rechecked, notes = 0, []
    for v in open_versions:
        if hooks is None:
            break
        try:
            with transaction.atomic():
                hooks.recheck(v)
            rechecked += 1
        except Exception as e:  # noqa: BLE001 - one filing that cannot be checked again is said, and the others still are
            notes.append(f"{v.submission.organisation.code}'s {v.submission.deadline.title} version {v.number} was not checked again: {e}")
    return {"moved": len(deadlines), "rechecked": rechecked, "notes": notes}
