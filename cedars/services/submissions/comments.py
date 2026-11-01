"""Comments and attachments on filing versions (BR-09.5), for Budgets, Claims and Monthly reports alike: the filing organization and CPUC
staff discuss a filing on its version pages, with files attached.

Who: the filing organization's people who may read its filings in the module (`<module>.read`, `.submit` or `.confirm` there), and CPUC
staff who may (`<module>.read` or `.approve` in the CPUC, a CPUC system administrator, or an approver's delegate for the module today).
A comment is written for one side: the filing organization, or the CPUC. Comments are never public, even on a published version (the
version's results and files are; the discussion is not), and never edited: the database refuses (migration 0004), so a correction is
another comment. The conversation runs across a filing's versions: each version page shows every comment on the submission, marked with
the version it was written on, and a new one goes on the version being read.

Files are checked and kept as the document library's are (`cedars.core.files`: the same accepted types and size limit, the uploads storage
area), under the version. Posting a comment and downloading a file each write an audit event on the version (`submission.commented`,
`submission.attachment_downloaded`), so the version's audit history shows them with its other events (BR-09.1)."""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.urls import NoReverseMatch, reverse

from cedars.core import files as filetypes
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied

from .models import Attachment, Comment, Submission, SubmissionVersion

ORG_ACTIONS = ("read", "submit", "confirm")
CPUC_ACTIONS = ("read", "approve")


class CommentRefused(ValueError):
    """A comment that cannot be posted as given; str(e) is the sentence the page shows."""


def max_chars() -> int:
    return settings.CEDARS_COMMENT_MAX_CHARS


def max_files() -> int:
    return settings.CEDARS_UPLOAD_MAX_FILES


def max_mb() -> int:
    return settings.DOCUMENTS_MAX_FILE_MB


def side(user, submission: Submission) -> Organisation | None:
    """The side the user speaks for on this filing: its organization if they may read its filings there, else the CPUC if they are
    CPUC staff for the module, else None (they may not take part)."""
    if user is None or not user.is_authenticated:
        return None
    module, org = submission.deadline.module, submission.organisation
    if any(access.has_permission(user, org, f"{module}.{a}") for a in ORG_ACTIONS):
        return org
    cpuc = access.cpuc()
    if cpuc is None:
        return None
    if access.is_system_admin(user) or any(access.has_permission(user, cpuc, f"{module}.{a}") for a in CPUC_ACTIONS):
        return cpuc
    from .service import approver_for

    return cpuc if approver_for(user, module) is not None else None


def can_discuss(user, submission: Submission) -> bool:
    return side(user, submission) is not None


def thread(submission: Submission) -> QuerySet:
    """Every comment on the submission, oldest first, with its version and files."""
    return (
        Comment.objects.filter(version__submission=submission)
        .select_related("version", "organisation")
        .prefetch_related("attachments")
        .order_by("created_at", "pk")
    )


def version_page(v: SubmissionVersion) -> str:
    """The module's page for the version (each filing module names it "<module>:version"), or "" where there is none."""
    try:
        return reverse(f"{v.submission.deadline.module}:version", args=[v.pk])
    except NoReverseMatch:
        return ""


def _author_name(user, org: Organisation) -> str:
    """The name shown with a comment: never an email address."""
    return (user.name or "").strip() or f"A {org.code} user"


@transaction.atomic
def post(actor, v: SubmissionVersion, text: str, uploads: list[tuple[str, object]] = ()) -> Comment:
    """Post a comment on the version, with files (name, readable binary file). The files are checked before anything is kept."""
    v = SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation").get(pk=v.pk)
    if v.imported:
        raise CommentRefused("This version was imported from legacy CEDARS; comment on a version filed on this platform.")
    org = side(actor, v.submission)
    if org is None:
        raise AccessDenied("Only the filing organization's people and CPUC staff comment on a filing.")
    text = (text or "").strip()
    if not text:
        raise CommentRefused("Write the comment (say what the attached files are, if you attach any).")
    if len(text) > max_chars():
        raise CommentRefused(f"Keep a comment to {max_chars():,} characters (see Limits, /limits/); post a longer one in parts.")
    uploads = list(uploads)
    if len(uploads) > max_files():
        raise CommentRefused(f"Attach at most {max_files()} files to one comment (see Limits, /limits/).")
    names = [filetypes.clean_name(n) for n, _ in uploads]
    if len({n.lower() for n in names}) != len(names):
        raise CommentRefused("Two files have the same name; rename one.")
    try:
        for n in names:
            filetypes.content_type(n)
    except filetypes.FileRefused as e:
        raise CommentRefused(str(e)) from e
    comment = Comment.objects.create(version=v, author=actor, author_name=_author_name(actor, org), organisation=org, text=text)
    kept = []
    try:
        for position, (name, (_, f)) in enumerate(zip(names, uploads, strict=True)):
            folder = f"submissions/versions/{v.pk}/comments/{comment.pk}/{position}"
            s = filetypes.store(folder, name, f, max_mb())
            kept.append(
                Attachment.objects.create(
                    comment=comment, position=position, name=s.name, key=s.key, sha256=s.sha256, size=s.size, content_type=s.content_type
                )
            )
    except filetypes.FileRefused as e:
        raise CommentRefused(str(e)) from e
    audit.record(
        "submission.commented",
        obj=v,
        after={
            "comment": comment.pk,
            "side": org.code,
            "text": text,
            "files": [{"name": a.name, "sha256": a.sha256, "bytes": a.size} for a in kept],
        },
        actor=actor,
        organisation=org,
    )
    return comment


def attachment_for(user, pk: int) -> Attachment:
    """The attachment, if the user may take part in its filing's discussion; AccessDenied otherwise."""
    a = Attachment.objects.select_related("comment__version__submission__deadline", "comment__version__submission__organisation").get(pk=pk)
    if not can_discuss(user, a.comment.version.submission):
        raise AccessDenied("Only the filing organization's people and CPUC staff open the files on its comments.")
    return a


def record_download(user, a: Attachment) -> None:
    v = a.comment.version
    audit.record(
        "submission.attachment_downloaded",
        obj=v,
        after={"comment": a.comment_id, "file": a.name, "sha256": a.sha256},
        actor=user,
        organisation=side(user, v.submission),
    )


def awaiting_reply(user) -> list[tuple[Submission, Comment]]:
    """Filings whose latest comment came from the other side, for the user's queue: for the filing organization's people who may submit
    or confirm, the CPUC wrote last; for the module's CPUC approvers (and their delegates), the filing organization did. Oldest first."""
    if user is None or not user.is_authenticated:
        return []
    from .service import approver_for

    out = []
    latest = (  # each submission's latest comment (PostgreSQL's DISTINCT ON)
        Comment.objects.order_by("version__submission_id", "-created_at", "-pk")
        .distinct("version__submission_id")
        .select_related("version__submission__deadline", "version__submission__organisation", "organisation")
    )
    approver_modules: dict[str, bool] = {}
    for c in latest:
        sub = c.version.submission
        module, org = sub.deadline.module, sub.organisation
        if c.organisation_id != org.pk:  # the CPUC wrote last: the filer's turn
            mine = any(access.has_permission(user, org, f"{module}.{a}") for a in ("submit", "confirm"))
        else:
            if module not in approver_modules:
                approver_modules[module] = approver_for(user, module) is not None
            mine = approver_modules[module]
        if mine:
            out.append((sub, c))
    return sorted(out, key=lambda x: x[1].created_at)
