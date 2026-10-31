"""The migration of CEDARS' own documents (BR-15.10): Reports & Documents (the PAs' report uploads and the CPUC staff guidance), DEER
Resources, and the program documents (implementation plans and their redlines and change summaries), each with every version and
file, their original dates, and their old addresses, which then lead to the migrated record (views.cedars_legacy).

CEDARS' pages are not an API, and its PA document lists are behind its login, so the records come from a manifest: a CSV with one row
per file of a version, written by whoever exports them (CEDARS' own database at cutover, or a crawl of its public pages), and a folder
of the files. Columns (MANIFEST; the rest optional):
  collection      pa | guidance | deer | program
  key             the record's id in CEDARS, unique within the collection ("dispositions/184", "guidance/29", "history/1203")
  document_type   a label of the collection's type list ("Disposition", "Template", "Implementation plan"); CPUC ED keeps the lists
  title, summary  the record's title and notes
  pas, programs   organisation codes and PrgIDs, separated by ";"
  first_year, last_year   the reporting year or the years it covers
  owner           the organisation that filed it (default: the first PA for PA and program documents, else the CPUC)
  version         1, 2 ... in order (DEER's history rows, the implementation plan's version numbers)
  posted          when the version was posted (date or ISO date-time): its filing and publication date
  in_effect_from, in_effect_to, notes   the version's start and end dates and notes, kept in its reason
  published       yes (default) or no
  file            the file's path under the files folder; file_name the name to give it (default: the file's own)
  role            main (default for the first file of a version) | redline | change_summary | attachment
  file_links      old addresses of this file's download ("/deer-resources/tools/energy-plus/file/3195/download/"), separated by ";"
  page_links      old addresses of the record's page or history ("/deer-resources/tools/energy-plus/resource/29/history/")
A record already imported is skipped, so the import can be run again; --dry-run reads and checks everything and stores nothing.

What the public pages showed on 2026-10-31 (CEDARS-Research notes): DEER Resources has about 1,760 resources in 20 sections, each
with a history of dated versions (no version numbers; superseded ones carry an end date) at /deer-resources/<section>/resource/<id>/
history/ and files at /deer-resources/<section>/file/<id>/download/; the staff guidance is 20 files at /documents/guidance-document/
download/<id>/; an implementation plan's history is /documents/history/<id>/ with each version's main, redline and change summary at
/documents/download/<id>/<kind>change_summary|main|redline)/ (the old site's broken route, kept exactly)."""

from __future__ import annotations

import csv
import re
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.registry.models import Program

from .models import Collection, Document, DocumentVersion, LegacyLink
from .pda_import import _upload, _when
from .service import DocumentError, _check_uploads, _store_files, type_list

MANIFEST = ["collection", "key", "document_type", "title", "version", "posted", "file"]
COLLECTIONS = {Collection.PA, Collection.GUIDANCE, Collection.DEER, Collection.PROGRAM}
ROLES = ["main", "attachment", "redline", "change_summary"]  # the order of a version's files
REASON = "Migrated from CEDARS"
LABEL = "CEDARS migration"
YES = {"", "yes", "y", "true", "1"}


def legacy_path(link: str) -> str:
    """An old address as it is kept: no scheme or host, decoded, one leading slash."""
    parts = urllib.parse.urlsplit(link.strip())
    return "/" + urllib.parse.unquote(parts.path).lstrip("/")


def _split(value: str) -> list[str]:
    return [v.strip() for v in re.split(r"[;\n]", value or "") if v.strip()]


