"""Publication snapshots. The submissions service tells this one when a version is published or un-confirmed (`register_listener`); the
work runs once the confirming transaction has committed, so copying a large results file never holds the version's lock, and a failure
leaves the version published with no snapshot (`manage.py snapshot_published` makes the missing ones).

What is in force (`rebuild`): the snapshots of each submission's standing published version (`submissions.current`), one per slot:
  forecast  every filing (application, TUAL, MCAL ...) is its own slot; the query layer picks the filing a page asks for
  claims    one slot per PA and claim year: each quarter's results are compiled year to date, so only the latest period stands
            (the annual claim once confirmed), never two quarters added together
  monthly   likewise, one per PA and report year: the latest month's year-to-date report
A release is made only when that set changes.

Imported (legacy) versions run no publisher hook and record no run: their loads build a measure-level file from the tables they stored
and call `publish` with its storage area and key (`legacy.publish_snapshot`), with `release=False`: a load of many PAs makes one release.

The scan for personal information (BR-08.5, `pii.py`): every new snapshot's text columns are searched before it can join a release.
A hit (or a file that cannot be read) holds it: its `PIIScan` says `held`, `in_force` leaves it out (the dataset keeps what was
published before: an earlier version of the same filing, or for claims and monthly the PA's previous period), and the data owner (the
filing organization's members who confirm in that module, and whoever confirmed it) and the CPUC system administrators are told. A
system administrator releases it with a reason (`release_hold`), or runs the scan again after changing the settings (`rescan`);
a corrected version is scanned in its own right. Every step is audited."""

import logging
import tempfile
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from . import pii
from .models import Dataset, PIIScan, Release, ScanSettings, Snapshot

log = logging.getLogger(__name__)

# The module that files each dataset (module names, not imports: a service never imports a module).
MODULE_DATASETS = {"claims": Dataset.CLAIMS, "budgets": Dataset.FORECAST, "monthly": Dataset.MONTHLY}
YEAR_SLOTS = {Dataset.CLAIMS, Dataset.MONTHLY}
AREA = "snapshots"


def dataset_of(v: SubmissionVersion) -> str | None:
    return MODULE_DATASETS.get(v.submission.module)


def source_key(v: SubmissionVersion) -> str | None:
    """The run's measure-level parquet, as the Calculations module recorded it on the version."""
    return ((v.calculation or {}).get("results") or {}).get("measure_level")


def label(v: SubmissionVersion) -> str:
    d = v.submission.deadline
    period = d.period or d.cycle or ""
    return f"{v.submission.organisation.code} {period} {d.kind} version {v.number}".replace("  ", " ")


# --- hooks (registered in apps.py)


def on_published(v: SubmissionVersion) -> None:
    if dataset_of(v) and source_key(v):
        transaction.on_commit(lambda: _safely(publish, v.pk))


def on_unpublished(v: SubmissionVersion) -> None:
    if dataset_of(v):
        transaction.on_commit(lambda: _safely(withdraw, v.pk))


def _safely(fn, pk) -> None:
    try:
        fn(pk)
    except Exception as e:  # the version stays published; snapshot_published retries
        log.exception("snapshot %s failed for version %s", fn.__name__, pk)
        audit.record("snapshot.failed", object_type="submissions.submissionversion", object_id=str(pk), reason=f"{fn.__name__}: {e}"[:500])


# --- writing


def publish(version_id: int, *, area: str = "results", key: str = "", release: bool = True) -> Snapshot | None:
    """Freeze a published version's measure-level results and release them. Idempotent: a version that already has its snapshot only
    has the release checked. None when the version is not published or has no measure-level results. `area` and `key` name the file
    when the version records no run (an imported version); `release=False` leaves the release to the caller's `rebuild`."""
    v = SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation").get(pk=version_id)
    dataset, key = dataset_of(v), key or source_key(v)
    if v.status != SubmissionVersion.Status.PUBLISHED or not dataset or not key:
        return None
    snap = Snapshot.objects.filter(version=v).first()
    if snap is None:
        snap = _write(v, dataset, key, area)
    if release:
        rebuild(dataset, f"{label(v)} published")
    return snap


def _write(v: SubmissionVersion, dataset: str, key: str, area: str = "results") -> Snapshot:
    import pyarrow.parquet as pq

    snap = Snapshot(dataset=dataset, version=v, organisation=v.submission.organisation, run=str((v.calculation or {}).get("run", "")))
    snap.year = v.submission.deadline.period.year if v.submission.deadline.period_id else None
    snap.source_key = key
    snap.key = f"{dataset}/{snap.id}/results.parquet"
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "results.parquet"
        with storage.store(area).open(key) as src, open(local, "wb") as dst:
            while block := src.read(1 << 22):
                dst.write(block)
        snap.rows = pq.ParquetFile(local).metadata.num_rows
        snap.sha256 = storage.sha256_of(local)
        snap.bytes = storage.store(AREA).put_file(snap.key, local).size
        result = _run_scan(local)
    with transaction.atomic():  # a snapshot never exists without its scan (one without would stand, as the ones made before it)
        snap.save()
        scan = _record_scan(snap, result)
        audit.record(
            "snapshot.created",
            obj=snap,
            after={
                **audit.snapshot(snap, ["dataset", "version", "organisation", "year", "run", "key", "sha256", "rows"]),
                "pii_scan": scan.outcome,
            },
            organisation=v.submission.organisation,
        )
    if scan.held:
        _hold(snap, scan)
    return snap


