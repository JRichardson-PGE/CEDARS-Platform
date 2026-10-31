from django.conf import settings
from django.db import models


class LegacySource(models.Model):
    """One of legacy CEDARS' record-level download files, staged in object storage (uploads area, legacy/cedars/...) after its SHA-256
    and size were checked against the download manifest. Never changed: a later download that differs is a new source."""

    dataset = models.CharField(max_length=20, help_text="claims, filings or filings_archive")
    year = models.CharField(max_length=20, help_text="As CEDARS names it: 2016, 2024_to_2025, 2024_2027 ...")
    pa = models.CharField("PA", max_length=20, help_text="A PA code, 'all' (the all-PA file) or 'SW' (statewide unallocated)")
    name = models.CharField(max_length=200)
    url = models.URLField(max_length=400)
    sha256 = models.CharField(max_length=64)
    bytes = models.BigIntegerField()
    key = models.CharField(max_length=400, help_text="Its key in the uploads storage area")
    members = models.JSONField(default=dict, help_text="{CSV inside the zip: its layout}")
    staged_at = models.DateTimeField(auto_now_add=True)
    staged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["dataset", "year", "pa", "-staged_at"]
        constraints = [models.UniqueConstraint(fields=["name", "sha256"], name="legacy_one_source_per_content")]

    def __str__(self):
        return self.name


class LegacyImport(models.Model):
    """One organization's filing or claim year loaded from legacy CEDARS: the imported version it became, the files it came from, the
    record count and the reconciliation report. A year that fails the hard checks is not published, so every row here is a published
    imported version."""

    version = models.OneToOneField("submissions.SubmissionVersion", on_delete=models.PROTECT, related_name="legacy_import")
    module = models.CharField(max_length=40, help_text="budgets or claims")
    period = models.CharField(max_length=40, help_text="The claim year or the filing, as the source names it")
    sources = models.ManyToManyField(LegacySource, related_name="imports")
    records = models.PositiveIntegerField(default=0, help_text="Records counted in the totals")
    outside_totals = models.PositiveIntegerField(default=0, help_text="Records kept but not counted (statewide unallocated claims)")
    reconciliation = models.JSONField(default=dict, help_text="The checks and their results, as the load wrote them")
    loaded_at = models.DateTimeField(auto_now_add=True)
    loaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["module", "period", "version__submission__organisation__code"]

    def __str__(self):
        return f"{self.module} {self.period} ({self.version.submission.organisation.code})"
