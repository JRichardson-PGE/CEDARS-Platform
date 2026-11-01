"""The PDA migration (BR-15.10): every published PDA document with its attributes, work order and task, its main file and attachments,
its comment window, and its comments and responses, with their original dates and authors' names, read from the PDA's own public API
(the JSON its pages use: /api/categories, /api/work-orders, /api/deliverables, /api/deliverables-comments) and its file links
(/api/downloads/<id>/<file>).

The records come from saved JSON files (a capture of those endpoints) or straight from the site; the files from a folder laid out as
<deliverableID>/<file name> or from the site. Each document is filed in the EM&V collection, owned by the CPUC, as one published version
(the PDA keeps no versions), and keeps its PDA id, so its old links redirect to it (views.pda_*). A document already imported is
skipped, so the import can be run again after a failure. --dry-run reads and maps everything and stores nothing.

Field meanings, checked against the capture of 2026-10-30 (2,669 published records; CEDARS-Research notes/pda_psr_capture_findings.md):
  - dateUploaded (UTC) is when the document was filed and published: the cards' "Published" date, shown in Pacific time;
  - pubViewStart and pubViewEnd are the comment window (the cards' "Comments End"); 2000-01-01 is the PDA's "no date" (433 starts),
    a window that starts after it ends (9 records) opens on its last day;
  - a work order is the record's workOrder, else its task's contract (all 179 tasked records); cpucContID holds the CPUC contract
    number where there is one ("23NC0465"), contName a description, so the number is cpucContID when it looks like a contract number
    and unique, else PDA-<contractID>;
  - three main file names end in leftover HTML ("<", "</a>"), cut off for the stored name; the old links keep the raw name.
Files of a type the library refuses (16 .exe main files) are kept inside a zip of the same name, never served as they are. Comment
attachments come only from a folder (comments/<commentID>/<file name>): the public API names them but no link to them is known."""

from __future__ import annotations

import json
import re
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from cedars.services import audit
from cedars.services.accounts import service as access

from .models import AttributeOption, Collection, Comment, CommentPeriod, CommentResponse, Document, DocumentVersion, Task, WorkOrder
from .service import FILE_TYPES, DocumentError, Upload, _check_uploads, _store_files, clean_name, type_list

FIELDS = {
    "id": "deliverableID",
    "title": "name",
    "summary": "summary",
    "filed": "dateUploaded",
    "comments_open": "pubViewStart",
    "comments_close": "pubViewEnd",
    "calmac": "calmacID",
    "file": "fileName",
    "attachments": "attachments",
    "options": "options",
    "work_order": "workOrder",
    "task": "task",
    "published": "isPublished",
}
DEFAULT_TYPE = "Report"  # the deliverable type of a document the PDA did not tag with one (109 have no tags at all)
REASON = "Migrated from the PDA"
TIMEOUT = 120


# --- where the records and files come from


class FolderSource:
    """A capture on disk: <root>/<deliverableID>/<file name>, and comments/<commentID>/<file name> for comment attachments."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def file(self, pda_id: int, name: str) -> Path | None:
        """The file under its PDA name, or under the name cut before leftover HTML (which cannot be a file name on Windows)."""
        for candidate in (name, name.split("<", 1)[0].strip()):
            path = self.root / str(pda_id) / candidate
            if candidate and "/" not in candidate and path.is_file():
                return path
        return None

    def comment_file(self, comment_id: int, name: str) -> Path | None:
        path = self.root / "comments" / str(comment_id) / name
        return path if path.is_file() else None


class SiteSource:
    """The PDA itself (e.g. https://pda.energydataweb.com): each file fetched from its download link into a temporary file."""

    def __init__(self, base_url: str, work: Path):
        self.base = base_url.rstrip("/")
        self.work = Path(work)

    def get_json(self, path: str):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=TIMEOUT) as r:  # noqa: S310 (an https URL the operator gives)
            return json.load(r)

    def file(self, pda_id: int, name: str) -> Path | None:
        target = self.work / str(pda_id) / urllib.parse.quote(name, safe="")
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.base}/api/downloads/{pda_id}/{urllib.parse.quote(name)}"
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r, open(target, "wb") as out:  # noqa: S310
                while chunk := r.read(1 << 20):
                    out.write(chunk)
        except OSError:
            target.unlink(missing_ok=True)
            return None
        return target

    def comment_file(self, comment_id: int, name: str) -> Path | None:
        return None


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- reading the records


NO_DATE = date(2000, 1, 1)  # the PDA's placeholder for a date not given


