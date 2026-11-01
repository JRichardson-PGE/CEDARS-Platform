from django.db import models


class Refresh(models.Model):
    """One sandbox refresh (BR-11.2): a bundle of production's published, non-PII data loaded into the sandbox. What each table gained,
    matched or left out is in `counts` and `skipped`; every row it added is in CopiedRecord."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    source = models.CharField(max_length=500, help_text="Where the bundle was read from")
    bundle_made_at = models.DateTimeField(null=True, blank=True, help_text="When production made the bundle")
    bundle_environment = models.CharField(max_length=20, blank=True)
    bundle_platform = models.CharField(max_length=40, blank=True, help_text="The platform release that made it")
    bundle_sha256 = models.CharField(max_length=64, blank=True, help_text="The SHA-256 of the bundle's manifest")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    counts = models.JSONField(default=dict, blank=True, help_text="{table: {added, matched, updated, skipped}}")
    skipped = models.JSONField(default=list, blank=True, help_text="[{table, key, why}] rows not copied, and why")
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Sandbox refresh of {self.started_at:%Y-%m-%d %H:%M}" if self.started_at else "Sandbox refresh"


class CopiedRecord(models.Model):
    """A row a sandbox refresh copied from production, under its production key. A later refresh finds it again by that key, and the
    sandbox's own clean-up (the sandbox_data retention class) leaves copied filings alone: they are production's, not a tester's."""

    table = models.CharField(max_length=60, help_text="The model, e.g. registry.program")
    key = models.CharField(max_length=500, help_text="Its production key (the bundle's natural key, as JSON)")
    local_id = models.CharField(max_length=64, help_text="Its primary key in the sandbox")
    refresh = models.ForeignKey(Refresh, on_delete=models.PROTECT, related_name="copied")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["table", "key"], name="sandbox_one_copy_per_key")]
        indexes = [models.Index(fields=["table", "local_id"], name="sandbox_copied_local")]

    def __str__(self):
        return f"{self.table} {self.key}"