# --- the scan for personal information (BR-08.5)


def scan_settings() -> ScanSettings:
    return ScanSettings.objects.get_or_create(pk=1, defaults={"patterns": list(pii.BY_CODE), "exempt_columns": list(pii.DEFAULT_EXEMPT)})[0]


def _run_scan(path) -> pii.Result:
    cfg = scan_settings()
    try:
        return pii.scan(path, patterns=cfg.patterns, exempt=cfg.exempt_columns)
    except Exception as e:  # an unreadable file is held, never published unscanned
        log.exception("publication scan failed for %s", path)
        return pii.Result([], 0, [], [], list(cfg.patterns), error=f"{type(e).__name__}: {e}"[:500])


def _record_scan(snap: Snapshot, result: pii.Result) -> PIIScan:
    fields = {
        "outcome": PIIScan.Outcome.CLEAR if result.clear else PIIScan.Outcome.HELD,
        "findings": [f.as_dict() for f in result.findings],
        "error": result.error,
        "columns": result.columns,
        "skipped": result.skipped,
        "patterns": result.patterns,
        "scanned_at": timezone.now(),
    }
    scan, _ = PIIScan.objects.update_or_create(snapshot=snap, defaults=fields)
    return scan


def held(snap: Snapshot) -> bool:
    """Whether the publication scan holds the snapshot (one made before the scan existed has no scan and is not held)."""
    try:
        return snap.pii_scan.held
    except PIIScan.DoesNotExist:
        return False


def module_of(snap: Snapshot) -> str:
    return snap.version.submission.module


def owners(snap: Snapshot):
    """Who is told of a hold: the filing organization's members who confirm in its module, whoever confirmed the version, and the
    CPUC system administrators (who release holds)."""
    from cedars.services.accounts import permissions as perms

    people = {u.pk: u for u in access.holders(snap.organisation, f"{module_of(snap)}.confirm")}
    if snap.version.confirmed_by_id and snap.version.confirmed_by.is_active:
        people.setdefault(snap.version.confirmed_by_id, snap.version.confirmed_by)
    cpuc = access.cpuc()
    if cpuc is not None:
        for u in access.holders(cpuc, perms.SYSTEM_ADMIN):
            people.setdefault(u.pk, u)
    return list(people.values())


def summary(scan: PIIScan) -> str:
    """One line per finding, with counts and no values (it goes into email)."""
    if scan.error:
        return f"The file could not be scanned: {scan.error}"
    lines = []
    for f in scan.findings:
        label = pii.BY_CODE[f["pattern"]].label if f["pattern"] in pii.BY_CODE else f["pattern"]
        n = f["records"]
        lines.append(f"{f['column']}: {n:,} record{'' if n == 1 else 's'} with what look like {label}")
    return "\n".join(lines)


def _hold(snap: Snapshot, scan: PIIScan) -> None:
    from cedars.services.notifications import service as notifications

    audit.record(
        "snapshot.held",
        obj=snap,
        after={"findings": [{k: f[k] for k in ("column", "pattern", "records")} for f in scan.findings], "error": scan.error},
        reason="The publication scan found what may be personal information" if not scan.error else "The publication scan failed",
        organisation=snap.organisation,
        actor=None,
    )
    title = f"Publication held: {label(snap.version)}"
    body = "\n".join(
        [
            f"{label(snap.version)} is confirmed, but its records are not on the public dashboards and downloads yet: the publication "
            "scan found what may be personal information.",
            summary(scan),
            "The dashboards and downloads keep showing what was published before. Correct the filing (un-confirm it before the due "
            "date, or request a correction), or, if these are not personal information, ask a CPUC system administrator to release it.",
        ]
    )
    told = 0
    for user in owners(snap):
        told += notifications.notify(user, title, body, f"/publication/holds/{snap.pk}/", key=f"publication-held:{snap.pk}") is not None
    scan.alerted = told
    scan.save(update_fields=["alerted"])


def _require_admin(actor, what: str) -> None:
    if actor is not None and not access.is_system_admin(actor):
        raise access.AccessDenied(f"Only a CPUC system administrator {what}.")


