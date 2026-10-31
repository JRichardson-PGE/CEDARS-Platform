from django.conf import settings
from django.db import models
from django.db.models import Q


class PackVersion(models.Model):
    """One version of a data pack the engine or a module reads: an ACC release, the impact-profile library, an eTRM snapshot, DEER
    package, value lists, run parameters, an engine release or a cost-test definition set. The files live in object storage under
    storage_uri; the manifest lists each with its SHA-256, and the fingerprint is the SHA-256 of the manifest, so a run that records it
    names its inputs exactly.

    A draft is usable for analysis runs and refused for filings (BR-11.8). Publishing needs the tie-outs its kind requires (BR-06.21).
    Once published, nothing that identifies the data changes (a database trigger refuses it); a pack is withdrawn, never deleted."""

    class Kind(models.TextChoices):
        ACC_ELECTRIC = "acc_electric", "ACC, electric (hourly)"
        ACC_GAS = "acc_gas", "ACC, gas (monthly)"
        IMPACT_PROFILES = "impact_profiles", "Impact-profile library"
        ETRM_SNAPSHOT = "etrm_snapshot", "eTRM measures and permutations"
        ETRM_PERMUTATIONS = "etrm_permutations", "eTRM published permutations"
        DEER = "deer", "DEER package"
        VALUE_LISTS = "value_lists", "Value lists"
        RUN_PARAMETERS = "run_parameters", "Run parameters"
        ENGINE_RELEASE = "engine_release", "Engine release"
        COST_TESTS = "cost_tests", "Cost-test definition set"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft (analysis only)"
        PUBLISHED = "published", "Published"
        WITHDRAWN = "withdrawn", "Withdrawn"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    label = models.CharField(max_length=120, help_text='e.g. "2026 ACC"')
    version = models.CharField(max_length=60, blank=True, help_text='the source\'s own version, e.g. "v1b" or an export number')
    acc_year = models.PositiveSmallIntegerField(null=True, blank=True)
    valid_from = models.DateField(null=True, blank=True, help_text="in force for program years from this date (blank: not dated)")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    manifest = models.JSONField(default=dict, help_text="{path: {sha256, bytes}}")
    fingerprint = models.CharField(max_length=64)
    storage_uri = models.CharField(max_length=500)
    source = models.TextField(blank=True, help_text="where the data came from (published file, sync job, module export)")
    notes = models.TextField(blank=True)
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    registered_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    withdrawn_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["kind", "-registered_at"]
        constraints = [models.UniqueConstraint(fields=["kind", "label", "version"], name="refdata_one_pack_version")]

    def __str__(self):
        return f"{self.label}{f' {self.version}' if self.version else ''}{' (draft)' if self.status == self.Status.DRAFT else ''}"

    @property
    def files(self) -> int:
        return len(self.manifest)

    @property
    def bytes(self) -> int:
        return sum(int(m.get("bytes", 0)) for m in self.manifest.values())


class TieOutRequirement(models.Model):
    """A check a kind of pack must pass before it is published (configuration, not code; seeded by migration 0002)."""

    kind = models.CharField(max_length=20, choices=PackVersion.Kind.choices)
    name = models.CharField(max_length=40)
    description = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "name"], name="refdata_one_requirement")]

    def __str__(self):
        return f"{self.kind}: {self.name}"


class TieOut(models.Model):
    """The result of one acceptance check on a pack: the loaded data reconcile to the source workbook, or a reference set of measures
    priced by an independent calculation matches the engine to a stated precision."""

    pack = models.ForeignKey(PackVersion, on_delete=models.PROTECT, related_name="tie_outs")
    name = models.CharField(max_length=40)
    passed = models.BooleanField()
    precision = models.FloatField(null=True, blank=True, help_text="largest relative difference allowed")
    largest_difference = models.FloatField(null=True, blank=True)
    engine_release = models.CharField(max_length=60, blank=True)
    summary = models.TextField(blank=True)
    report = models.JSONField(default=dict, blank=True)
    report_uri = models.CharField(max_length=500, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at"]
        constraints = [
            models.CheckConstraint(condition=Q(precision__isnull=True) | Q(precision__gte=0), name="refdata_tieout_precision_not_negative")
        ]

    def __str__(self):
        return f"{self.pack} {self.name}: {'passed' if self.passed else 'failed'}"


class EtrmPermutation(models.Model):
    """One published eTRM permutation of an eTRM permutations pack, by its Measure Detail ID (a claim's MeasDetailID): the deemed values
    the platform reads, under eTRM's own column labels (refdata.etrm.KEPT; blanks left out). Indexed from the pack's files when it is
    loaded, and never changed: a new download is a new pack."""

    pack = models.ForeignKey(PackVersion, on_delete=models.CASCADE, related_name="etrm_permutations")
    measure_detail_id = models.CharField(max_length=255)
    key = models.CharField(max_length=255, help_text="measure_detail_id in lower case, for the lookup")
    measure_version_id = models.CharField(max_length=60, help_text="e.g. SWHC052-02, in lower case")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    values = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["pack", "key"], name="refdata_one_permutation_per_pack")]
        indexes = [models.Index(fields=["pack", "measure_version_id"], name="refdata_perm_version")]

    def __str__(self):
        return self.measure_detail_id
