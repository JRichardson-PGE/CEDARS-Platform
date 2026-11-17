from django.conf import settings
from django.db import models


class Feedback(models.Model):
    """One request sent from a page's Feedback link: a question, a problem, an idea or a comment. It keeps what the sender would
    otherwise be asked for: the page it was sent from, who sent it (blank: signed out) and the organization they were acting for,
    the site's version and its environment. CPUC system administrators are told of each one and work through them on the Feedback
    page: they answer it, ask the sender a question, or close it with a note; the conversation is its `Reply` rows. A request is
    never edited or deleted by hand: it goes when its retention period ends (record class "feedback")."""

    class Kind(models.TextChoices):
        HELP = "help", "A question"
        PROBLEM = "problem", "A problem"
        IDEA = "idea", "An idea"
        COMMENT = "comment", "A comment"
        BARRIER = "barrier", "An accessibility barrier"
        SECURITY = "security", "A security concern"
        FINDING = "finding", "A test finding"  # made by the Testing service from a failed or blocked result, never from the form

    class Status(models.TextChoices):
        NEW = "new", "Received"
        LOOKING = "looking", "Being looked at"
        WAITING = "waiting", "Waiting for you"  # the CPUC asked the sender a question
        ANSWERED = "answered", "Answered"
        CLOSED = "closed", "Closed"

    OPEN = (Status.NEW, Status.LOOKING, Status.WAITING, Status.ANSWERED)
    TO_ANSWER = (Status.NEW, Status.LOOKING)  # waiting on the CPUC
    URGENT = (Kind.BARRIER, Kind.SECURITY)  # told of as urgent and listed first
    # what the sender's organization's administrators may read (unless the sender keeps it from them): never comments, ideas or
    # security concerns
    FOR_ORGANISATION = (Kind.HELP, Kind.PROBLEM, Kind.BARRIER)

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.COMMENT)

    page = models.CharField(max_length=500, help_text="The page's path and query, as the sender saw it")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    organisation = models.ForeignKey(
        "accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="Who the sender acted for"
    )
    message = models.TextField()
    private = models.BooleanField(default=False, help_text="The sender kept it from their organization's administrators")
    sender_key = models.CharField(
        max_length=64,
        blank=True,
        help_text="A keyed hash of a signed-out sender's address, only to limit how often one sends (never the address)",
    )
    site_version = models.CharField(max_length=30, blank=True, help_text="The site's version when it was sent")
    environment = models.CharField(max_length=30, blank=True, help_text="The site's environment when it was sent (sandbox, production)")
    created_at = models.DateTimeField(auto_now_add=True)
    active_at = models.DateTimeField(null=True, blank=True, help_text="When it was sent or last replied to")
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

    @property
    def is_open(self) -> bool:
        return self.status != self.Status.CLOSED

    @property
    def is_urgent(self) -> bool:
        return self.kind in self.URGENT


class Reply(models.Model):
    """One entry in a request's conversation: the CPUC's answer, the CPUC's question to the sender, the sender's reply, or a note
    the CPUC's staff keep for themselves (never shown to the sender). Never edited; it goes with its request."""

    class Kind(models.TextChoices):
        ANSWER = "answer", "Answer"
        QUESTION = "question", "Question"
        REPLY = "reply", "Reply"
        NOTE = "note", "Staff note"

    FROM_STAFF = (Kind.ANSWER, Kind.QUESTION, Kind.NOTE)

    feedback = models.ForeignKey(Feedback, on_delete=models.CASCADE, related_name="replies")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name_plural = "replies"

    def __str__(self):
        return f"{self.get_kind_display()} on feedback {self.feedback_id}"