def _when(value) -> datetime | None:
    """A PDA date or date-time as an aware date-time (a date alone is midnight in the platform's time zone); None for the PDA's
    2000-01-01 placeholder."""
    if not value or str(value).startswith(NO_DATE.isoformat()):
        return None
    text = str(value)
    dt = parse_datetime(text)
    if dt is None:
        d = parse_date(text[:10])
        if d is None:
            return None
        dt = datetime.combine(d, time())
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


def _day(value) -> date | None:
    dt = _when(value)
    return timezone.localdate(dt) if dt else None


def _attachment_names(record: dict) -> list[str]:
    out = []
    for a in record.get(FIELDS["attachments"]) or []:
        name = a if isinstance(a, str) else (a.get("fileName") or a.get("name") or a.get("attachment") or "")
        if name:
            out.append(str(name))
    return out


def _option_ids(record: dict) -> list[int]:
    out = []
    for o in record.get(FIELDS["options"]) or []:
        if isinstance(o, dict):
            value = o.get("catOptID") or (o.get("option") or {}).get("catOptID")
        else:
            value = o
        if value not in (None, ""):
            out.append(int(value))
    return out


@dataclass
class Report:
    documents: int = 0
    skipped_existing: int = 0
    skipped_unpublished: int = 0
    files: int = 0
    wrapped: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    no_main_file: list[int] = field(default_factory=list)
    untyped: list[int] = field(default_factory=list)
    unknown_options: Counter = field(default_factory=Counter)
    comments: int = 0
    responses: int = 0
    comment_files_missing: int = 0
    work_orders: int = 0
    tasks: int = 0
    failed: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"{self.documents} documents imported ({self.files} files), {self.skipped_existing} already imported, "
            f"{self.skipped_unpublished} not published (left out)",
            f"{self.comments} comments and {self.responses} responses; {self.comment_files_missing} comment attachments not found",
            f"{self.work_orders} work orders and {self.tasks} tasks added",
        ]
        if self.untyped:
            out.append(f"{len(self.untyped)} documents with no deliverable type filed as {DEFAULT_TYPE}: {_ids(self.untyped)}")
        if self.no_main_file:
            out.append(f"{len(self.no_main_file)} documents left out, main file not found: {_ids(self.no_main_file)}")
        if self.failed:
            out.append(f"{len(self.failed)} documents left out on an error: {'; '.join(self.failed[:20])}")
        if self.missing_files:
            out.append(f"{len(self.missing_files)} attachments not found: {', '.join(self.missing_files[:20])}")
        if self.wrapped:
            out.append(f"{len(self.wrapped)} files of a refused type kept inside a zip: {', '.join(self.wrapped[:20])}")
        if self.unknown_options:
            unknown = ", ".join(f"{k} x{v}" for k, v in self.unknown_options.items())
            out.append(f"category options not in the value lists (left off): {unknown}")
        return out


def _ids(ids) -> str:
    ids = list(ids)
    return ", ".join(str(i) for i in ids[:20]) + (f" and {len(ids) - 20} more" if len(ids) > 20 else "")


# --- work orders and tasks


CONTRACT_NUMBER = re.compile(r"(?=.*\d)[A-Za-z0-9][A-Za-z0-9_.-]{4,49}")  # "23NC0465", "13PS5048", "ED_D_Res_1"; not "Year 2" or "609"


def _number(data: dict, pda_id: int) -> str:
    candidate = str(data.get("cpucContID") or "").strip()
    if CONTRACT_NUMBER.fullmatch(candidate) and not candidate.isdigit():
        taken = WorkOrder.objects.filter(number__iexact=candidate).exclude(pda_id=pda_id).exclude(pda_id__isnull=True).exists()
        if not taken:
            return candidate
    return f"PDA-{pda_id}"


def _work_order(data: dict | None, report: Report, *, dry_run: bool) -> WorkOrder | None:
    if not data or not data.get("contractID"):
        return None
    pda_id = int(data["contractID"])
    wo = WorkOrder.objects.filter(pda_id=pda_id).first()
    if wo is not None:
        return wo
    number = _number(data, pda_id)
    wo = WorkOrder.objects.filter(number__iexact=number, pda_id__isnull=True).first()
    if dry_run:
        report.work_orders += wo is None
        return wo
    if wo is not None:
        wo.pda_id = pda_id
        wo.save(update_fields=["pda_id"])
        return wo
    firm = data.get("company") or data.get("firmName") or data.get("firm") or data.get("name") or ""
    if isinstance(firm, dict):
        firm = firm.get("name") or firm.get("firmName") or ""
    wo = WorkOrder.objects.create(
        number=number,
        name=str(data.get("contName") or "")[:255],
        firm_name=str(firm)[:200],
        cpuc_contract_id=str(data.get("cpucContID") or "")[:50],
        pda_id=pda_id,
    )
    audit.record("documents.work_order_imported", obj=wo, after=audit.snapshot(wo), actor_label="PDA migration")
    report.work_orders += 1
    return wo


