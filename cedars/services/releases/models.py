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
