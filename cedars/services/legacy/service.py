"""Staging legacy CEDARS' downloads and recording the years loaded from them.

Staging (`stage`, the `stage_legacy_sources` command): the download manifest lists each file (dataset, year, pa, filename, url, bytes,
sha256). Each record-level file found under the folder is checked against it (size and SHA-256, exactly), opened to read the header of
every CSV inside (each must be a published layout, `dictionary.legacy.layout_of`; macOS `__MACOSX` entries are skipped) and copied to the
uploads storage area at legacy/cedars/<dataset>/<year>/<first 12 of its SHA-256>/<filename>. A file staged before with the same content
is skipped, so the command can be re-run; a file that differs from the manifest is reported and not staged. The loads read the staged
copies only, so they never need CEDARS to be up.

Loading (`record_import`): a module (Budgets for filings, Claims for claim years) turns a year's staged files into canonical tables and
calls this with them; it becomes a published imported submission version (`submissions.import_version`) and a LegacyImport row with the
sources, record counts and reconciliation. A reconciliation with hard failures is refused: that year is not published (the plan's rule).
Loading the same sources again returns the existing import."""

import csv
import io
import shutil
import tempfile
import zipfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.dictionary import legacy as crosswalk
from cedars.services.submissions import service as submissions

from .models import LegacyImport, LegacySource

DATASETS = ("claims", "filings", "filings_archive")
MANIFEST_COLUMNS = ("dataset", "year", "pa", "filename", "url", "bytes", "sha256")
AREA = "uploads"
PREFIX = "legacy/cedars"


class LegacyError(ValueError):
    """A source or an import that cannot be accepted; the message says why."""


@dataclass
class StageReport:
    staged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.refused

    def lines(self) -> list[str]:
        out = [f"staged {len(self.staged)}, already staged {len(self.skipped)}, missing {len(self.missing)}, refused {len(self.refused)}"]
        out += [f"missing: {m}" for m in self.missing]
        out += [f"refused: {n}: {why}" for n, why in self.refused]
        return out


def read_manifest(text: str, datasets=DATASETS) -> list[dict]:
    """The manifest's record-level rows (other rows, such as saved dashboard pages, are left out)."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if rows and not set(MANIFEST_COLUMNS) <= set(rows[0]):
        raise LegacyError(f"The manifest needs the columns {', '.join(MANIFEST_COLUMNS)}.")
    return [r for r in rows if r["dataset"] in datasets]


def members(path: Path) -> dict[str, str]:
    """{CSV inside the zip: its published layout}. Raises LegacyError for a zip with no CSV or a header that is no published layout."""
    out = {}
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                name = info.filename
                if info.is_dir() or name.startswith("__MACOSX/") or not name.lower().endswith(".csv"):
                    continue
                with z.open(info) as f:
                    header = next(csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")), [])
                try:
                    out[name] = crosswalk.layout_of(header)
                except crosswalk.LayoutError as e:
                    raise LegacyError(f"{name}: {e}") from e
    except zipfile.BadZipFile as e:
        raise LegacyError(f"not a zip file ({e})") from e
    if not out:
        raise LegacyError("the zip holds no CSV")
    return out


def _find(folder: Path, row: dict) -> Path | None:
    for candidate in (folder / row["dataset"] / row["year"] / row["filename"], folder / row["dataset"] / row["filename"]):
        if candidate.is_file():
            return candidate
    return next((p for p in folder.rglob(row["filename"]) if p.is_file()), None)


def key_for(row: dict) -> str:
    return f"{PREFIX}/{row['dataset']}/{row['year']}/{row['sha256'][:12]}/{row['filename']}"


def stage(actor, manifest: str, folder: Path, *, datasets=DATASETS, dry_run: bool = False) -> StageReport:
    """Check and copy the manifest's record-level files from the folder to storage (a system job, or a CPUC system administrator)."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator stages legacy CEDARS files.")
    report = StageReport()
    store = storage.store(AREA)
    for row in read_manifest(manifest, datasets):
        name = row["filename"]
        expected_sha, expected_bytes = row["sha256"].strip().lower(), int(row["bytes"])
        existing = LegacySource.objects.filter(name=name, sha256=expected_sha).first()
        if existing is not None and store.exists(existing.key):
            report.skipped.append(name)
            continue
        path = _find(Path(folder), row)
        if path is None:
            report.missing.append(name)
            continue
        size = path.stat().st_size
        if size != expected_bytes:
            report.refused.append((name, f"{size:,} bytes, the manifest says {expected_bytes:,}"))
            continue
        sha = storage.sha256_of(path)
        if sha != expected_sha:
            report.refused.append((name, "its SHA-256 differs from the manifest's"))
            continue
        try:
            inside = members(path)
        except LegacyError as e:
            report.refused.append((name, str(e)))
            continue
        if dry_run:
            report.staged.append(name)
            continue
        key = key_for({**row, "sha256": sha})
        store.put_file(key, path)
        with transaction.atomic():
            source, _ = LegacySource.objects.update_or_create(
                name=name,
                sha256=sha,
                defaults={
                    "dataset": row["dataset"],
                    "year": row["year"],
                    "pa": row["pa"],
                    "url": row["url"],
                    "bytes": size,
                    "key": key,
                    "members": inside,
                    "staged_by": actor,
                },
            )
            audit.record(
                "legacy.source_staged",
                obj=source,
                after={"name": name, "sha256": sha, "bytes": size, "key": key, "members": inside},
                actor=actor,
            )
        report.staged.append(name)
    return report