def import_work_orders(rows: list[dict], report: Report, *, dry_run: bool = False) -> None:
    for row in rows:
        _work_order(row, report, dry_run=dry_run)


def _task(data, wo: WorkOrder | None, report: Report, *, dry_run: bool) -> Task | None:
    if not data or wo is None or wo.pk is None:
        return None
    if isinstance(data, dict):
        pda_id, name = data.get("taskID"), data.get("taskName") or data.get("name") or ""
    else:
        pda_id, name = None, str(data)
    name = str(name).strip()[:200] or (f"Task {pda_id}" if pda_id else "")
    if not name:
        return None
    existing = Task.objects.filter(pda_id=pda_id).first() if pda_id else None
    existing = existing or Task.objects.filter(work_order=wo, name=name).first()
    if existing is not None or dry_run:
        return existing
    report.tasks += 1
    return Task.objects.create(work_order=wo, name=name, pda_id=int(pda_id) if pda_id else None)


# --- documents


def _upload(path: Path, name: str, report: Report, work: list[Path]) -> tuple[Upload, str]:
    """The file as the library takes it, and its PDA name when that differs: leftover HTML after the name is cut off, and a refused
    type goes inside a zip of the same name (the zip is made in a temporary file listed in work, for the caller to remove)."""
    clean = clean_name(name.split("<", 1)[0].strip() or name)
    if Path(clean).suffix.lower() in FILE_TYPES:
        return Upload(clean, open(path, "rb")), (name if clean != name else "")
    report.wrapped.append(name)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        work.append(Path(tmp.name))
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(path, arcname=clean)
    return Upload(f"{clean}.zip", open(work[-1], "rb")), name


def import_document(record: dict, source, cpuc, report: Report, *, comments: list[dict] | None = None, dry_run: bool = False):
    """One PDA document; None when it is left out (already imported, unpublished, no main file) or on a dry run."""
    pda_id = int(record[FIELDS["id"]])
    if Document.objects.filter(pda_id=pda_id).exists():
        report.skipped_existing += 1
        return None
    if record.get(FIELDS["published"]) is False:
        report.skipped_unpublished += 1
        return None
    types = type_list(Collection.EMV)
    options = {o.pda_id: o for o in AttributeOption.objects.filter(pda_id__isnull=False).select_related("attribute_list")}
    chosen = []
    for oid in _option_ids(record):
        if oid in options:
            chosen.append(options[oid])
        else:
            report.unknown_options[oid] += 1
    doc_type = next((o for o in chosen if o.attribute_list_id == types.pk), None)
    if doc_type is None:
        report.untyped.append(pda_id)
        doc_type = types.options.get(label=DEFAULT_TYPE)
    attributes = [o for o in chosen if o.attribute_list_id != types.pk and Collection.EMV in o.attribute_list.collections]

    main_name = str(record.get(FIELDS["file"]) or "")
    main = source.file(pda_id, main_name) if main_name else None
    if main is None:
        report.no_main_file.append(pda_id)
        return None
    found = [(main_name, main)]
    for name in _attachment_names(record):
        path = source.file(pda_id, name)
        if path is None:
            report.missing_files.append(f"{pda_id}/{name}")
        else:
            found.append((name, path))
    task_data = record.get(FIELDS["task"]) or {}
    contract = record.get(FIELDS["work_order"]) or (task_data.get("contract") if isinstance(task_data, dict) else None)
    wo = _work_order(contract, report, dry_run=dry_run)
    task = _task(task_data, wo, report, dry_run=dry_run)
    if dry_run:
        report.documents += 1
        report.files += len(found)
        report.comments += len(comments or [])
        report.responses += sum(1 for c in comments or [] if c.get("response"))
        return None
    try:
        with transaction.atomic():
            return _create(record, pda_id, cpuc, doc_type, attributes, wo, task, found, comments or [], source, report)
    except DocumentError as e:  # e.g. an empty file: the document is left out and named in the report
        report.failed.append(f"{pda_id}: {e}")
        return None