@dataclass
class Report:
    documents: int = 0
    versions: int = 0
    files: int = 0
    links: int = 0
    skipped_existing: int = 0
    wrapped: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unknown: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    link_taken: list[str] = field(default_factory=list)
    by_collection: Counter = field(default_factory=Counter)

    def lines(self) -> list[str]:
        collections = ", ".join(f"{dict(Collection.choices)[c]} {n}" for c, n in sorted(self.by_collection.items()))
        out = [
            f"{self.documents} documents imported ({self.versions} versions, {self.files} files; {collections or 'none'}), "
            f"{self.skipped_existing} already imported",
            f"{self.links} old addresses lead to them",
        ]
        if self.failed:
            out.append(f"{len(self.failed)} documents left out: {'; '.join(self.failed[:20])}" + _more(self.failed))
        if self.missing_files:
            out.append(f"{len(self.missing_files)} files not found (their documents left out): {', '.join(self.missing_files[:20])}")
        if self.wrapped:
            out.append(f"{len(self.wrapped)} files of a refused type kept inside a zip: {', '.join(self.wrapped[:20])}")
        for what, values in self.unknown.items():
            listed = ", ".join(f"{v!r} x{n}" for v, n in values.most_common(15))
            out.append(f"{what} not found here (left off): {listed}")
        if self.link_taken:
            taken = ", ".join(self.link_taken[:10])
            out.append(f"{len(self.link_taken)} old addresses already lead elsewhere (kept as they were): {taken}")
        return out


def _more(items) -> str:
    return f" and {len(items) - 20} more" if len(items) > 20 else ""


