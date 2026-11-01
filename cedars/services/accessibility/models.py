from django.conf import settings
from django.db import models

from . import checks


class CheckSettings(models.Model):
    """The CPUC's settings for the document accessibility check (BR-10.3). One row; changed on the Document accessibility settings page
    with a reason, audited. By default nothing is refused: the uploader is warned and the file is listed for review. With `block` on,
    an upload with any finding in `blocking` is refused (imports from the legacy sites never are)."""

    block = models.BooleanField(default=False, help_text="Refuse an upload with a blocking finding, instead of warning")
    blocking = models.JSONField(default=list, help_text="The finding codes that refuse an upload when blocking is on (checks.CODES)")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Document accessibility settings"


class FileCheck(models.Model):
    """The check of one uploaded file: a document's file, a file attached to a public comment on a document, or one attached to a
    comment on a filing. Written when the file is stored (files never change, so neither does the check); a CPUC system administrator
    may mark it reviewed with a note (for example "an accessible copy is filed as version 2"). Only checked types (checks.checkable)
    get a row. The findings are checks.Finding dicts."""

    class Source(models.TextChoices):
        DOCUMENT = "document", "Document library"
        DOCUMENT_COMMENT = "document_comment", "Comment on a document"
        FILING_COMMENT = "filing_comment", "Comment on a filing"

    source = models.CharField(max_length=20, choices=Source.choices)
    ref = models.CharField(max_length=64, help_text="The stored file's id in its source (DocumentFile, Comment or Attachment)")
    url = models.CharField(max_length=500, blank=True, help_text="The page the file is on")
    title = models.CharField(max_length=300, blank=True, help_text="What the file belongs to (a document's title, a filing)")
    file_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    kind = models.CharField(max_length=10, help_text="pdf or workbook")
    findings = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=300, blank=True, help_text="Why the file could not be checked")
    organisation = models.ForeignKey("accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    imported = models.BooleanField(default=False, help_text="Brought over from a legacy site, not uploaded here")
    checked_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-checked_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["source", "ref"], name="accessibility_filecheck_source_ref")]

    def __str__(self):
        return f"Accessibility check of {self.file_name}"

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("accessibility:file", args=[self.pk])

    @property
    def finding_list(self) -> list[checks.Finding]:
        return [checks.Finding.from_dict(f) for f in self.findings if f.get("code") in checks.CODES]

    @property
    def flagged(self) -> bool:
        return bool(self.findings) or bool(self.note)

    @property
    def reviewed(self) -> bool:
        return self.reviewed_at is not None
