from django.conf import settings
from django.db import models


class Feedback(models.Model):
    """One message sent from a page's Feedback link (BR-12.4): the page it was sent from, who sent it (blank: signed out) and the
    organization they were acting for, and the message. CPUC system administrators are told of each one and work through them on the
    Feedback page; closing one records who closed it, when and a note. A message is never edited or deleted by hand: it goes when its
    retention period ends (record class "feedback")."""

    class Status(models.TextChoices):
        NEW = "new", "New"
        CLOSED = "closed", "Closed"

    page = models.CharField(max_length=500, help_text="The page's path and query, as the sender saw it")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    organisation = models.ForeignKey(
        "accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="Who the sender acted for"
    )
    message = models.TextField()
    sender_key = models.CharField(
        max_length=64,
        blank=True,
        help_text="A keyed hash of a signed-out sender's address, only to limit how often one sends (never the address)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, help_text="What was done about it")
    notified = models.PositiveIntegerField(default=0, help_text="How many CPUC system administrators were told")

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name_plural = "feedback"
        indexes = [models.Index(fields=["status", "-created_at"], name="feedback_status_created")]

    def __str__(self):
        return f"Feedback on {self.page[:80]} ({self.created_at:%Y-%m-%d})" if self.created_at else f"Feedback on {self.page[:80]}"
