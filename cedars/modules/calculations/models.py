import uuid

from django.conf import settings as conf
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class EngineRelease(models.Model):
    """A ReCET release the platform can run (configured, not coded): its worker image or command, the calculations it offers (ReCET's
    own and CET replicas, D13) and the ACCs it has. One release is the default for new runs; only ReCET's own calculation is accepted
    for filings."""

    release = models.CharField(max_length=40, unique=True, help_text="The ReCET version, e.g. 0.1.78; a job asks for exactly this release")
    image = models.CharField(max_length=300, blank=True, help_text="The worker image (AWS Batch), e.g. <account>.dkr.ecr...:0.1.78")
    batch_job_definition = models.CharField(max_length=200, blank=True, help_text="The AWS Batch job definition that runs the image")
    calculations = models.JSONField(default=list, help_text='"recet" and any CET replicas it carries, e.g. ["recet", "cet-25.1"]')
    accs = models.JSONField(
        default=dict, help_text='The ACC years each calculation has, e.g. {"recet": ["2024", "2026"], "cet-25.1": ["2024"]}'
    )
    default = models.BooleanField(default=False, help_text="Offered first on New run")
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    added_by = models.ForeignKey(conf.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-default", "-added_at"]
        constraints = [models.UniqueConstraint(fields=["default"], condition=Q(default=True), name="calculations_one_default_release")]

    def __str__(self):
        return f"ReCET {self.release}"


class InputDraft(models.Model):
    """A small job's Measure and ProgramCost tables being edited on the Edit inputs pages (up to the engine's MAX_EDIT_ROWS measures),
    started from an upload, a run's inputs or a blank job. Tables are kept as text, in CET's column order, as ReCET's editor keeps them:
    {"columns": [...], "rows": [[...], ...]}. A draft for an organization is shared with its members who may start runs there; a draft
    with no organization is its owner's own. Drafts are scratch data (retention class scratch_run, from their last change)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        "accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="calculation_drafts"
    )
    owner = models.ForeignKey(conf.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="calculation_drafts")
    name = models.CharField(max_length=200)
    source = models.CharField(max_length=300, blank=True, help_text="Where the tables came from")
    source_run = models.ForeignKey("Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="drafts")
    measures = models.JSONField(default=dict)
    programcost = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    @property
    def measure_count(self) -> int:
        return len((self.measures or {}).get("rows", []))


class Run(models.Model):
    """One calculation run: who asked for it, for which organization, the settings, the inputs (by key and SHA-256), the job spec the
    worker was given, its progress, and the worker's receipt (engine, packs, allocation table, outputs and their hashes). A run belongs to
    its organization, so it stays with the organization when its owner leaves; a run with no organization is its owner's own."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Finished"
        FAILED = "failed", "Failed"
        REFUSED = "refused", "Refused by the engine"
        CANCELLED = "cancelled", "Stopped"

    ENDED = (Status.SUCCEEDED, Status.FAILED, Status.REFUSED, Status.CANCELLED)

    class Lane(models.TextChoices):
        SMALL = "small", "Small runs"
        LARGE = "large", "Large runs"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        "accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="calculation_runs"
    )
    owner = models.ForeignKey(conf.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="calculation_runs")
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    record_class = models.CharField(max_length=40, default="scratch_run", help_text="The retention record class (decision M)")

    engine_release = models.CharField(max_length=40)
    calculation = models.CharField(max_length=40, default="recet", help_text='"recet" or a CET replica such as "cet-25.1"')
    acc_version = models.CharField("ACC", max_length=10)
    settings = models.JSONField(default=dict, help_text="The job spec's settings block (RunConfig settings)")
    output_format = models.CharField(max_length=10, default="csv.zip")
    allocation_version = models.ForeignKey(
        "registry.AllocationVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="calculation_runs"
    )

    inputs = models.JSONField(default=dict, help_text="{input: {key, sha256, bytes}}")
    input_hash = models.CharField(max_length=64, help_text="SHA-256 over the inputs' hashes and the settings: identical jobs share it")
    measure_count = models.PositiveIntegerField(null=True, blank=True)
    lane = models.CharField(max_length=5, choices=Lane.choices, default=Lane.SMALL)
    estimate_seconds = models.PositiveIntegerField(null=True, blank=True)
    storage_prefix = models.CharField(max_length=200)
    spec = models.JSONField(default=dict, help_text="The job spec the worker was given")
    submission_version = models.ForeignKey(
        "submissions.SubmissionVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="calculation_runs"
    )

    backend = models.CharField(max_length=10, default="local")
    backend_job_id = models.CharField(max_length=200, blank=True)
    worker = models.CharField(max_length=200, blank=True, help_text="Which worker process took it (local backend)")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    progress = models.JSONField(default=dict, blank=True, help_text="The worker's progress.json: stage, fraction, seconds left")
    receipt = models.JSONField(default=dict, blank=True, help_text="The worker's receipt.json")
    error = models.TextField(blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(conf.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "lane", "created_at"])]

    def __str__(self):
        return self.name

    @property
    def ended(self) -> bool:
        return self.status in self.ENDED

    @property
    def job_id(self) -> str:
        return f"run-{self.id}"

    @property
    def cost_tests(self) -> dict:
        """The cost-test set the run was pinned to (calculations.service.cost_tests_record), {} when none was registered."""
        return ((self.spec or {}).get("context") or {}).get("cost_tests") or {}

    @property
    def seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


class OutputTemplate(models.Model):
    """An output template (BR-06.10): a named list of measure-level columns, in order. A run given one writes only those columns, in
    that order, to its measure-level file; the Results tables still read every column. A template with no owner is offered to everyone
    and kept by CPUC system administrators (the two ReCET ships with are added by migration); the rest are their owner's own."""

    owner = models.ForeignKey(conf.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="output_templates")
    name = models.CharField(max_length=60)
    columns = models.JSONField(default=list, help_text="Field names (the dictionary's canonical names), in file order")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [models.F("owner").asc(nulls_first=True), Lower("name")]
        constraints = [
            models.UniqueConstraint("owner", Lower("name"), name="calculations_template_name_per_owner"),
            models.UniqueConstraint(Lower("name"), condition=Q(owner__isnull=True), name="calculations_shared_template_name"),
        ]

    def __str__(self):
        return self.name

    @property
    def shared(self) -> bool:
        return self.owner_id is None


class OutputTemplateDefault(models.Model):
    """The output template a person's new runs start with (none: every column)."""

    user = models.OneToOneField(conf.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="output_template_default")
    template = models.ForeignKey(OutputTemplate, on_delete=models.CASCADE, related_name="+")

    def __str__(self):
        return f"{self.user}: {self.template}"