@transaction.atomic
def release_hold(actor, snap: Snapshot, reason: str) -> PIIScan:
    """Release a held snapshot to the public release, with the reason (e.g. "the 'numbers' are measure codes"). Audited; the data
    owner is told."""
    from cedars.services.notifications import service as notifications

    _require_admin(actor, "releases a held publication")
    if not reason.strip():
        raise access.AccessDenied("Give the reason for releasing it: why what the scan found is not personal information.")
    scan = PIIScan.objects.select_for_update().get(snapshot=snap)
    if not scan.held:
        raise access.AccessDenied("That publication is not held.")
    scan.outcome, scan.released_by, scan.released_at, scan.release_reason = PIIScan.Outcome.RELEASED, actor, timezone.now(), reason
    scan.save()
    audit.record("snapshot.hold_released", obj=snap, before={"outcome": "held"}, after={"outcome": "released"}, reason=reason, actor=actor)
    rebuild(snap.dataset, f"{label(snap.version)} released after the publication scan")
    for user in owners(snap):
        if user != actor:
            notifications.notify(
                user,
                f"Publication released: {label(snap.version)}",
                f"A CPUC system administrator released it to the public dashboards and downloads. Reason: {reason}",
                f"/publication/holds/{snap.pk}/",
                key=f"publication-released:{snap.pk}",
            )
    return scan


def rescan(actor, snap: Snapshot) -> PIIScan:
    """Run the scan again on a held snapshot with the settings as they are now (after a pattern is turned off or a column left out).
    Clear now: it joins the release. Still finding something: the findings are replaced and it stays held."""
    _require_admin(actor, "runs the publication scan again")
    scan = PIIScan.objects.get(snapshot=snap)
    if not scan.held:
        raise access.AccessDenied("Only a held publication is scanned again.")
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "results.parquet"
        with storage.store(AREA).open(snap.key) as src, open(local, "wb") as dst:
            while block := src.read(1 << 22):
                dst.write(block)
        result = _run_scan(local)
    before = {"outcome": scan.outcome, "findings": [{k: f[k] for k in ("column", "pattern", "records")} for f in scan.findings]}
    with transaction.atomic():
        scan = _record_scan(snap, result)
        after = {"outcome": scan.outcome, "findings": [{k: f[k] for k in ("column", "pattern", "records")} for f in scan.findings]}
        audit.record("snapshot.rescanned", obj=snap, before=before, after=after, actor=actor)
        if not scan.held:
            rebuild(snap.dataset, f"{label(snap.version)} cleared by the publication scan")
    return scan


@transaction.atomic
def set_scan_settings(actor, *, patterns: list[str], exempt_columns: list[str], reason: str) -> ScanSettings:
    _require_admin(actor, "changes the publication scan")
    if not reason.strip():
        raise access.AccessDenied("Give the reason for changing the publication scan.")
    unknown = [p for p in patterns if p not in pii.BY_CODE]
    if unknown:
        raise access.AccessDenied(f"Unknown pattern: {', '.join(unknown)}.")
    cfg = ScanSettings.objects.select_for_update().get(pk=scan_settings().pk)
    before = {"patterns": cfg.patterns, "exempt_columns": cfg.exempt_columns}
    cfg.patterns = [p.code for p in pii.PATTERNS if p.code in patterns]
    cfg.exempt_columns = list(dict.fromkeys(c.strip() for c in exempt_columns if c.strip()))
    cfg.changed_by = actor
    cfg.save()
    audit.record(
        "snapshot.scan_settings_changed",
        obj=cfg,
        before=before,
        after={"patterns": cfg.patterns, "exempt_columns": cfg.exempt_columns},
        reason=reason,
        actor=actor,
    )
    return cfg


def holds(user=None):
    """The scans that held a publication, newest first: every one for a system administrator, else those of the user's
    organizations (members see their own filings' holds)."""
    qs = PIIScan.objects.exclude(outcome=PIIScan.Outcome.CLEAR).select_related(
        "snapshot__organisation", "snapshot__version__submission__deadline__period", "released_by"
    )
    if user is not None and not access.is_system_admin(user):
        qs = qs.filter(snapshot__organisation__in=access.organisations_of(user))
    return qs


def may_see(user, snap: Snapshot) -> bool:
    return access.is_system_admin(user) or access.organisations_of(user).filter(pk=snap.organisation_id).exists()


def superseded(snap: Snapshot) -> bool:
    """A newer published version of the filing stands, so the held one would not be published anyway."""
    current = submissions.current(snap.version.submission)
    return current is not None and current.pk != snap.version_id


