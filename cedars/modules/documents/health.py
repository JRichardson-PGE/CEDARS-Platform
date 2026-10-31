"""The Documents module's health check (cedars/core/health.py): files whose text has not been read for search yet."""

from cedars.core import health

from .models import DocumentFile


def unread_text() -> health.Result:
    n = DocumentFile.objects.filter(text__isnull=True).count()
    result = health.Result("documents.text", "Documents", "Files not yet searchable by their text", link="/documents/")
    if n:
        result.status = health.ATTENTION
        result.summary = f"{n} file{'s' if n != 1 else ''} stored without their text; the daily documents_read_text job reads them."
    else:
        result.summary = "Every stored file's text has been read (or why it cannot be is recorded)."
    return result


def register() -> None:
    health.register("documents.text", unread_text)
