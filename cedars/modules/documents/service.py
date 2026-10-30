"""The document library (BR-15.1, 15.2, 15.9): who may see and change what, filing a document, its versions and their publication, value
lists, search and downloads. Every change writes an audit event; so does every download (BR-15.9, replacing the PDA's download report).

Who may do what (permissions in the documents module, held in an organisation):
  - anyone, signed in or not, reads a published document and its published versions;
  - documents.read, .submit, .confirm or .admin in the owning organisation or in a PA named on the document (a joint document) shows
    its unpublished versions too, and so does any of them in the CPUC (BR-15.2: "only their owning organisation and CPUC ED");
  - documents.submit there adds versions and changes the details; documents.confirm publishes a version;
  - documents.submit in the acting organisation files a new document there: a PA files PA documents, program files and EM&V documents,
    another organisation (an evaluator) EM&V documents, the CPUC any collection (guidance and DEER resources are the CPUC's alone);
  - documents.admin in the CPUC (or a CPUC system administrator) does all of it on every document and keeps the value lists.
`actor=None` is the system (imports)."""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePath

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Prefetch, Q, QuerySet
from django.utils import timezone

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.registry.models import Program

from .models import AttributeList, AttributeOption, Collection, Document, DocumentFile, DocumentVersion, Study, Task, WorkOrder

READ = "documents.read"
SUBMIT = "documents.submit"
CONFIRM = "documents.confirm"
ADMIN = "documents.admin"
ANY = (READ, SUBMIT, CONFIRM, ADMIN)

CPUC_COLLECTIONS = (Collection.GUIDANCE, Collection.DEER)
PA_COLLECTIONS = (Collection.PA, Collection.PROGRAM, Collection.EMV)
OTHER_COLLECTIONS = (Collection.EMV,)

# The file types a document may carry, with the type it is served as. Executables and web pages are refused (the PDA holds 16 .exe main
# files and 3 file names containing HTML: capture of 2026-10-30). Only the INLINE types open in the browser; the rest download.
FILE_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
INLINE = {".pdf", ".txt", ".png", ".jpg", ".jpeg"}
SEARCH_FIELDS = ("q", "collection", "document_type", "pa", "program", "year", "options", "published_from", "published_to", "status")


class DocumentError(ValueError):
    """A document that cannot be filed or changed as asked; str(e) is the sentence the page shows."""


# --- who may do what


def _cpuc_has(user, *permissions) -> bool:
    org = access.cpuc()
    return org is not None and any(access.has_permission(user, org, p) for p in permissions)


def is_admin(user) -> bool:
    """CPUC ED's document administrators: every document, the value lists. None is the system."""
    if user is None:
        return True
    return user.is_authenticated and (access.is_system_admin(user) or _cpuc_has(user, ADMIN))


def _parties(doc: Document) -> list[Organisation]:
    return [doc.owner, *[o for o in doc.pas.all() if o.pk != doc.owner_id]]


def _party_has(user, doc: Document, *permissions) -> bool:
    if user is None or not user.is_authenticated:
        return False
    return any(access.has_permission(user, org, p) for org in _parties(doc) for p in permissions)


def sees_unpublished(user, doc: Document) -> bool:
    if user is None:
        return True
    return user.is_authenticated and (is_admin(user) or _cpuc_has(user, *ANY) or _party_has(user, doc, *ANY))


def can_read(user, doc: Document) -> bool:
    return doc.published_at is not None or sees_unpublished(user, doc)


def can_manage(user, doc: Document) -> bool:
    """Add versions and change the details."""
    return is_admin(user) or _party_has(user, doc, SUBMIT)


def can_publish(user, doc: Document) -> bool:
    return is_admin(user) or _party_has(user, doc, CONFIRM)


def collections_for(user, organisation: Organisation | None) -> list[str]:
    """The collections the user may file new documents in while acting for the organisation."""
    if organisation is None or not organisation.active:
        return []
    if user is not None and not (access.has_permission(user, organisation, SUBMIT) or (organisation.kind == "cpuc" and is_admin(user))):
        return []
    if organisation.kind == Organisation.Kind.CPUC:
        return [c.value for c in Collection]
    if organisation.kind == Organisation.Kind.PA:
        return [c.value for c in PA_COLLECTIONS]
    return [c.value for c in OTHER_COLLECTIONS]


