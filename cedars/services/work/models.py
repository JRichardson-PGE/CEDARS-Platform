from django.conf import settings
from django.db import models


class ReleasePlan(models.Model):
    """What a coming release is to hold: work items taken from the top of the backlog. A backlog manager fills it and proposes
    it; it is the plan once the CPUC approves it, and a change to its work items after that needs approval again. Where testing is
    on, a test lead opens a round of testing for its acceptance tests (`campaign`, the last one opened)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Being filled"
        PROPOSED = "proposed", "Waiting for approval"
        APPROVED = "approved", "Approved"
        RELEASED = "released", "Released"

    name = models.CharField(max_length=60, unique=True, help_text="The release's name or number, e.g. 1.5")
    expected_on = models.DateField("Expected on", null=True, blank=True)
    note = models.TextField(blank=True, help_text="What the release is for, in a sentence or two")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    proposed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_back_reason = models.TextField(blank=True, help_text="Why the CPUC did not approve it as proposed")
    # the decision on the built release: go (it is released) or no-go (held, with the reason, until decided again)
    held_reason = models.TextField(blank=True, help_text="Why the CPUC last decided the release does not go yet")
    held_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    held_at = models.DateTimeField(null=True, blank=True)
    released_on = models.DateField("Released on", null=True, blank=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    campaign = models.ForeignKey(
        "testing.Campaign",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="release_plans",
        help_text="The round of testing last opened for the release's acceptance tests",
    )

    class Meta:
        ordering = [models.F("expected_on").asc(nulls_last=True), "pk"]

    def __str__(self):
        return f"Release {self.name}"


class WorkItem(models.Model):
    """One problem to fix or one improvement to make that the CPUC has decided to consider. (A "change" is already a line of a
    release note, so this record has its own name.) It starts Proposed, is accepted into the one ordered backlog or declined with a
    reason, and is internal until marked public."""

    class Kind(models.TextChoices):
        PROBLEM = "problem", "A problem"
        IMPROVEMENT = "improvement", "An improvement"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        ACCEPTED = "accepted", "In the backlog"
        PLANNED = "planned", "Planned for a release"
        DONE = "done", "Done"
        DECLINED = "declined", "Not planned"

    class Size(models.TextChoices):
        SMALL = "S", "Small"
        MEDIUM = "M", "Medium"
        LARGE = "L", "Large"

    class Severity(models.TextChoices):
        BLOCKS = "blocks", "Blocks a filing"
        WRONG = "wrong", "Gives a wrong result"
        WORKAROUND = "workaround", "Has a workaround"
        COSMETIC = "cosmetic", "Cosmetic"

    title = models.CharField(max_length=200, help_text="In plain words; what people outside the CPUC see once it is public")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PROPOSED)
    # the story: who, where, what they need and why
    who = models.CharField(max_length=200, blank=True, help_text="The kind of user")
    pages = models.TextField(blank=True, help_text="The pages it touches, one address a line")
    need = models.TextField(blank=True)
    why = models.TextField(blank=True)
    size = models.CharField(max_length=1, choices=Size.choices, blank=True)
    severity = models.CharField(max_length=12, choices=Severity.choices, blank=True)
    workaround = models.TextField(blank=True, help_text="What a person can do meanwhile")
    public = models.BooleanField(default=False)
    position = models.PositiveIntegerField(null=True, blank=True, help_text="Its place in the backlog, 1 first; accepted items only")
    release = models.ForeignKey(
        ReleasePlan, null=True, blank=True, on_delete=models.PROTECT, related_name="items", help_text="The release it is planned for"
    )
    issue_url = models.URLField("Issue", max_length=300, blank=True, help_text="The address of its issue in the developers' tracker")
    issue_state = models.CharField(
        max_length=6, blank=True, help_text="open or closed, as the developers' tracker last said; blank until it says"
    )
    issue_changed_at = models.DateTimeField(null=True, blank=True, help_text="When the tracker says the issue last changed")
    built_at = models.DateTimeField(
        null=True, blank=True, help_text="When it was marked built, ready for the release: by a backlog manager, or by its issue closing"
    )
    built_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The backlog manager who marked it built; empty when its issue closing did",
    )
    # what a backlog manager proposed and the CPUC has not yet decided
    decline_proposed = models.TextField(blank=True, help_text="The reason proposed for declining it")
    decline_proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    public_proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    proposed_at = models.DateTimeField(null=True, blank=True, help_text="When the oldest undecided proposal was made")
    declined_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True, help_text="When it was accepted or declined")

    class Meta:
        ordering = ["-pk"]
        indexes = [models.Index(fields=["status", "position"])]

    def __str__(self):
        return f"{self.number}: {self.title}"

    @property
    def number(self) -> str:
        return f"W-{self.pk}"

    @property
    def awaits_approval(self) -> bool:
        return bool(self.decline_proposed_by_id or self.public_proposed_by_id)

    @property
    def page_list(self) -> list[str]:
        return [p.strip() for p in self.pages.splitlines() if p.strip()]


class RequestLink(models.Model):
    """A request (a Feedback) joined to the work item that would settle it: the item's evidence. It keeps the request's kind, page,
    organization and date, so the evidence stays when a closed request's retention period ends and the request is deleted."""

    work_item = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name="links")
    feedback = models.OneToOneField("feedback.Feedback", null=True, blank=True, on_delete=models.SET_NULL, related_name="work_link")
    kind = models.CharField(max_length=10)
    page = models.CharField(max_length=500)
    organisation = models.ForeignKey("accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    sent_at = models.DateTimeField()
    linked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sent_at", "pk"]

    def __str__(self):
        return f"A request of {self.sent_at:%Y-%m-%d} joined to W-{self.work_item_id}"


class TrackerPull(models.Model):
    """A pull request in the developers' tracker that names a work item (by its number, W-12, or by its issue, #34), as the
    tracker's signed events last described it. Only its number, address and state are kept, never its words."""

    class State(models.TextChoices):
        OPEN = "open", "Open"
        MERGED = "merged", "Merged"
        CLOSED = "closed", "Closed without merging"

    work_item = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name="pulls")
    number = models.PositiveIntegerField()
    url = models.URLField(max_length=300)
    state = models.CharField(max_length=6, choices=State.choices)
    changed_at = models.DateTimeField(help_text="When the tracker says the pull request last changed")
    seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["number", "pk"]
        constraints = [models.UniqueConstraint(fields=["work_item", "url"], name="work_one_pull_per_item")]

    def __str__(self):
        return f"Pull request #{self.number} for W-{self.work_item_id}"


class Follow(models.Model):
    """A signed-in person who said a public work item affects them too. They are told when it is planned for a release or
    declined, and the count is part of the item's evidence."""

    work_item = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name="follows")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    organisation = models.ForeignKey("accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["work_item", "user"], name="work_follow_once")]

    def __str__(self):
        return f"A follower of W-{self.work_item_id}"


class AcceptanceTest(models.Model):
    """What "done" means for a work item: steps and an expected result, written as the test scripts' tests are. It may name a
    test of the loaded test scripts by its ID; that test is then run in the release's round of testing, and its results are
    shown with the release."""

    work_item = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name="tests")
    title = models.CharField(max_length=300)
    steps = models.TextField(help_text="One step a line")
    expected = models.TextField(help_text="What the person sees when it works")
    library_id = models.CharField(
        "Test ID", max_length=20, blank=True, help_text="The test's ID in the test scripts, when it is one of them, e.g. PAF-023"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.title

    @property
    def step_list(self) -> list[str]:
        return [line.strip() for line in self.steps.splitlines() if line.strip()]
