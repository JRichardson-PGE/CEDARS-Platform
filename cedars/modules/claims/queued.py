"""Claims' queued checks (submissions.queued): an upload, a Check again or a PII upload too large to check while the page waits is
checked by a worker with these functions, exactly as the request would have checked it, as the person who sent it."""

from cedars.services.submissions import filing_parts
from cedars.services.submissions import queued as submissions_queued
from cedars.services.submissions import service as submissions
from cedars.services.vault.service import PIIError

from . import service, spec
from .upload import UploadError
from .upload import combine as combine_upload

NEXT = "Price the year to date next."


def _said(v) -> dict:
    level, text = filing_parts.checked_message(v, NEXT)
    return {"level": level, "text": text, "version": v.pk}


def upload(q, files) -> dict:
    c = combine_upload(files, spec.for_filing(q.deadline))
    return _said(service.upload_claims(q.created_by, q.deadline, q.organisation, c.data, c.filename, sources=c.sources))


def check(q, files) -> dict:
    return _said(service.check(q.created_by, q.version))


def finish(q, files) -> dict:
    v = submissions.finish_queued(q, service.finish_with)
    if v is None:
        return {
            "level": "info",
            "text": "The check had been replaced by a newer one, so there was nothing to finish.",
            "version": q.version_id,
        }
    return _said(v)


def pii(q, files) -> dict:
    name, content = files[0]
    up = service.upload_pii(q.created_by, q.version, content, name, q.assurance or None)
    if up.status == up.Status.ACCEPTED:
        return {"level": "success", "text": "The PII file passed its checks and is kept in the vault.", "version": q.version_id}
    return {
        "level": "warning",
        "text": f"The PII file was not kept: {len(up.findings):,} findings. Fix them and upload it again.",
        "version": q.version_id,
    }


def register() -> None:
    errors = (UploadError, PIIError)
    submissions_queued.register_queued_check("claims.upload", upload, label="claims upload", errors=errors)
    submissions_queued.register_queued_check("claims.check", check, label="check of the year to date", errors=errors)
    submissions_queued.register_queued_check("claims.pii", pii, label="PII upload", errors=errors)
    submissions_queued.register_queued_check("claims.finish", finish, label="final check with the rules' findings", errors=errors)
