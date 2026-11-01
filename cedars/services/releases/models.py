"""Release notes (BR-12.1): a dated notice of what changes, published before it takes effect.

A note is drafted, then published (it can no longer change: a database trigger refuses any edit, and its changes are fixed with it),
and may be withdrawn with a reason (it stays on the page, marked withdrawn). A changed plan is a new note. Each note lists its changes:
what kind (a specification, a rule, a limit or the system), which part of CEDARS, what changes and what users need to do; a limit
change names the limit (limits.LIMITS) and its new value, so the Limits page shows it as coming."""

from django.conf import settings
from django.db import models
from django.db.models import Q


class ReleaseNote(models.Model):
    title = models.CharField(max_length=200)
    version = models.CharField(
        "Platform release", max_length=20, blank=True, help_text="The release it ships in, e.g. 0.3.0; blank when no new code is needed"
    )
    summary = models.TextField(help_text="What changes and why, in a few sentences")
    effective_on = models.DateField("Takes effect on")
    short_notice_reason = models.TextField(blank=True, help_text="Why it is published with less than the usual notice")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    withdrawn_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-effective_on", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["version"], condition=~Q(version="") & Q(withdrawn_at__isnull=True), name="releases_one_note_per_version"
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.effective_on:%Y-%m-%d})"

    @property
    def is_draft(self) -> bool:
        return self.published_at is None

    @property
    def is_withdrawn(self) -> bool:
        return self.withdrawn_at is not None


class Change(models.Model):
    class Kind(models.TextChoices):
        SPECIFICATION = "specification", "Specification (fields, templates, value lists)"
        RULE = "rule", "Validation rules"
        LIMIT = "limit", "A limit"
        SYSTEM = "system", "How CEDARS works"

    note = models.ForeignKey(ReleaseNote, on_delete=models.CASCADE, related_name="changes")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    area = models.CharField("Part of CEDARS", max_length=20)  # a key of service.AREAS
    text = models.TextField("What changes")
    action = models.TextField("What you need to do", blank=True)
    limit = models.CharField(max_length=40, blank=True)  # a key of limits.LIMITS, for a limit change
    limit_value = models.CharField("New value", max_length=60, blank=True, help_text="As people will see it, e.g. 200 MB")
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["note", "position", "pk"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.text[:60]}"


class Mandate(models.Model):
    """An entry of the public register of CPUC-mandated data and calculation changes (BR-12.7): the decision or resolution, what it
    requires of CEDARS' data or calculations, when it takes effect and how far CEDARS has implemented it, with links to the rule set,
    dictionary version, value list version, field, data pack or release note that carries it.

    The register is public, so an entry holds only public CPUC information: the citation, the requirement as the document states it
    and a link to the document on a ca.gov site. A CPUC system administrator drafts an entry (only administrators see drafts) and
    publishes it; a published entry is never deleted (a database trigger refuses it) and every change to it is audited with a reason,
    which its page shows as its history. An entry entered in error is withdrawn, with the reason, and stays on the register."""

    class Authority(models.TextChoices):
        DECISION = "decision", "Decision"
        RESOLUTION = "resolution", "Resolution"
        RULING = "ruling", "Ruling"
        OTHER = "other", "Other CPUC direction"

    class Area(models.TextChoices):
        DATA = "data", "Data (fields, templates, value lists, rules)"
        CALCULATION = "calculation", "Calculations"
        BOTH = "both", "Data and calculations"

    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        IN_PROGRESS = "in_progress", "In progress"
        IMPLEMENTED = "implemented", "Implemented"
        SUPERSEDED = "superseded", "Superseded"
        WITHDRAWN = "withdrawn", "Withdrawn (entered in error)"

    reference = models.CharField("Decision or resolution", max_length=60, help_text="As the CPUC cites it, e.g. D.25-08-034 or Res. E-5350")
    authority = models.CharField("Kind", max_length=12, choices=Authority.choices, default=Authority.DECISION)
    title = models.CharField(max_length=200, help_text="What it is about, in a few words")
    issued_on = models.DateField("Issued on", null=True, blank=True)
    document_url = models.URLField(
        "Link to the document", max_length=400, blank=True, help_text="The public document on a ca.gov site (https), e.g. docs.cpuc.ca.gov"
    )
    requirement = models.TextField(help_text="What it requires of CEDARS' data or calculations, as the document states it")
    area = models.CharField("Changes", max_length=12, choices=Area.choices, default=Area.DATA)
    effective_on = models.DateField("Takes effect on")
    status = models.CharField("Implementation status", max_length=12, choices=Status.choices, default=Status.NOT_STARTED)
    status_note = models.TextField("About the status", blank=True, help_text="What is done, what is left, or why it is superseded")
    implemented_on = models.DateField("Implemented on", null=True, blank=True)
    superseded_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="supersedes")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-effective_on", "reference", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status="implemented") | Q(implemented_on__isnull=False), name="releases_implemented_mandate_has_date"
            )
        ]

    def __str__(self):
        return f"{self.reference}: {self.title}"

    @property
    def is_draft(self) -> bool:
        return self.published_at is None

    @property
    def in_force(self) -> bool:
        """Still a requirement: neither superseded nor withdrawn."""
        return self.status not in (self.Status.SUPERSEDED, self.Status.WITHDRAWN)


class MandateLink(models.Model):
    """Where CEDARS carries a mandate: a published rule set, dictionary version, value list version, a field, a data pack (a cost-test
    set is one) or a release note, named by kind and key and checked to exist and be public when linked (mandates.LINK_KINDS)."""

    mandate = models.ForeignKey(Mandate, on_delete=models.CASCADE, related_name="links")
    kind = models.CharField(max_length=20)  # a key of mandates.LINK_KINDS
    key = models.CharField(max_length=120, help_text="The record's id, or a field's name")
    label = models.CharField(max_length=300, help_text="What it is, as it was when linked")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["mandate", "kind", "pk"]
        constraints = [models.UniqueConstraint(fields=["mandate", "kind", "key"], name="releases_one_link_per_record")]

    def __str__(self):
        return self.label