def visible_versions(user, doc: Document) -> list[DocumentVersion]:
    """Newest first; published ones only for readers who do not see unpublished work."""
    qs = doc.versions.select_related("organisation", "created_by", "published_by").prefetch_related("files")
    if not sees_unpublished(user, doc):
        qs = qs.filter(published_at__isnull=False)
    return list(qs.order_by("-number"))


def current_version(user, doc: Document) -> DocumentVersion | None:
    """The version the permanent link serves: the newest published one, or for those who see unpublished work the newest of all when
    nothing is published yet."""
    versions = visible_versions(user, doc)
    published = [v for v in versions if v.published]
    return published[0] if published else (versions[0] if versions else None)


# --- value lists


def type_list(collection: str) -> AttributeList | None:
    return AttributeList.objects.filter(type_of=collection).first()


def attribute_lists(collection: str) -> list[AttributeList]:
    """The lists a collection's documents carry, besides its document types."""
    return list(AttributeList.objects.filter(collections__contains=[collection]).exclude(type_of=collection).prefetch_related("options"))


def offered(attribute_list: AttributeList, day: date | None = None) -> list[AttributeOption]:
    day = day or timezone.localdate()
    return [o for o in attribute_list.options.all() if o.offered_on(day)]


def _require_admin(actor, what: str) -> None:
    if not is_admin(actor):
        raise AccessDenied(f"Only CPUC ED's document administrators can {what}.")


def add_option(actor, attribute_list: AttributeList, label: str, *, reason: str = "") -> AttributeOption:
    _require_admin(actor, "change the value lists")
    label = label.strip()
    if not label:
        raise DocumentError("Give the new value a label.")
    if attribute_list.options.filter(label__iexact=label).exists():
        raise DocumentError(f"{attribute_list.name} already has {label}.")
    position = (attribute_list.options.aggregate(m=Max("position"))["m"] or 0) + 1
    option = AttributeOption.objects.create(attribute_list=attribute_list, label=label, position=position)
    audit.record("documents.option_added", obj=option, after=audit.snapshot(option), reason=reason, actor=actor)
    return option


def rename_option(actor, option: AttributeOption, label: str, *, reason: str) -> AttributeOption:
    """A correction of the label (a spelling); every document carrying it shows the new label. A different meaning is a new option."""
    _require_admin(actor, "change the value lists")
    label = label.strip()
    if not label or not reason.strip():
        raise DocumentError("Give the new label and the reason for the change.")
    if option.attribute_list.options.filter(label__iexact=label).exclude(pk=option.pk).exists():
        raise DocumentError(f"{option.attribute_list.name} already has {label}.")
    before = audit.snapshot(option)
    option.label = label
    option.save(update_fields=["label"])
    audit.record("documents.option_renamed", obj=option, before=before, after=audit.snapshot(option), reason=reason, actor=actor)
    return option


def retire_option(actor, option: AttributeOption, *, on: date | None = None, reason: str) -> AttributeOption:
    """Stop offering an option from a date. Documents filed with it keep it, and search still finds them by it."""
    _require_admin(actor, "change the value lists")
    if not reason.strip():
        raise DocumentError("Give the reason for retiring it.")
    before = audit.snapshot(option)
    option.retired_on = on or timezone.localdate()
    option.save(update_fields=["retired_on"])
    audit.record("documents.option_retired", obj=option, before=before, after=audit.snapshot(option), reason=reason, actor=actor)
    return option


def reinstate_option(actor, option: AttributeOption, *, reason: str) -> AttributeOption:
    _require_admin(actor, "change the value lists")
    before = audit.snapshot(option)
    option.retired_on = None
    option.save(update_fields=["retired_on"])
    audit.record("documents.option_reinstated", obj=option, before=before, after=audit.snapshot(option), reason=reason, actor=actor)
    return option


# --- files


@dataclass
class Upload:
    """A file on its way into a version: its name as given and a readable binary file (a Django UploadedFile, or any open file)."""

    name: str
    file: object


