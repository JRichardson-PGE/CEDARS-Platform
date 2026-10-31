from django.conf import settings
from django.db import models


class DownloadJob(models.Model):
    """A record download too large to make while the person waits (BR-07.2): queued, made by `manage.py run_data_downloads`, kept in
    the results storage area under `downloads/<id>/`, and announced through the notification service. Only the person who asked sees
    it; the file goes when its retention period ("data_download") ends."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Being made"
        DONE = "done", "Ready"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="data_downloads")
    dataset = models.CharField(max_length=20)
    query = models.TextField(help_text="The records page's query string: the records and columns, as a link reproduces them")
    fmt = models.CharField("format", max_length=10)
    records = models.PositiveBigIntegerField(help_text="How many records matched when it was asked for")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    key = models.CharField(max_length=300, blank=True, help_text="The file in the results storage area")
    filename = models.CharField(max_length=200, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    rows = models.PositiveBigIntegerField(null=True, blank=True, help_text="Records written")
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.dataset} records download #{self.pk} ({self.status})"
