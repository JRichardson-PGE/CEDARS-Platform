from django.conf import settings
from django.db import models
from django.db.models import Q


class Deadline(models.Model):
    """One entry in the filing calendar: what a module expects, for which period or cycle, and when it is due (BR-09.4)."""

    module = models.CharField(max_length=40)
    kind = models.CharField(max_length=80, help_text="e.g. Q1 claim, TUAL, monthly report")
    period = models.ForeignKey("registry.ReportingPeriod", null=True, blank=True, on_delete=models.PROTECT, related_name="deadlines")
    cycle = models.ForeignKey("registry.FilingCycle", null=True, blank=True, on_delete=models.PROTECT, related_name="deadlines")
    opens_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField()
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["due_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(period__isnull=False) | Q(cycle__isnull=False), name="submissions_deadline_has_period_or_cycle"
            ),
            models.UniqueConstraint(fields=["module", "kind", "period", "cycle"], name="submissions_one_deadline", nulls_distinct=False),
        ]

    def __str__(self):
        what = self.period or self.cycle
        return f"{self.module} {self.kind} {what} due {self.due_at:%Y-%m-%d}"


class WorkflowPolicy(models.Model):
    """A module's workflow settings, changed without a release. reopen_after_due is the correction policy (BRD D1)."""

    class Reopen(models.TextChoices):
        APPROVAL = "approval", "Allowed with an approver's approval"
        OPEN = "open", "Allowed with a reason, no approval"
        CLOSED = "closed", "Not allowed (no edits after close-out)"

    module = models.CharField(max_length=40, unique=True)
    reopen_after_due = models.CharField(max_length=10, choices=Reopen.choices, default=Reopen.APPROVAL)
    late_confirmation = models.BooleanField(
        default=True, help_text="A first version may still be confirmed after the due date (marked late)"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "workflow policies"

    def __str__(self):
        return f"{self.module} workflow"


class Submission(models.Model):
    """One organization's filing for one deadline: a series of versions."""

    deadline = models.ForeignKey(Deadline, on_delete=models.PROTECT, related_name="submissions")
    organisation = models.ForeignKey("accounts.Organisation", on_delete=models.PROTECT, related_name="submissions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["deadline__due_at", "organisation__code"]
        constraints = [models.UniqueConstraint(fields=["deadline", "organisation"], name="submissions_one_per_org_and_deadline")]

    def __str__(self):
        return f"{self.organisation.code} {self.deadline}"

    @property
    def module(self) -> str:
        return self.deadline.module


class SubmissionVersion(models.Model):
    """Version n of a submission. Its rows and files belong to it; once confirmed it is never edited (a correction is version n+1)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        VALIDATED = "validated", "Validated"
        CONFIRMED = "confirmed", "Confirmed"
        PUBLISHED = "published", "Published"

    class Origin(models.TextChoices):
        FILED = "filed", "Filed on this platform"
        IMPORTED = "imported", "Imported from legacy CEDARS"

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    origin = models.CharField(
        max_length=10,
        choices=Origin.choices,
        default=Origin.FILED,
        help_text="Imported versions are legacy CEDARS' years as it published them: read-only, never reopened or corrected",
    )
    reason = models.TextField(blank=True, help_text="Why this version exists (every version after the first has one)")
    files = models.JSONField(default=list, blank=True, help_text="[{name, key, sha256, bytes}] in object storage")
    rule_set = models.ForeignKey("rules.RuleSet", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    findings = models.JSONField(default=dict, blank=True, help_text="The latest validation's summary: records, rejects, warnings")
    pack_versions = models.JSONField(default=dict, blank=True, help_text="Reference data the version was priced with")
    calculation = models.JSONField(default=dict, blank=True, help_text="The run it was priced with: {run, input_hash, finished_at}")
    late = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["submission", "number"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "number"], name="submissions_one_number"),
            models.CheckConstraint(condition=Q(origin="filed") | Q(status="published"), name="submissions_imported_is_published"),
        ]

    def __str__(self):
        return f"{self.submission} v{self.number}"

    @property
    def locked(self) -> bool:
        return self.status in (self.Status.CONFIRMED, self.Status.PUBLISHED)

    @property
    def imported(self) -> bool:
        return self.origin == self.Origin.IMPORTED


class ReopenRequest(models.Model):
    """A request to correct a submission after its due date: only this organization and deadline, with a reason (BR-05.2, BR-09.5)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="reopen_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_as_delegate_of = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"reopen {self.submission} ({self.status})"


class Delegation(models.Model):
    """An approver's delegate for a module, for a period (BR-09.5: approvals with delegates)."""

    module = models.CharField(max_length=40)
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    delegate = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    starts = models.DateField()
    ends = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts"]

    def __str__(self):
        return f"{self.delegate} for {self.approver} ({self.module})"