def sources(dataset: str, year: str) -> list[LegacySource]:
    """The latest staged copy of each file of a dataset's year (all-PA, statewide and per-PA files)."""
    latest: dict[str, LegacySource] = {}
    for s in LegacySource.objects.filter(dataset=dataset, year=year).order_by("-staged_at", "-pk"):
        latest.setdefault(s.name, s)
    return sorted(latest.values(), key=lambda s: (s.pa != "all", s.pa, s.name))


@contextmanager
def open_member(source: LegacySource, member: str, *, text: bool = True):
    """A stream over one CSV inside a staged zip (text, or bytes with text=False), read from storage (never from CEDARS). A zip needs
    random access, so one in S3 is copied to a temporary file first."""
    if member not in source.members:
        raise LegacyError(f"{source.name} holds no {member}")
    store = storage.store(AREA)
    with ExitStack() as stack:
        if hasattr(store, "path"):
            path = store.path(source.key)
        else:
            tmp = stack.enter_context(tempfile.NamedTemporaryFile(suffix=".zip"))
            shutil.copyfileobj(store.open(source.key), tmp)
            tmp.flush()
            path = tmp.name
        z = stack.enter_context(zipfile.ZipFile(path))
        raw = stack.enter_context(z.open(member))
        yield stack.enter_context(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")) if text else raw


def hard_failures(reconciliation: dict) -> list[str]:
    """The names of the hard checks that failed: reconciliation["checks"] = [{name, kind: hard|soft|info, ok, ...}]."""
    return [c["name"] for c in reconciliation.get("checks", []) if c.get("kind") == "hard" and not c.get("ok")]


def already_imported(deadline, organisation, sources: list[LegacySource]) -> LegacyImport | None:
    """The import of this organisation's filing made from exactly these sources, if there is one (any version, not only the latest:
    an earlier snapshot loaded again is not a new version)."""
    shas = sorted(s.sha256 for s in sources)
    for imp in LegacyImport.objects.filter(
        version__submission__deadline=deadline, version__submission__organisation=organisation
    ).prefetch_related("sources"):
        if sorted(s.sha256 for s in imp.sources.all()) == shas:
            return imp
    return None


@transaction.atomic
def record_import(
    actor,
    *,
    module: str,
    period: str,
    deadline,
    organisation,
    files: list[dict],
    sources: list[LegacySource],
    records: int,
    outside_totals: int = 0,
    reconciliation: dict,
    calculation: dict | None = None,
) -> LegacyImport:
    """Publish a loaded year as an imported version. Refused while any hard check failed; the same sources loaded again return the
    import already made."""
    failed = hard_failures(reconciliation)
    if failed:
        raise LegacyError(f"{organisation.code} {period} is not published: the hard checks {', '.join(failed)} failed.")
    shas = sorted(s.sha256 for s in sources)
    done = already_imported(deadline, organisation, sources)
    if done is not None:
        return done
    previous = LegacyImport.objects.filter(version__submission__deadline=deadline, version__submission__organisation=organisation).first()
    reason = "Imported from legacy CEDARS: " + ", ".join(s.url for s in sources)
    if previous is not None:
        reason = "Re-imported: the legacy CEDARS download changed. " + reason
    version = submissions.import_version(
        actor,
        deadline=deadline,
        organisation=organisation,
        files=files,
        reason=reason,
        calculation=calculation or {"engine": "CET (legacy CEDARS), release not published", "sources": shas},
        findings={"records": records, "outside_totals": outside_totals},
    )
    imp = LegacyImport.objects.create(
        version=version,
        module=module,
        period=period,
        records=records,
        outside_totals=outside_totals,
        reconciliation=reconciliation,
        loaded_by=actor,
    )
    imp.sources.set(sources)
    audit.record(
        "legacy.imported",
        obj=imp,
        after={"module": module, "period": period, "version": version.number, "records": records, "sources": shas},
        actor=actor,
        organisation=organisation if actor is None else None,
    )
    return imp
