from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


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


class SavedView(models.Model):
    """A saved view (BR-07.4): a dashboard page's or the records page's query (its GET parameters, columns included) under a name, so its
    owner can open it again on the data published now. The owner may share it with named people and with the members of one of their
    organizations; a CPUC system administrator may publish it for everyone, signed in or not. A published view does not change: its
    owner changes or deletes it only after it is unpublished. Every change is an audit event (`data/saved.py`)."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="data_views")
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True, max_length=500)
    page = models.CharField(max_length=40, help_text='"records", or the slug of a dashboard page')
    query = models.TextField(blank=True, help_text="The page's query string, without the release, so the view opens on current data")
    organisation = models.ForeignKey(
        "accounts.Organisation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Shared with this organization's active members (one the owner belongs to)",
    )
    shared_with = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="data_views_shared")
    publish_requested_at = models.DateTimeField(null=True, blank=True, help_text="When the owner asked the CPUC to publish it")
    publish_request_note = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    publish_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [Lower("name"), "pk"]
        constraints = [
            models.UniqueConstraint("owner", Lower("name"), name="data_view_name_per_owner"),
            models.CheckConstraint(
                condition=Q(published_at__isnull=True, published_by__isnull=True)
                | Q(published_at__isnull=False, published_by__isnull=False),
                name="data_view_published_by_someone",
            ),
        ]
        indexes = [models.Index(fields=["page", "published_at"])]

    def __str__(self):
        return self.name

    @property
    def published(self) -> bool:
        return self.published_at is not None


class RecordColumnDefault(models.Model):
    """The columns a person's records page starts with when a link names none (BR-07.4: each user's own column view), instead of the
    key columns. Names the release does not have are left out and said so."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="record_column_default")
    columns = models.JSONField(default=list, help_text="Canonical field names, in the catalog's order")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user}: {len(self.columns)} columns"