def _create(record, pda_id, cpuc, doc_type, attributes, wo, task, found, comments, source, report) -> Document:
    filed = _when(record.get(FIELDS["filed"])) or timezone.now()
    doc = Document.objects.create(
        collection=Collection.EMV,
        document_type=doc_type,
        title=str(record.get(FIELDS["title"]) or main_title(found))[:200],
        summary=str(record.get(FIELDS["summary"]) or "")[:3500],
        owner=cpuc,
        work_order=wo,
        task=task,
        published_at=filed,
        created_at=filed,
        pda_id=pda_id,
        calmac_id=str(record.get(FIELDS["calmac"]) or "").strip()[:50],
    )
    doc.attributes.set(attributes)
    version = DocumentVersion.objects.create(document=doc, number=1, reason=REASON, organisation=cpuc, created_at=filed, published_at=filed)
    uploads, legacy, work = [], [], []
    try:
        for name, path in found:
            upload, original = _upload(path, name, report, work)
            uploads.append(upload)
            legacy.append(original)
        files = _store_files(version, uploads, _check_uploads(uploads), legacy_names=legacy)
    finally:
        for u in uploads:
            u.file.close()
        for p in work:
            p.unlink(missing_ok=True)
    report.files += len(files)
    opens, closes = _day(record.get(FIELDS["comments_open"])), _day(record.get(FIELDS["comments_close"]))
    if closes:
        opens = min(opens or timezone.localdate(filed), closes)
        CommentPeriod.objects.create(version=version, opens=opens, closes=closes)
    for c in sorted(comments, key=lambda c: (str(c.get("createdOn") or ""), c.get("commentID") or 0)):
        _comment(c, version, cpuc, source, report)
    audit.record(
        "documents.imported",
        obj=doc,
        after={"pda_id": pda_id, "files": [f.name for f in files], "comments": len(comments), "published_at": filed.isoformat()},
        reason=REASON,
        actor_label="PDA migration",
    )
    report.documents += 1
    return doc


def main_title(found) -> str:
    return Path(found[0][0]).stem


def _comment(c: dict, version: DocumentVersion, cpuc, source, report: Report) -> None:
    pda_id = int(c["commentID"]) if c.get("commentID") else None
    if pda_id and Comment.objects.filter(pda_id=pda_id).exists():
        return
    posted = _when(c.get("createdOn")) or version.published_at
    file, checked = {}, None
    name = c.get("attachment")
    if name and pda_id:
        path = source.comment_file(pda_id, str(name))
        if path is None:
            report.comment_files_missing += 1
        else:
            from .comments import _store_attachment

            try:
                with open(path, "rb") as f:
                    file, checked = _store_attachment(version, Upload(str(name), f), enforce=False)
            except DocumentError:  # a refused type or an empty file: the comment is kept without it
                report.comment_files_missing += 1
    comment = Comment.objects.create(
        version=version,
        kind=Comment.Kind.QUESTION if c.get("isQuestion") else Comment.Kind.COMMENT,
        subject=(str(c.get("subject") or "").strip() or "Comment")[:100],
        section=str(c.get("section") or "").strip()[:100],
        text=str(c.get("comment") or "").strip(),
        author=None,
        author_name=str(c.get("createdBy") or "").strip()[:200] or "PDA user",
        created_at=posted,
        withdrawn_at=posted if c.get("isActive") is False else None,
        pda_id=pda_id,
        **file,
    )
    report.comments += 1
    if checked is not None:
        from cedars.services.accessibility import service as accessibility

        accessibility.record(
            "document_comment",
            comment.pk,
            checked,
            file_name=comment.file_name,
            sha256=comment.file_sha256,
            url=version.document.get_absolute_url(),
            title=f"Comment on {version.document.title}",
            imported=True,
        )
    if c.get("response"):
        CommentResponse.objects.create(
            comment=comment,
            text=str(c["response"]).strip(),
            author=None,
            author_name=str(c.get("responseBy") or "").strip()[:200] or "CPUC Energy Division",
            organisation=cpuc,
            created_at=_when(c.get("responseDate")) or posted,
        )
        report.responses += 1


def run(records: list[dict], source, *, comments_for=None, dry_run: bool = False, report: Report | None = None) -> Report:
    """Import the records in their PDA id order. comments_for(pda_id) gives a document's comments (or None for none)."""
    report = report or Report()
    cpuc = access.cpuc()
    if cpuc is None:
        raise RuntimeError("Set up the CPUC organization before importing the PDA.")
    for record in sorted(records, key=lambda r: int(r[FIELDS["id"]])):
        pda_id = int(record[FIELDS["id"]])
        import_document(record, source, cpuc, report, comments=comments_for(pda_id) if comments_for else None, dry_run=dry_run)
    return report