def clean_name(name: str) -> str:
    """The file's own name (no folders), without characters that break a download header or a storage key."""
    base = PurePath(str(name).replace("\\", "/")).name
    base = re.sub(r'[\x00-\x1f"<>|:*?/\\]+', "_", base).strip(" .")
    return base[:200] or "file"


def file_type(name: str) -> str:
    """The type a file is served as, from its extension; DocumentError when the extension is not accepted."""
    ext = Path(name).suffix.lower()
    if ext not in FILE_TYPES:
        accepted = ", ".join(sorted(FILE_TYPES))
        raise DocumentError(f"{name}: files of type {ext or '(none)'} are not accepted. Accepted: {accepted}.")
    return FILE_TYPES[ext]


def opens_inline(f: DocumentFile) -> bool:
    return Path(f.name).suffix.lower() in INLINE


def _max_bytes() -> int:
    return int(getattr(settings, "DOCUMENTS_MAX_FILE_MB", 200)) * 1024 * 1024


def _check_uploads(uploads: list[Upload]) -> list[tuple[str, str]]:
    if not uploads:
        raise DocumentError("Add the main file.")
    names = [clean_name(u.name) for u in uploads]
    if len(set(n.lower() for n in names)) != len(names):
        raise DocumentError("Two files have the same name; rename one.")
    out = []
    for name, u in zip(names, uploads, strict=True):
        content_type = file_type(name)
        size = getattr(u.file, "size", None)
        if size is not None and size > _max_bytes():
            raise DocumentError(f"{name} is larger than the {settings.DOCUMENTS_MAX_FILE_MB} MB a file may be.")
        out.append((name, content_type))
    return out


def _store_files(version: DocumentVersion, uploads: list[Upload], checked: list[tuple[str, str]]) -> list[DocumentFile]:
    store = storage.store("uploads")
    rows = []
    for position, (u, (name, content_type)) in enumerate(zip(uploads, checked, strict=True)):
        key = f"documents/{version.document_id}/v{version.number}/{position}/{name}"
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            if hasattr(u.file, "seek"):
                u.file.seek(0)
            chunks = u.file.chunks() if hasattr(u.file, "chunks") else iter(lambda f=u.file: f.read(1 << 20), b"")
            size = 0
            for chunk in chunks:
                digest.update(chunk)
                tmp.write(chunk)
                size += len(chunk)
        try:
            if size > _max_bytes():
                raise DocumentError(f"{name} is larger than the {settings.DOCUMENTS_MAX_FILE_MB} MB a file may be.")
            if size == 0:
                raise DocumentError(f"{name} is empty.")
            store.put_file(key, Path(tmp.name))
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        rows.append(
            DocumentFile.objects.create(
                version=version, position=position, name=name, key=key, sha256=digest.hexdigest(), size=size, content_type=content_type
            )
        )
    return rows


# --- filing and changing documents


@dataclass
class Details:
    """A document's details as a form gives them."""

    document_type: AttributeOption
    title: str
    summary: str = ""
    pas: list[Organisation] = field(default_factory=list)
    programs: list[Program] = field(default_factory=list)
    first_year: int | None = None
    last_year: int | None = None
    attributes: list[AttributeOption] = field(default_factory=list)
    study: Study | None = None
    work_order: WorkOrder | None = None
    task: Task | None = None


STUDY_COLLECTIONS = (Collection.EMV, Collection.PROGRAM)


def _check_links(actor, collection: str, d: Details, doc: Document | None = None) -> None:
    """A document joins a study (EM&V documents and program files) only by someone who may update that study; a work order and task
    only on EM&V documents, the task one of the work order's (BR-15.3)."""
    from .studies import can_edit

    if d.study is not None:
        if collection not in STUDY_COLLECTIONS:
            raise DocumentError("Only EM&V documents and program files belong to a study.")
        if (doc is None or doc.study_id != d.study.pk) and not can_edit(actor, d.study):
            raise AccessDenied(f"Only {d.study.manager.code} (the study manager) and CPUC ED add documents to {d.study.study_id}.")
    if (d.work_order is not None or d.task is not None) and collection != Collection.EMV:
        raise DocumentError("Only EM&V documents are filed under a work order.")
    if d.task is not None and d.task.work_order_id != getattr(d.work_order, "pk", None):
        raise DocumentError(f"The task {d.task.name} is not one of the chosen work order's tasks.")


