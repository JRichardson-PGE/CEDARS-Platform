"""Public comment (BR-15.4), replacing the PDA's comments, questions and Comment and Resolution Report: a registered user comments on or
asks about a published version while its comment period is open; CPUC ED, the PAs on the document and the study's evaluator respond;
everything is public, dated and attributed to its author and organisation (never an email address). The author may withdraw a comment
until it is answered. Comments and responses are never edited (the database refuses: migration 0007)."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied

from .models import Comment, CommentPeriod, CommentResponse, Document, DocumentVersion
from .service import (
    ADMIN,
    SUBMIT,
    DocumentError,
    Upload,
    _check_period,
    _check_uploads,
    _max_bytes,
    _parties,
    can_manage,
    can_read,
    is_admin,
)

REGISTERED = "Registered user"


def is_open(version: DocumentVersion, day: date | None = None) -> bool:
    period = getattr(version, "comment_period", None)
    return version.published and period is not None and period.open_on(day or timezone.localdate())


def open_versions(versions: list[DocumentVersion], day: date | None = None) -> list[DocumentVersion]:
    return [v for v in versions if is_open(v, day)]


def can_comment(user, version: DocumentVersion, day: date | None = None) -> bool:
    return user is not None and user.is_authenticated and is_open(version, day) and can_read(user, version.document)


def shown(version_or_doc) -> QuerySet:
    """The comments shown (not withdrawn), oldest first, with their responses."""
    qs = (
        Comment.objects.filter(withdrawn_at__isnull=True)
        .select_related("organisation", "version")
        .prefetch_related("responses__organisation")
    )
    if isinstance(version_or_doc, Document):
        return qs.filter(version__document=version_or_doc)
    return qs.filter(version=version_or_doc)


def _name(user) -> str:
    return (user.name or "").strip() or REGISTERED


def _author_organisation(user, organisation: Organisation | None) -> Organisation | None:
    """The organisation a comment is posted for: the acting one, if the user belongs to it."""
    if organisation is None:
        return None
    return organisation if organisation in access.organisations_of(user) else None


def _store_attachment(version: DocumentVersion, upload: Upload) -> dict:
    """Store a comment's file under its version, in a folder named by its content's hash (the comment row is written once, after)."""
    ((name, content_type),) = _check_uploads([upload])
    digest, size = hashlib.sha256(), 0
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        if hasattr(upload.file, "seek"):
            upload.file.seek(0)
        chunks = upload.file.chunks() if hasattr(upload.file, "chunks") else iter(lambda f=upload.file: f.read(1 << 20), b"")
        for chunk in chunks:
            digest.update(chunk)
            tmp.write(chunk)
            size += len(chunk)
    try:
        if size == 0:
            raise DocumentError(f"{name} is empty.")
        if size > _max_bytes():
            raise DocumentError(f"{name} is larger than a file may be.")
        key = f"documents/{version.document_id}/v{version.number}/comments/{digest.hexdigest()[:16]}/{name}"
        storage.store("uploads").put_file(key, Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return {"file_name": name, "file_key": key, "file_sha256": digest.hexdigest(), "file_size": size, "file_type": content_type}


@dataclass
class NewComment:
    kind: str
    subject: str
    text: str
    section: str = ""
    attachment: Upload | None = None


@transaction.atomic
def post(actor, organisation: Organisation | None, version: DocumentVersion, c: NewComment) -> Comment:
    """Post a comment or question on a version whose comment period is open."""
    if actor is None or not actor.is_authenticated:
        raise AccessDenied("Sign in to comment.")
    if not can_comment(actor, version):
        raise DocumentError("Comments on this version are closed.")
    if c.kind not in Comment.Kind.values:
        raise DocumentError("Say whether it is a comment or a question.")
    if not c.subject.strip() or not c.text.strip():
        raise DocumentError("Give the subject and the comment.")
    if len(c.subject.strip()) > 100 or len(c.section.strip()) > 100 or len(c.text.strip()) > 2000:
        raise DocumentError("Keep the subject and the section to 100 characters and the comment to 2,000.")
    file = _store_attachment(version, c.attachment) if c.attachment is not None else {}
    comment = Comment.objects.create(
        version=version,
        kind=c.kind,
        subject=c.subject.strip(),
        section=c.section.strip(),
        text=c.text.strip(),
        author=actor,
        author_name=_name(actor),
        organisation=_author_organisation(actor, organisation),
        **file,
    )
    audit.record(
        "documents.comment_posted",
        obj=comment,
        after={"version": version.number, "kind": comment.kind, "subject": comment.subject, "file": comment.file_name},
        actor=actor,
        organisation=comment.organisation,
    )
    return comment


def can_withdraw(user, comment: Comment) -> bool:
    return (
        user is not None
        and user.is_authenticated
        and comment.author_id == user.pk
        and comment.withdrawn_at is None
        and not comment.responses.exists()
    )


@transaction.atomic
def withdraw(actor, comment: Comment) -> None:
    """The author takes back a comment nobody has answered yet. It stops being shown; the row and its audit event stay."""
    comment = Comment.objects.select_for_update().get(pk=comment.pk)
    if not can_withdraw(actor, comment):
        raise DocumentError("Only its author withdraws a comment, and only until it is answered.")
    comment.withdrawn_at = timezone.now()
    comment.save(update_fields=["withdrawn_at"])
    audit.record("documents.comment_withdrawn", obj=comment, before={"subject": comment.subject}, actor=actor)


def responder_organisations(user, doc: Document) -> list[Organisation]:
    """The organisations the user may respond for: the CPUC (ED staff with documents.submit or .admin), and the document's PAs, owner and
    study manager where the user holds documents.submit."""
    if user is None or not user.is_authenticated:
        return []
    out = []
    cpuc = access.cpuc()
    if cpuc is not None and (is_admin(user) or access.has_permission(user, cpuc, SUBMIT) or access.has_permission(user, cpuc, ADMIN)):
        out.append(cpuc)
    out += [o for o in _parties(doc) if o not in out and access.has_permission(user, o, SUBMIT)]
    return out


@transaction.atomic
def respond(actor, organisation: Organisation, comment: Comment, text: str) -> CommentResponse:
    if comment.withdrawn_at is not None:
        raise DocumentError("That comment was withdrawn.")
    allowed = responder_organisations(actor, comment.version.document)
    if organisation not in allowed:
        if not allowed:
            raise AccessDenied("Only CPUC ED, the PAs on the document and the study's evaluator respond to comments.")
        organisation = allowed[0]
    text = text.strip()
    if not text:
        raise DocumentError("Write the response.")
    if len(text) > 2000:
        raise DocumentError("Keep the response to 2,000 characters.")
    response = CommentResponse.objects.create(comment=comment, text=text, author=actor, author_name=_name(actor), organisation=organisation)
    audit.record("documents.comment_answered", obj=comment, after={"response": response.pk}, actor=actor, organisation=organisation)
    return response


@transaction.atomic
def set_period(actor, version: DocumentVersion, opens: date, closes: date, *, reason: str = "") -> CommentPeriod:
    """Open a version's comment period, or move its dates (an extension, an early close) with a reason. Set by the document's managers."""
    if not can_manage(actor, version.document):
        raise AccessDenied("Only those who manage the document set its comment periods.")
    opens, closes = _check_period((opens, closes))
    period = CommentPeriod.objects.filter(version=version).first()
    if period is None:
        period = CommentPeriod.objects.create(version=version, opens=opens, closes=closes)
        audit.record(
            "documents.comment_period_set", obj=version, after={"opens": str(opens), "closes": str(closes)}, reason=reason, actor=actor
        )
        return period
    if not reason.strip():
        raise DocumentError("Give the reason for changing the comment period.")
    before = {"opens": str(period.opens), "closes": str(period.closes)}
    period.opens, period.closes = opens, closes
    period.save(update_fields=["opens", "closes"])
    audit.record(
        "documents.comment_period_set",
        obj=version,
        before=before,
        after={"opens": str(opens), "closes": str(closes)},
        reason=reason,
        actor=actor,
    )
    return period


# --- the comment-and-response report (the PDA's Comment and Resolution Report)

REPORT_COLUMNS = [
    "WorkOrder",
    "StudyID",
    "DocumentID",
    "Document",
    "Version",
    "Stage",
    "CommentID",
    "Type",
    "Subject",
    "Section",
    "Comment",
    "Author",
    "AuthorOrganisation",
    "CommentDate",
    "Attachment",
    "Response",
    "Responder",
    "ResponderOrganisation",
    "ResponseDate",
]


def report(comments: QuerySet, base_url: str) -> list[list]:
    """One row per response (a comment with none has one row with the response columns blank), in document and date order."""
    rows = []
    comments = comments.select_related("version__document__study", "version__document__work_order", "organisation").order_by(
        "version__document_id", "version__number", "created_at", "id"
    )
    for c in comments:
        doc = c.version.document
        head = [
            doc.work_order.number if doc.work_order else "",
            doc.study.study_id if doc.study else "",
            doc.pk,
            doc.title,
            c.version.number,
            c.version.stage_label,
            c.pk,
            c.get_kind_display(),
            c.subject,
            c.section,
            c.text,
            c.author_name,
            c.organisation.code if c.organisation else "",
            c.created_at.date().isoformat(),
            f"{base_url}{c.get_file_url()}" if c.file_name else "",
        ]
        answers = list(c.responses.all())
        for r in answers or [None]:
            rows.append(
                head
                + ([r.text, r.author_name, r.organisation.code, r.created_at.date().isoformat()] if r is not None else ["", "", "", ""])
            )
    return rows
