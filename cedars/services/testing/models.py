from django.conf import settings
from django.db import models
from django.db.models import Q


class ScriptVersion(models.Model):
    """One loading of the test scripts (manage.py load_test_scripts): the scripts as they stood, never edited. An edited script is
    loaded again under a new label, and a round of testing keeps the version it was opened with, so a result always says which wording
    of a test it answers. The scripts are data on this site only: they are never part of the code."""

    label = models.CharField(max_length=80, unique=True, help_text="A name for this loading, e.g. 2026-11-12")
    site = models.CharField(max_length=200, blank=True, help_text="The site address the scripts' steps were written with")
    about = models.JSONField(
        default=dict, help_text="What the file says about itself: how to test, priorities, the build it was written for"
    )
    sha256 = models.CharField(max_length=64)
    loaded_at = models.DateTimeField(auto_now_add=True)
    loaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-loaded_at", "-pk"]

    def __str__(self):
        return self.label


class Script(models.Model):
    """One script of a version: the tests for one kind of user."""

    version = models.ForeignKey(ScriptVersion, on_delete=models.CASCADE, related_name="scripts")
    code = models.CharField(max_length=12, help_text="The prefix of its tests' IDs, e.g. PAF")
    number = models.CharField(max_length=6, blank=True)
    title = models.CharField(max_length=200)
    who = models.TextField(blank=True, help_text="Who should run it")
    scope = models.TextField(blank=True)
    accounts = models.JSONField(default=list, help_text="The test accounts it uses: [name, what it is]")
    before = models.JSONField(default=list, help_text="What must be in place before the script starts")
    order_notes = models.JSONField(default=list)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["version", "position", "pk"]
        constraints = [models.UniqueConstraint(fields=["version", "code"], name="testing_one_script_per_code")]

    def __str__(self):
        return f"{self.number} {self.title}".strip()


class TestCase(models.Model):
    """One test of a script: the account to use, what must be true first, the steps and the one expected result."""

    script = models.ForeignKey(Script, on_delete=models.CASCADE, related_name="tests")
    test_id = models.CharField(max_length=20, help_text="e.g. PAF-023")
    section = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=300)
    priority = models.CharField(max_length=4, blank=True, help_text="P1 must pass, P2 should pass, P3 nice to have")
    account = models.CharField(max_length=100, blank=True, help_text="The test account to sign in as (blank or None: signed out)")
    precondition = models.TextField(blank=True)
    steps = models.JSONField(default=list)
    expected = models.TextField()
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["script", "position", "pk"]
        constraints = [models.UniqueConstraint(fields=["script", "test_id"], name="testing_one_test_per_id")]

    def __str__(self):
        return f"{self.test_id} {self.title}"


class Campaign(models.Model):
    """One round of testing (a release's acceptance test, a retest): a name, the script version it uses and its dates. Results are
    taken while it is open; closing it keeps them as they stand."""

    name = models.CharField(max_length=120, unique=True)
    version = models.ForeignKey(ScriptVersion, on_delete=models.PROTECT, related_name="campaigns")
    note = models.TextField(blank=True, help_text="What is being tested, for the testers")
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-starts_on", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gte=models.F("starts_on")), name="testing_campaign_ends_after_start"
            )
        ]

    def __str__(self):
        return self.name

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class Assignment(models.Model):
    """One script of a round of testing given to one person's own account. The tester's results hang from it. Its run link lets
    the tester's browser keep the run open while they are signed in to the site as a test account, or signed out; `link_number` goes
    up when the tester asks for a new link, which stops the old one."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="assignments")
    script = models.ForeignKey(Script, on_delete=models.PROTECT, related_name="assignments")
    tester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="test_assignments")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField(auto_now_add=True)
    link_number = models.PositiveIntegerField(default=1)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    withdrawn_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["campaign", "script__position", "tester__email", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "script", "tester"], condition=Q(withdrawn_at__isnull=True), name="testing_one_assignment_per_tester"
            )
        ]

    def __str__(self):
        return f"{self.script} for {self.tester} ({self.campaign})"

    @property
    def is_live(self) -> bool:
        return self.withdrawn_at is None and self.campaign.is_open


class Result(models.Model):
    """A tester's answer to one test of their assignment: the outcome, what happened and a note against any step. One per test;
    saving again replaces it (the audit trail keeps each change of outcome)."""

    class Outcome(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        BLOCKED = "blocked", "Blocked"
        NOT_APPLICABLE = "na", "Does not apply"

    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="results")
    test = models.ForeignKey(TestCase, on_delete=models.PROTECT, related_name="results")
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    comment = models.TextField(blank=True)
    step_notes = models.JSONField(default=list, help_text="A note per step, in the steps' order (blank where none)")
    recorded_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["assignment", "test__position", "pk"]
        constraints = [models.UniqueConstraint(fields=["assignment", "test"], name="testing_one_result_per_test")]

    def __str__(self):
        return f"{self.test.test_id}: {self.get_outcome_display()}"

    @property
    def needs_attention(self) -> bool:
        return self.outcome in (self.Outcome.FAILED, self.Outcome.BLOCKED)


class Finding(models.Model):
    """A failed or blocked result sent to the requests inbox as a request of kind "finding", from the tester: the same inbox and
    the same conversation as any request, and it can be joined to a work item. One per result; a result that fails again after its
    finding was closed gets a new request. `feedback` empties when the request's retention period ends."""

    result = models.OneToOneField(Result, on_delete=models.CASCADE, related_name="finding")
    feedback = models.OneToOneField("feedback.Feedback", null=True, blank=True, on_delete=models.SET_NULL, related_name="test_finding")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Finding from {self.result}"


class Draft(models.Model):
    """What a tester has typed into one test's form and not yet saved as a result: kept as they type, so a closed tab or a sign-in as
    a test account loses nothing. Not a result (it counts nowhere and nobody else reads it); saving the result removes it."""

    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="drafts")
    test = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name="drafts")
    outcome = models.CharField(max_length=10, blank=True, choices=Result.Outcome.choices)
    comment = models.TextField(blank=True)
    step_notes = models.JSONField(default=list, help_text="A note per step, in the steps' order (blank where none)")
    saved_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["assignment", "test"], name="testing_one_draft_per_test")]

    def __str__(self):
        return f"{self.test.test_id}: draft"
