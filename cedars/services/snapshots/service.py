"""Publication snapshots. The submissions service tells this one when a version is published or un-confirmed (`register_listener`); the
work runs once the confirming transaction has committed, so copying a large results file never holds the version's lock, and a failure
leaves the version published with no snapshot (`manage.py snapshot_published` makes the missing ones).

What is in force (`rebuild`): the snapshots of each submission's standing published version (`submissions.current`), one per slot:
  forecast  every filing (application, TUAL, MCAL ...) is its own slot; the query layer picks the filing a page asks for
  claims    one slot per PA and claim year: each quarter's results are compiled year to date, so only the latest period stands
            (the annual claim once confirmed), never two quarters added together
  monthly   likewise, one per PA and report year: the latest month's year-to-date report
A release is made only when that set changes.

Imported (legacy) versions run no publisher hook; their loads can call `publish` once they write measure-level results."""

import logging
import tempfile
from pathlib import Path

from django.db import transaction

from cedars.core import storage
from cedars.services import audit
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .models import Dataset, Release, Snapshot

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


def publish(version_id: int) -> Snapshot | None:
    """Freeze a published version's measure-level results and release them. Idempotent: a version that already has its snapshot only
    has the release checked. None when the version is not published or has no measure-level results."""
    v = SubmissionVersion.objects.select_related("submission__deadline__period", "submission__organisation").get(pk=version_id)
    dataset, key = dataset_of(v), source_key(v)
    if v.status != SubmissionVersion.Status.PUBLISHED or not dataset or not key:
        return None
    snap = Snapshot.objects.filter(version=v).first()
    if snap is None:
        snap = _write(v, dataset, key)
    rebuild(dataset, f"{label(v)} published")
    return snap


def _write(v: SubmissionVersion, dataset: str, key: str) -> Snapshot:
    import pyarrow.parquet as pq

    snap = Snapshot(dataset=dataset, version=v, organisation=v.submission.organisation, run=str((v.calculation or {}).get("run", "")))
    snap.year = v.submission.deadline.period.year if v.submission.deadline.period_id else None
    snap.source_key = key
    snap.key = f"{dataset}/{snap.id}/results.parquet"
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "results.parquet"
        with storage.store("results").open(key) as src, open(local, "wb") as dst:
            while block := src.read(1 << 22):
                dst.write(block)
        snap.rows = pq.ParquetFile(local).metadata.num_rows
        snap.sha256 = storage.sha256_of(local)
        snap.bytes = storage.store(AREA).put_file(snap.key, local).size
    snap.save()
    audit.record(
        "snapshot.created",
        obj=snap,
        after=audit.snapshot(snap, ["dataset", "version", "organisation", "year", "run", "key", "sha256", "rows"]),
        organisation=v.submission.organisation,
    )
    return snap


def withdraw(version_id: int) -> None:
    """A version un-confirmed before its due date: its snapshot and file go (decision M), and a release without it is made."""
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
        reason="un-confirmed before the due date",
        organisation=v.submission.organisation,
    )
    rebuild(dataset, f"{label(v)} withdrawn", changed=was_released)


def in_force(dataset: str) -> list[Snapshot]:
    """The snapshots that stand now (see the module docstring)."""
    snaps = Snapshot.objects.filter(dataset=dataset, version__status=SubmissionVersion.Status.PUBLISHED).select_related(
        "version__submission__deadline__period"
    )
    standing = []
    for s in snaps:
        current = submissions.current(s.version.submission)
        if current is not None and current.pk == s.version_id:
            standing.append(s)
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
    submitting organisation, the year, the filing or claim kind and the version."""
    out = []
    for s in release.snapshots.select_related(
        "organisation", "version__submission__deadline__period", "version__submission__deadline__cycle"
    ):
        d = s.version.submission.deadline
        out.append(
            {
                "snapshot": str(s.pk),
                "uri": storage.store(AREA).uri(s.key),
                "key": s.key,
                "organisation": s.organisation.code,
                "year": s.year,
                "cycle": str(d.cycle) if d.cycle_id else "",
                "kind": d.kind,
                "version": s.version_id,
                "version_number": s.version.number,
                "rows": s.rows,
            }
        )
    return sorted(out, key=lambda f: (f["organisation"], f["year"] or 0, f["kind"], f["version"]))