def withdraw(version_id: int, *, reason: str = "un-confirmed before the due date", release: bool = True) -> None:
    """A version un-confirmed before its due date: its snapshot and file go (decision M), and a release without it is made. Also used
    to replace an imported version's snapshot built wrongly (`reason` says why; `release=False` leaves the release to the caller)."""
    snap = Snapshot.objects.select_related("version__submission__deadline__period", "version__submission__organisation").filter(
        version_id=version_id
    )
    snap = snap.first()
    if snap is None:
        return
    v = snap.version
    before = audit.snapshot(snap, ["dataset", "version", "organisation", "year", "run", "key", "sha256", "rows"])
    storage.store(AREA).delete_prefix(snap.key.rsplit("/", 1)[0] + "/")
    dataset = snap.dataset
    was_released = snap.releases.exists()
    snap.delete()
    audit.record(
        "snapshot.withdrawn",
        object_type="snapshots.snapshot",
        object_id=before["key"],
        before=before,
        reason=reason,
        organisation=v.submission.organisation,
    )
    if release:
        rebuild(dataset, f"{label(v)} withdrawn", changed=was_released)


def in_force(dataset: str) -> list[Snapshot]:
    """The snapshots that stand now (see the module docstring)."""
    snaps = Snapshot.objects.filter(dataset=dataset, version__status=SubmissionVersion.Status.PUBLISHED).select_related(
        "version__submission__deadline__period", "pii_scan"
    )
    by_submission: dict[int, list[Snapshot]] = {}
    for s in snaps:
        by_submission.setdefault(s.version.submission_id, []).append(s)
    standing = []
    for group in by_submission.values():
        current = submissions.current(group[0].version.submission)
        mine = next((s for s in group if current is not None and s.version_id == current.pk), None)
        if mine is None:
            continue
        if not held(mine):
            standing.append(mine)
            continue
        # held by the publication scan: the filing's earlier published version stands until it is released or corrected
        earlier = [s for s in group if s.version.number < current.number and not held(s)]
        if earlier:
            standing.append(max(earlier, key=lambda s: s.version.number))
    if dataset not in YEAR_SLOTS:
        return sorted(standing, key=lambda s: s.created_at)
    best: dict[tuple, Snapshot] = {}
    for s in standing:
        slot = (s.organisation_id, s.year)
        if slot not in best or _rank(s) > _rank(best[slot]):
            best[slot] = s
    return sorted(best.values(), key=lambda s: s.created_at)


def _rank(s: Snapshot) -> tuple:
    period = s.version.submission.deadline.period
    return (period.ends if period else None, s.version.number, s.created_at)


@transaction.atomic
def rebuild(dataset: str, reason: str, changed: bool = False) -> Release | None:
    """A new release when the snapshots in force differ from the latest release's; None when nothing changed. `changed`: a withdrawn
    snapshot has already left the releases that held it, so the comparison alone would miss it."""
    members = in_force(dataset)
    latest = Release.objects.select_for_update().filter(dataset=dataset).order_by("-number").first()
    if latest is None and not members:
        return None
    if not changed and latest is not None and {s.pk for s in latest.snapshots.all()} == {s.pk for s in members}:
        return None
    release = Release.objects.create(dataset=dataset, number=(latest.number + 1) if latest else 1, reason=reason[:300])
    release.snapshots.set(members)
    audit.record(
        "snapshot.released",
        obj=release,
        before={"release": latest.number, "snapshots": sorted(str(s.pk) for s in latest.snapshots.all())} if latest else None,
        after={"release": release.number, "snapshots": sorted(str(s.pk) for s in members)},
        reason=reason,
    )
    return release


# --- reading (the Data module's query layer)


def current_release(dataset: str) -> Release | None:
    return Release.objects.filter(dataset=dataset).order_by("-number").first()


def as_of(dataset: str):
    """When the dataset's data last changed: the time every page and download shows. None before anything is published."""
    release = current_release(dataset)
    return release.created_at if release else None


def release_files(release: Release) -> list[dict]:
    """One entry per snapshot in a release, with what a query needs to label its rows: the file's address (file:// or s3://), the
    submitting organization (under its party's current code: a renamed or merged PA's filings count as the one PA), the year, the
    filing or claim kind and the version."""
    out = []
    mapping = access.code_map()
    for s in release.snapshots.select_related(
        "organisation", "version__submission__deadline__period", "version__submission__deadline__cycle"
    ):
        d = s.version.submission.deadline
        out.append(
            {
                "snapshot": str(s.pk),
                "uri": storage.store(AREA).uri(s.key),
                "key": s.key,
                "organisation": mapping.get(s.organisation.code, s.organisation.code),
                "year": s.year,
                "cycle": str(d.cycle) if d.cycle_id else "",
                "kind": d.kind,
                "due_at": d.due_at,
                "version": s.version_id,
                "version_number": s.version.number,
                "rows": s.rows,
                "sha256": s.sha256,
                "created_at": s.created_at,
            }
        )
    return sorted(out, key=lambda f: (f["organisation"], f["year"] or 0, f["kind"], f["version"]))
