from django.conf import settings
from django.db import models
from django.db.models import Q


class RetentionPolicy(models.Model):
    """How long one class of record is kept (decision M). keep_days None = permanent; 0 = not kept once superseded. Changed on the
    Retention page with a reason, never in code."""

    record_class = models.CharField(max_length=40, unique=True)
    label = models.CharField(max_length=120)
    keep_days = models.PositiveIntegerField(null=True, blank=True)
    counted_from = models.CharField(max_length=200, help_text="the event the period starts from")
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["record_class"]

    def __str__(self):
        return self.label

    @property
    def period(self) -> str:
        if self.keep_days is None:
            return "Permanent"
        if self.keep_days == 0:
            return "Not kept"
        if self.keep_days % 365 == 0:
            years = self.keep_days // 365
            return f"{years} year{'s' if years > 1 else ''}"
        return f"{self.keep_days} days"


class LegalHold(models.Model):
    """Nothing under a hold is deleted, whatever its retention period: one record (object_ref) or a whole class (blank)."""

    record_class = models.CharField(max_length=40)
    object_ref = models.CharField(max_length=120, blank=True, help_text="blank: every record of the class")
    reason = models.TextField()
    placed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    placed_at = models.DateTimeField(auto_now_add=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-placed_at"]
        indexes = [models.Index(fields=["record_class", "object_ref"], condition=Q(released_at__isnull=True), name="retention_active_hold")]

    def __str__(self):
        return f"hold on {self.record_class} {self.object_ref or '(all)'}"


class PurgeRun(models.Model):
    """One run of the retention job: what each class deleted, or would have (dry run)."""

    started_at = models.DateTimeField(auto_now_add=True)
    dry_run = models.BooleanField(default=False)
    counts = models.JSONField(default=dict, help_text="{record_class: {deleted, held}}")

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"purge {self.started_at:%Y-%m-%d %H:%M}{' (dry run)' if self.dry_run else ''}"
