import uuid

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