def _check_details(collection: str, d: Details, *, keeping: set[int] = frozenset()) -> None:
    """keeping: option ids the document already carries, which stay valid even once retired."""
    today = timezone.localdate()
    if not d.title.strip():
        raise DocumentError("Give the document a title.")
    if len(d.summary) > 3500:
        raise DocumentError("Keep the summary to 3,500 characters.")
    types = type_list(collection)
    if types is None or d.document_type.attribute_list_id != types.pk:
        raise DocumentError(f"Choose a document type of {Collection(collection).label}.")
    if not d.document_type.offered_on(today) and d.document_type.pk not in keeping:
        raise DocumentError(f"{d.document_type.label} is retired; choose another document type.")
    if any(o.kind != Organisation.Kind.PA for o in d.pas):
        raise DocumentError("Only program administrators can be named as the PAs a document applies to.")
    if d.first_year and d.last_year and d.first_year > d.last_year:
        raise DocumentError("The first year comes after the last year.")
    allowed = {a.pk: a for a in attribute_lists(collection)}
    chosen: dict[int, list[AttributeOption]] = {}
    for o in d.attributes:
        if o.attribute_list_id not in allowed:
            raise DocumentError(f"{o.attribute_list.name} is not used in {Collection(collection).label}.")
        if not o.offered_on(today) and o.pk not in keeping:
            raise DocumentError(f"{o.attribute_list.name}: {o.label} is retired.")
        chosen.setdefault(o.attribute_list_id, []).append(o)
    for list_id, options in chosen.items():
        if len(options) > 1 and not allowed[list_id].multiple:
            raise DocumentError(f"Choose one {allowed[list_id].name.lower()}.")


def _detail_snapshot(doc: Document) -> dict:
    snap = audit.snapshot(doc, ["collection", "document_type", "title", "summary", "owner", "first_year", "last_year"])
    snap["pas"] = sorted(o.code for o in doc.pas.all())
    snap["programs"] = sorted(p.prg_id for p in doc.programs.all())
    snap["attributes"] = sorted(doc.attributes.values_list("pk", flat=True))
    snap["study"] = doc.study.study_id if doc.study else ""
    snap["work_order"] = doc.work_order.number if doc.work_order else ""
    snap["task"] = doc.task.name if doc.task else ""
    return snap


def _require_organisation(actor, organisation: Organisation | None, collection: str) -> None:
    if organisation is None:
        raise AccessDenied("Choose the organisation you are acting for before you file a document.")
    if collection not in collections_for(actor, organisation):
        allowed = collections_for(actor, organisation)
        if not allowed:
            raise AccessDenied(f"You need the Documents: submit permission in {organisation.code} to file documents.")
        raise AccessDenied(f"{organisation.code} cannot file {Collection(collection).label.lower()}.")


@transaction.atomic
def create_document(
    actor, organisation: Organisation, collection: str, details: Details, uploads: list[Upload], *, publish: bool = False
) -> Document:
    """File a document with its first version. A PA's own document names the PA itself among the PAs it applies to."""
    _require_organisation(actor, organisation, collection)
    _check_details(collection, details)
    _check_links(actor, collection, details)
    checked = _check_uploads(uploads)
    doc = Document.objects.create(
        collection=collection,
        document_type=details.document_type,
        title=details.title.strip(),
        summary=details.summary.strip(),
        owner=organisation,
        first_year=details.first_year,
        last_year=details.last_year,
        study=details.study,
        work_order=details.work_order,
        task=details.task,
        created_by=actor,
    )
    pas = list(details.pas)
    if organisation.kind == Organisation.Kind.PA and organisation not in pas:
        pas.insert(0, organisation)
    doc.pas.set(pas)
    doc.programs.set(details.programs)
    doc.attributes.set(details.attributes)
    audit.record("documents.filed", obj=doc, after=_detail_snapshot(doc), actor=actor, organisation=organisation)
    version = _new_version(actor, organisation, doc, uploads, checked, reason="")
    if publish:
        publish_version(actor, version)
        doc.refresh_from_db()
    return doc


