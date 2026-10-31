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


PROGRAM_EVENTS = [  # what a followed program's followers may hear about (the settings page's keys of service.EVENTS)
    "submission.published:budgets",
    "fund_shift.effective",
    "submission.published:monthly",
    "submission.published:claims",
    "program.changed",
    "documents.version_published",
]


def all_program_events() -> list[str]:
    return list(PROGRAM_EVENTS)


class Preference(models.Model):
    """Per-user settings beside the subscriptions: messages about the user's own runs and access, and what the user follows (BR-09.2).

    The PAs followed (pa_scope: every PA, the ones chosen, or none) narrow each subscribed event that concerns a PA. The programs
    followed add to the subscriptions: whatever the PA choice, the user hears of the program_events that concern a program they follow
    (a filing or a correction that includes it, a fund shift that moves its funds, a change to its record, a document about it)."""

    class PAScope(models.TextChoices):
        ALL = "all", "Every PA"
        CHOSEN = "chosen", "Only the PAs I choose"
        NONE = "none", "No PA (I hear only about the programs I follow)"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference")
    email_direct = models.BooleanField(default=True, help_text="Email me when my runs finish or fail and when my access changes")
    pa_scope = models.CharField("PAs followed", max_length=10, choices=PAScope.choices, default=PAScope.ALL)
    pas = models.ManyToManyField("accounts.Organisation", blank=True, related_name="+", help_text="The PAs chosen (pa_scope chosen)")
    programs = models.ManyToManyField("registry.Program", blank=True, related_name="followers", help_text="The programs followed")
    program_events = models.JSONField(default=all_program_events, blank=True, help_text="What the programs' followers hear about")
    program_channel = models.CharField(max_length=10, choices=Subscription.Channel.choices, default=Subscription.Channel.EMAIL)

    def __str__(self):
        return f"{self.user} notification preference"


class DeadlineReminder(models.Model):
    """A user's reminder of the filing calendar's deadlines, a chosen number of days before each is due (0: on the day), for one module
    or every module. A user may keep several (e.g. 14 days and 2 days before). Each reminder is sent once per deadline and due date."""

    MAX_DAYS = 365

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="deadline_reminders")
    module = models.CharField(max_length=40, blank=True, help_text="blank: every module")
    days_before = models.PositiveSmallIntegerField(help_text="How many days before the due date (0: on the day)")
    channel = models.CharField(max_length=10, choices=Subscription.Channel.choices, default=Subscription.Channel.EMAIL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user", "-days_before", "module"]
        constraints = [
            models.UniqueConstraint(fields=["user", "module", "days_before"], name="notifications_one_reminder"),
            models.CheckConstraint(condition=Q(days_before__lte=365), name="notifications_reminder_within_a_year"),
        ]

    def __str__(self):
        return f"{self.user} {self.days_before} days before {self.module or 'every'} deadline"


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
