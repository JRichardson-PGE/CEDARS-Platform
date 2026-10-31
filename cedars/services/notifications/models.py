from django.conf import settings
from django.db import models
from django.db.models import Q


class Subscription(models.Model):
    """A user's wish to hear about one kind of event, optionally for one module or organization only (BR-09.2, BR-12.1)."""

    class Channel(models.TextChoices):
        IN_APP = "in_app", "On the notifications page"
        EMAIL = "email", "On the notifications page and by email"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions")
    event = models.CharField(max_length=60)
    module = models.CharField(max_length=40, blank=True, help_text="blank: every module")
    organisation = models.ForeignKey(
        "accounts.Organisation", null=True, blank=True, on_delete=models.CASCADE, related_name="+", help_text="blank: all I may see"
    )
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.EMAIL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event", "module", "organisation"], name="notifications_one_subscription", nulls_distinct=False
            )
        ]

    def __str__(self):
        return f"{self.user} {self.event}"


class Preference(models.Model):
    """Per-user settings for notifications that are not subscriptions: messages about the user's own runs and access."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference")
    email_direct = models.BooleanField(default=True, help_text="Email me when my runs finish or fail and when my access changes")

    def __str__(self):
        return f"{self.user} notification preference"


class Notification(models.Model):
    """One message to one user, from an audit event they subscribed to, a reminder, or a module writing to them directly."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    event = models.ForeignKey("audit.AuditEvent", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    key = models.CharField(max_length=120, blank=True, help_text="sends a reminder once per user (e.g. deadline:12:7)")
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    email = models.BooleanField(default=False)
    emailed_at = models.DateTimeField(null=True, blank=True)
    email_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "read_at"])]
        constraints = [models.UniqueConstraint(fields=["user", "key"], condition=~Q(key=""), name="notifications_once_per_key")]

    def __str__(self):
        return f"{self.user}: {self.title}"