@transaction.atomic
def change_details(actor, doc: Document, details: Details, *, reason: str) -> Document:
    """Change what is recorded about a document (not its files: those are versions). Audited with the before and after."""
    if not can_manage(actor, doc):
        raise AccessDenied("You cannot change this document.")
    if not reason.strip():
        raise DocumentError("Give the reason for the change.")
    keeping = set(doc.attributes.values_list("pk", flat=True)) | {doc.document_type_id}
    _check_details(doc.collection, details, keeping=keeping)
    _check_links(actor, doc.collection, details, doc)
    before = _detail_snapshot(doc)
    doc.document_type = details.document_type
    doc.title = details.title.strip()
    doc.summary = details.summary.strip()
    doc.first_year, doc.last_year = details.first_year, details.last_year
    doc.study, doc.work_order, doc.task = details.study, details.work_order, details.task
    doc.save()
    pas = list(details.pas)
    if doc.owner.kind == Organisation.Kind.PA and doc.owner not in pas:
        pas.insert(0, doc.owner)
    doc.pas.set(pas)
    doc.programs.set(details.programs)
    doc.attributes.set(details.attributes)
    after = _detail_snapshot(doc)
    if after != before:
        audit.record("documents.details_changed", obj=doc, before=before, after=after, reason=reason, actor=actor)
    return doc


def _new_version(actor, organisation, doc: Document, uploads, checked, *, reason: str) -> DocumentVersion:
    number = (doc.versions.aggregate(m=Max("number"))["m"] or 0) + 1
    version = DocumentVersion.objects.create(document=doc, number=number, reason=reason, organisation=organisation, created_by=actor)
    files = _store_files(version, uploads, checked)
    audit.record(
        "documents.version_added",
        obj=version,
        after={"number": number, "files": [{"name": f.name, "sha256": f.sha256, "size": f.size} for f in files]},
        reason=reason,
        actor=actor,
        organisation=organisation,
    )
    return version


@transaction.atomic
def add_version(
    actor, organisation: Organisation | None, doc: Document, uploads: list[Upload], *, reason: str, publish: bool = False
) -> DocumentVersion:
    """Replace a document's files with a new version; earlier versions stay listed and downloadable (BR-15.1, BR-09.6)."""
    if not can_manage(actor, doc):
        raise AccessDenied("You cannot add versions to this document.")
    if not reason.strip():
        raise DocumentError("Say why this version replaces the previous one.")
    checked = _check_uploads(uploads)
    Document.objects.select_for_update().get(pk=doc.pk)
    version = _new_version(actor, organisation or doc.owner, doc, uploads, checked, reason=reason.strip())
    if publish:
        publish_version(actor, version)
    return version


@transaction.atomic
def publish_version(actor, version: DocumentVersion) -> DocumentVersion:
    """Publish a version: it becomes public and never changes again. The document's published date is its first publication's."""
    doc = Document.objects.select_for_update().get(pk=version.document_id)
    if not can_publish(actor, doc):
        raise AccessDenied("You need the Documents: confirm permission of an organisation on this document to publish it.")
    version = DocumentVersion.objects.select_for_update().get(pk=version.pk)
    if version.published:
        raise DocumentError(f"Version {version.number} is already published.")
    now = timezone.now()
    version.published_at, version.published_by = now, actor
    version.save(update_fields=["published_at", "published_by"])
    if doc.published_at is None:
        doc.published_at = now
        doc.save(update_fields=["published_at", "updated_at"])
    audit.record("documents.version_published", obj=version, after={"number": version.number, "published_at": now}, actor=actor)
    return version


def record_download(user, f: DocumentFile, *, viewed: bool = False) -> None:
    """Every download (or view in the browser) is an audit event, replacing the PDA's Deliverable Download Report (BR-15.9)."""
    who = user if user is not None and user.is_authenticated else None
    audit.record(
        "documents.file_viewed" if viewed else "documents.file_downloaded",
        obj=f,
        object_repr=f"{f.version.document.title} v{f.version.number}: {f.name}"[:300],
        actor=who,
        actor_label="public",
    )


