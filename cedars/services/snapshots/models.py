import uuid

from django.conf import settings
from django.db import models


class Dataset(models.TextChoices):
    CLAIMS = "claims", "Claims"
    FORECAST = "forecast", "Forecast (budget filings)"
    MONTHLY = "monthly", "Monthly reports"


class Snapshot(models.Model):
    """One published version's measure-level results, frozen in the snapshots storage area (`snapshots/<dataset>/<id>.parquet`) exactly
    as the run wrote them. Never changed. A version un-confirmed before its due date loses its snapshot (decision M: what is superseded
    before the due date is not kept)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset = models.CharField(max_length=10, choices=Dataset.choices)
    version = models.OneToOneField("submissions.SubmissionVersion", on_delete=models.PROTECT, related_name="snapshot")
    organisation = models.ForeignKey("accounts.Organisation", on_delete=models.PROTECT, related_name="+")
    year = models.PositiveSmallIntegerField(null=True, blank=True, help_text="The claim or report year (claims and monthly)")
    run = models.CharField(max_length=64, help_text="The calculation run the version was confirmed on")
    source_key = models.CharField(max_length=400, help_text="The run's measure-level parquet in the results area")
    key = models.CharField(max_length=400, help_text="Its key in the snapshots area")
    sha256 = models.CharField(max_length=64)
    bytes = models.BigIntegerField()
    rows = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["dataset", "-created_at"]

    def __str__(self):
        return f"{self.get_dataset_display()} snapshot of {self.version_id}"


class Release(models.Model):
    """The snapshots in force for one dataset from `created_at` until the next release. Never changed: each publication or withdrawal
    makes a new release, numbered per dataset, so a page or download names exactly what it read (BR-07.2)."""

    dataset = models.CharField(max_length=10, choices=Dataset.choices)
    number = models.PositiveIntegerField()
    snapshots = models.ManyToManyField(Snapshot, related_name="releases", blank=True)
    reason = models.CharField(max_length=300, help_text="What changed, e.g. 'PGE 2026Q2 claim version 1 published'")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["dataset", "-number"]
        constraints = [models.UniqueConstraint(fields=["dataset", "number"], name="snapshots_one_release_number")]

    def __str__(self):
        return f"{self.get_dataset_display()} release {self.number}"


class ScanSettings(models.Model):
    """The CPUC's settings for the publication scan (BR-08.5): which patterns run and which columns are left out (keys such as
    CEInputID that are never personal information). One row; changed on the Publication scan page with a reason, audited."""

    patterns = models.JSONField(default=list, help_text="The codes of the patterns that run (pii.PATTERNS)")
    exempt_columns = models.JSONField(default=list, help_text="Column names the scan leaves out, compared without case")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Publication scan settings"


class PIIScan(models.Model):
    """The publication scan of one snapshot. A snapshot with a scan that is `held` is in no release until a CPUC system administrator
    releases it with a reason, a scan run again comes back clear, or a newer version replaces it. Snapshots made before the scan existed
    have none and stand as before. The findings never hold the values found (pii.Finding: masked examples and record IDs only)."""

    class Outcome(models.TextChoices):
        CLEAR = "clear", "Clear"
        HELD = "held", "Held"
        RELEASED = "released", "Released by an administrator"

    snapshot = models.OneToOneField(Snapshot, on_delete=models.CASCADE, related_name="pii_scan")
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    findings = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, help_text="Why the file could not be scanned (the publication is held)")
    columns = models.JSONField(default=list, blank=True, help_text="The text columns searched")
    skipped = models.JSONField(default=list, blank=True, help_text="Text columns the settings left out")
    patterns = models.JSONField(default=list, blank=True, help_text="The patterns that ran")
    scanned_at = models.DateTimeField()
    alerted = models.PositiveIntegerField(default=0, help_text="How many people were told of the hold")
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-scanned_at"]

    def __str__(self):
        return f"Publication scan of {self.snapshot}: {self.get_outcome_display().lower()}"

    @property
    def held(self) -> bool:
        return self.outcome == self.Outcome.HELD

    @property
    def records(self) -> int:
        return sum(f.get("records", 0) for f in self.findings)