def read_manifest(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise DocumentError("The manifest has no rows.")
    missing = [c for c in MANIFEST if c not in rows[0]]
    if missing:
        raise DocumentError(f"The manifest lacks the columns {', '.join(missing)}.")
    return [{k: (v or "").strip() for k, v in row.items() if k} for row in rows]


def _records(rows: list[dict], report: Report) -> list[tuple[str, str, list[dict]]]:
    """The rows grouped by record, in the manifest's order."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for n, row in enumerate(rows, start=2):
        collection, key = row["collection"].lower(), row["key"]
        if collection not in COLLECTIONS or not key:
            report.failed.append(f"line {n}: collection {row['collection']!r}, key {key!r}")
            continue
        grouped.setdefault((collection, key), []).append(row)
    return [(c, k, r) for (c, k), r in grouped.items()]


def _versions(rows: list[dict]) -> list[tuple[int, list[dict]]]:
    by_number: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_number[int(row["version"] or 1)].append(row)
    out = []
    for number in sorted(by_number):
        files = by_number[number]
        for i, row in enumerate(files):
            row.setdefault("role", "")
            row["role"] = (row["role"] or ("main" if i == 0 else "attachment")).lower()
        files.sort(key=lambda r: ROLES.index(r["role"]) if r["role"] in ROLES else len(ROLES))
        out.append((number, files))
    return out


def _reason(number: int, first: dict) -> str:
    parts = [REASON if number == 1 else f"{REASON}: version {number}"]
    start, end = first.get("in_effect_from", ""), first.get("in_effect_to", "")
    if start or end:
        parts.append(f"In effect {'from ' + start if start else ''}{' ' if start and end else ''}{'to ' + end if end else ''}")
    if first.get("notes"):
        parts.append(first["notes"])
    return ". ".join(parts)


class Context:
    def __init__(self, files: Path):
        self.files = Path(files)
        self.cpuc = access.cpuc()
        if self.cpuc is None:
            raise RuntimeError("Set up the CPUC organisation before importing CEDARS' documents.")
        self.organisations = {o.code.upper(): o for o in Organisation.objects.all()}
        self.programs = {p.prg_id.upper(): p for p in Program.objects.all()}
        self.types = {}
        for c in COLLECTIONS:
            tl = type_list(c)
            self.types[c] = {o.label.lower(): o for o in tl.options.all()} if tl else {}

    def file(self, name: str) -> Path | None:
        if not name:
            return None
        path = (self.files / name).resolve()
        root = self.files.resolve()
        return path if path.is_file() and root in path.parents else None


def import_record(collection: str, key: str, rows: list[dict], ctx: Context, report: Report, *, dry_run: bool = False):
    cedars_key = f"{collection}:{key}"
    if Document.objects.filter(cedars_key=cedars_key).exists():
        report.skipped_existing += 1
        return None
    first = rows[0]
    doc_type = ctx.types[collection].get(first["document_type"].lower())
    if doc_type is None:
        report.unknown[f"document types of {dict(Collection.choices)[collection]}"][first["document_type"]] += 1
        report.failed.append(f"{cedars_key}: no type {first['document_type']!r}")
        return None
    pas = []
    for code in _split(first.get("pas", "")):
        o = ctx.organisations.get(code.upper())
        if o is None:
            report.unknown["organisations"][code] += 1
        elif o not in pas:
            pas.append(o)
    programs = []
    for prg_id in _split(first.get("programs", "")):
        p = ctx.programs.get(prg_id.upper())
        if p is None:
            report.unknown["programs"][prg_id] += 1
        else:
            programs.append(p)
    owner = ctx.organisations.get(first.get("owner", "").upper()) if first.get("owner") else None
    if owner is None:
        owner = pas[0] if pas and collection in (Collection.PA, Collection.PROGRAM) else ctx.cpuc
    try:
        versions = _versions(rows)
    except ValueError:
        report.failed.append(f"{cedars_key}: a version is not a number")
        return None
    found = []
    for number, files in versions:
        paths = []
        for row in files:
            path = ctx.file(row["file"])
            if path is None:
                report.missing_files.append(row["file"] or f"{cedars_key} v{number}")
                return None
            paths.append((row, path))
        found.append((number, paths))
    years = [int(y) for y in (first.get("first_year", ""), first.get("last_year", "")) if y.isdigit()]
    if dry_run:
        report.documents += 1
        report.by_collection[collection] += 1
        report.versions += len(found)
        report.files += sum(len(p) for _, p in found)
        return None
    try:
        with transaction.atomic():
            return _create(collection, cedars_key, first, doc_type, owner, pas, programs, years, found, report)
    except (DocumentError, ValueError) as e:
        report.failed.append(f"{cedars_key}: {e}")
        return None


def _posted(row: dict) -> datetime | None:
    return _when(row.get("posted"))


def _create(collection, cedars_key, first, doc_type, owner, pas, programs, years, found, report) -> Document:
    dates = [_posted(paths[0][0]) or timezone.now() for _, paths in found]
    published = [d for d, (_, paths) in zip(dates, found, strict=True) if paths[0][0].get("published", "").lower() in YES]
    doc = Document.objects.create(
        collection=collection,
        document_type=doc_type,
        title=(first["title"] or Path(first["file"]).stem)[:200],
        summary=first.get("summary", "")[:3500],
        owner=owner,
        first_year=min(years) if years else None,
        last_year=max(years) if years else None,
        published_at=min(published) if published else None,
        created_at=min(dates),
        cedars_key=cedars_key,
    )
    doc.pas.set(pas)
    doc.programs.set(programs)
    links = []
    for (number, paths), posted in zip(found, dates, strict=True):
        row = paths[0][0]
        is_published = row.get("published", "").lower() in YES
        version = DocumentVersion.objects.create(
            document=doc,
            number=number,
            reason=_reason(number, row),
            organisation=owner,
            created_at=posted,
            published_at=posted if is_published else None,
        )
        uploads, legacy, work = [], [], []
        try:
            for r, path in paths:
                upload, original = _upload(path, r.get("file_name") or path.name, report, work)
                uploads.append(upload)
                legacy.append(original)
            files = _store_files(version, uploads, _check_uploads(uploads), legacy_names=legacy)
        finally:
            for u in uploads:
                u.file.close()
            for p in work:
                p.unlink(missing_ok=True)
        report.versions += 1
        report.files += len(files)
        for (r, _), f in zip(paths, files, strict=True):
            links += [(link, f) for link in _split(r.get("file_links", ""))]
            links += [(link, None) for link in _split(r.get("page_links", ""))]
    for link, f in links:
        path = legacy_path(link)
        if LegacyLink.objects.filter(path=path).exists():
            if not LegacyLink.objects.filter(path=path, document=doc).exists():
                report.link_taken.append(path)
            continue
        LegacyLink.objects.create(path=path, document=doc, file=f)
        report.links += 1
    audit.record(
        "documents.imported",
        obj=doc,
        after={"cedars_key": cedars_key, "versions": len(found), "published_at": doc.published_at and doc.published_at.isoformat()},
        reason=REASON,
        actor_label=LABEL,
    )
    report.documents += 1
    report.by_collection[collection] += 1
    return doc


def run(rows: list[dict], files: Path, *, dry_run: bool = False, limit: int | None = None) -> Report:
    report = Report()
    ctx = Context(files)
    records = _records(rows, report)
    for collection, key, record_rows in records[:limit] if limit else records:
        import_record(collection, key, record_rows, ctx, report, dry_run=dry_run)
    return report