def downloads(doc: Document):
    """The download log of a document's files, newest first."""
    from cedars.services.audit.models import AuditEvent

    ids = [str(pk) for pk in DocumentFile.objects.filter(version__document=doc).values_list("pk", flat=True)]
    return AuditEvent.objects.filter(
        object_type="documents.documentfile", object_id__in=ids, action__in=["documents.file_downloaded", "documents.file_viewed"]
    )


# --- search (BR-15.2)


@dataclass
class Search:
    q: str = ""
    collection: str = ""
    document_type: AttributeOption | None = None
    pa: Organisation | None = None
    program: str = ""
    year: int | None = None
    options: list[AttributeOption] = field(default_factory=list)
    published_from: date | None = None
    published_to: date | None = None
    status: str = ""  # "", "published", "unpublished" (the last only shows what the user may see)
    study: Study | None = None
    work_order: WorkOrder | None = None


def readable(user) -> QuerySet:
    """The documents the user may open: every published one, and the unpublished ones of their organisations (all of them for CPUC
    readers)."""
    qs = Document.objects.all()
    if user is not None and user.is_authenticated and (is_admin(user) or _cpuc_has(user, *ANY)):
        return qs
    mine = []
    if user is not None and user.is_authenticated:
        mine = [o.pk for o in access.organisations_of(user) if access.effective_permissions(user, o) & set(ANY)]
    return qs.filter(Q(published_at__isnull=False) | Q(owner__in=mine) | Q(pas__in=mine)).distinct()


def search(user, s: Search) -> QuerySet:
    """The documents a search keeps, newest publication first. Each list's chosen options must all match a document's options in that
    list (any of them, within the list)."""
    qs = readable(user)
    if s.q:
        terms = s.q.split()
        for term in terms:
            qs = qs.filter(Q(title__icontains=term) | Q(summary__icontains=term) | Q(versions__files__name__icontains=term))
    if s.collection:
        qs = qs.filter(collection=s.collection)
    if s.document_type is not None:
        qs = qs.filter(document_type=s.document_type)
    if s.pa is not None:
        qs = qs.filter(Q(pas=s.pa) | Q(owner=s.pa))
    if s.program:
        qs = qs.filter(programs__prg_id__iexact=s.program.strip())
    if s.year:
        # a document covers the years from its first to its last; one with only one of them, that year
        qs = qs.filter(
            Q(first_year__lte=s.year, last_year__gte=s.year)
            | Q(first_year=s.year, last_year__isnull=True)
            | Q(first_year__isnull=True, last_year=s.year)
        )
    by_list: dict[int, list[int]] = {}
    for o in s.options:
        by_list.setdefault(o.attribute_list_id, []).append(o.pk)
    for ids in by_list.values():
        qs = qs.filter(pk__in=Document.objects.filter(attributes__in=ids).values("pk"))
    if s.study is not None:
        qs = qs.filter(study=s.study)
    if s.work_order is not None:
        qs = qs.filter(work_order=s.work_order)
    if s.published_from:
        qs = qs.filter(published_at__date__gte=s.published_from)
    if s.published_to:
        qs = qs.filter(published_at__date__lte=s.published_to)
    if s.status == "published":
        qs = qs.filter(published_at__isnull=False)
    elif s.status == "unpublished":
        qs = qs.filter(published_at__isnull=True)
    return (
        Document.objects.filter(pk__in=qs.values("pk"))
        .select_related("document_type", "owner", "study", "work_order", "task")
        .prefetch_related(
            "pas",
            "programs",
            Prefetch("attributes", queryset=AttributeOption.objects.select_related("attribute_list")),
            Prefetch("versions", queryset=DocumentVersion.objects.order_by("-number").prefetch_related("files")),
        )
    )


def listed_version(doc: Document, *, unpublished: bool) -> DocumentVersion | None:
    """The version a result row shows, from the prefetched versions: the newest published, else (for those who see it) the newest."""
    versions = list(doc.versions.all())
    published = [v for v in versions if v.published]
    if published:
        return published[0]
    return versions[0] if versions and unpublished else None
