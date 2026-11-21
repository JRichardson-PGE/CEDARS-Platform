"""Budget filings' queued checks (submissions.queued): an upload too large to check while the page waits is checked by a worker with
this function, exactly as the request would have checked it, as the person who sent it."""

from cedars.services.accounts.service import AccessDenied
from cedars.services.submissions import filing_parts
from cedars.services.submissions import queued as submissions_queued
from cedars.services.submissions import service as submissions

from . import jobfile, service

NEXT = "Price it next."


def can_upload(actor, deadline, organisation) -> None:
    """What the upload would refuse before reading the file: a large upload is not queued only to be refused."""
    if not hasattr(deadline, "budget_setup"):
        raise AccessDenied("This filing is not set up for budgets.")
    submissions.start(actor, deadline, organisation)


def upload(q, files) -> dict:
    c = jobfile.combine(files)
    v = service.upload(q.created_by, q.deadline, q.organisation, c.data, c.filename, sources=c.sources)
    level, text = filing_parts.checked_message(v, NEXT)
    return {"level": level, "text": text, "version": v.pk}


def register() -> None:
    submissions_queued.register_queued_check("budgets.upload", upload, label="budget filing upload", errors=(jobfile.JobError,))
