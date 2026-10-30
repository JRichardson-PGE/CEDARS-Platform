"""The Impact profiles module's records.

Three groups:
  - the eTRM snapshot: each nightly sync (EtrmSyncRun) stores a new PackageVersion of a draft measure package only when its content
    changed, so the snapshot at any moment is every package's latest version by then, and an upload pins the version it was checked
    against. Packages the sync refused are listed with the reason (ExcludedPackage), as CEDARS' sync history page does;
  - deemed load shapes: an upload (LoadShapeUpload) with its findings, the load shapes it added (LoadShape, one per series identity, each
    with numbered LoadShapeVersions), and the impact profiles generated from them;
  - the profile catalog: ImpactProfile (the engine's key: profile ID, target sector, IOU territory) with numbered, immutable
    ProfileVersions, each holding one 8,760 per climate zone (ProfileZone). Deemed, DEER, TOU, custom and imported legacy profiles share
    the catalog and differ by `family`. ProfileMapping rows let a permutation point at an existing profile (NR and NC sharing a shape).

Every 8,760 is stored once as an array (Shape, content-addressed in default storage), whatever uses it.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

HOURS = 8760


class Shape(models.Model):
    """One 8,760-value array (float64, little-endian), stored once in default storage under its SHA-256 and shared by every load shape and
    profile zone that has exactly these values."""

    sha256 = models.CharField(max_length=64, unique=True)
    total = models.FloatField(help_text="Sum of the 8,760 values")
    minimum = models.FloatField()
    maximum = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.sha256[:12]

    @property
    def storage_name(self) -> str:
        return f"impact_profiles/shapes/{self.sha256[:2]}/{self.sha256}.f8"


# --- the eTRM snapshot


class EtrmSyncRun(models.Model):
    """One pull of draft measure packages and their permutations from eTRM (nightly, or started by a CPUC administrator)."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        AUTOMATIC = "automatic", "Automatic"
        MANUAL = "manual", "Manual"

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.AUTOMATIC)
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=200, blank=True, help_text="Where the data came from (the eTRM database, or a fixture file)")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    packages_seen = models.PositiveIntegerField(default=0)
    imported = models.PositiveIntegerField(default=0, help_text="New packages and packages whose content changed")
    unchanged = models.PositiveIntegerField(default=0)
    excluded = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at", "-pk"]

    def __str__(self):
        return f"eTRM sync {self.started_at:%Y-%m-%d %H:%M} ({self.status})"


class MeasurePackage(models.Model):
    """A draft eTRM measure version (its Measure Version ID ends in "-draft"), as last synced. Its contents are in `current`."""

    measure_version_id = models.CharField(max_length=60, unique=True, help_text="e.g. SWHC045-03-draft")
    statewide_id = models.CharField(max_length=20, blank=True, help_text="e.g. SWHC045")
    name = models.CharField(max_length=300)
    pa_lead = models.CharField("PA lead", max_length=20, help_text="The lead PA's code; only its members upload load shapes for it")
    start_date = models.DateField(null=True, blank=True)
    current = models.ForeignKey("PackageVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    in_last_sync = models.BooleanField(default=True, help_text="False once eTRM no longer offers it (published, or the draft deleted)")

    class Meta:
        ordering = ["measure_version_id"]

    def __str__(self):
        return f"{self.measure_version_id} {self.name}"


class PackageVersion(models.Model):
    """One version of a package's content: its header and permutations, as they were in one sync. Never changed."""

    package = models.ForeignKey(MeasurePackage, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    sync_run = models.ForeignKey(EtrmSyncRun, on_delete=models.PROTECT, related_name="package_versions")
    content_sha256 = models.CharField(max_length=64)
    name = models.CharField(max_length=300)
    pa_lead = models.CharField(max_length=20)
    start_date = models.DateField(null=True, blank=True)
    permutation_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["package", "-number"]
        constraints = [models.UniqueConstraint(fields=["package", "number"], name="impact_profiles_one_package_version_number")]

    def __str__(self):
        return f"{self.package.measure_version_id} v{self.number}"


class Permutation(models.Model):
    """One eTRM permutation's load-shape fields: the building and technology it applies to and its full-precision UECs (kWh per unit).
    The 1st baseline is the pre-existing technology when there is one (accelerated replacement), else the standard; an accelerated
    replacement's 2nd baseline is the standard technology. Each technology has its own Technology Group and Type in eTRM (a fuel
    substitution's standard furnace is not in the measure's heat-pump group); a blank base-case group or type means the measure's."""

    version = models.ForeignKey(PackageVersion, on_delete=models.CASCADE, related_name="permutations")
    sector = models.CharField(max_length=40)
    bldg_type = models.CharField(max_length=40)
    bldg_vint = models.CharField(max_length=40)
    bldg_hvac = models.CharField(max_length=40)
    bldg_loc = models.CharField(max_length=20)
    tech_group = models.CharField(max_length=80)
    tech_type = models.CharField(max_length=80)
    measure_tech_id = models.CharField(max_length=160)
    standard_tech_id = models.CharField(max_length=160, blank=True)
    pre_existing_tech_id = models.CharField(max_length=160, blank=True)
    standard_tech_group = models.CharField(max_length=80, blank=True)
    standard_tech_type = models.CharField(max_length=80, blank=True)
    pre_existing_tech_group = models.CharField(max_length=80, blank=True)
    pre_existing_tech_type = models.CharField(max_length=80, blank=True)
    norm_unit = models.CharField(max_length=40, blank=True)
    measure_application_type = models.CharField(max_length=20, blank=True)
    uec_kwh_meas = models.FloatField(null=True, blank=True)
    uec_kwh_base1 = models.FloatField(null=True, blank=True)
    uec_kwh_base2 = models.FloatField(null=True, blank=True)
    elec_target_sector = models.CharField(
        max_length=60, blank=True, help_text="The permutation's electric target sector (CET's E3TargetSector)"
    )
    electric_impact_profile_id = models.CharField(max_length=300, blank=True, help_text="eTRM's own Electric Impact Profile ID field")

    class Meta:
        ordering = ["pk"]
        indexes = [models.Index(fields=["version", "measure_tech_id"])]

    def __str__(self):
        return f"{self.sector}-{self.bldg_type}-{self.bldg_vint}-{self.bldg_hvac}-{self.bldg_loc} {self.measure_tech_id}"

    @property
    def base1_tech_id(self) -> str:
        return self.pre_existing_tech_id or self.standard_tech_id

    @property
    def base2_tech_id(self) -> str:
        return self.standard_tech_id if self.pre_existing_tech_id else ""

    @property
    def measure_technology(self) -> tuple[str, str, str]:
        return (self.tech_group, self.tech_type, self.measure_tech_id)

    @property
    def standard_technology(self) -> tuple[str, str, str]:
        return (self.standard_tech_group or self.tech_group, self.standard_tech_type or self.tech_type, self.standard_tech_id)

    @property
    def pre_existing_technology(self) -> tuple[str, str, str]:
        return (self.pre_existing_tech_group or self.tech_group, self.pre_existing_tech_type or self.tech_type, self.pre_existing_tech_id)

    def technologies(self) -> list[tuple[str, str, str]]:
        """(TechGroup, TechType, TechID) of the measure and each base case it names: the load shapes it can be paired with."""
        out = [self.measure_technology]
        out += [t for t in (self.pre_existing_technology, self.standard_technology) if t[2]]
        return out

    def baselines(self) -> list[tuple[str, tuple[str, str, str], float | None]]:
        """("1st" / "2nd", the base case's (TechGroup, TechType, TechID), its UEC kWh)."""
        first = self.pre_existing_technology if self.pre_existing_tech_id else self.standard_technology
        out = [("1st", first, self.uec_kwh_base1)]
        if self.pre_existing_tech_id and self.standard_tech_id and self.uec_kwh_base2 is not None:
            out.append(("2nd", self.standard_technology, self.uec_kwh_base2))
        return out


class ExcludedPackage(models.Model):
    """A package one sync refused, with the reason (a zero UEC, no permutations, non-ASCII text, a missing field, an error)."""

    sync_run = models.ForeignKey(EtrmSyncRun, on_delete=models.CASCADE, related_name="exclusions")
    measure_version_id = models.CharField(max_length=60)
    name = models.CharField(max_length=300, blank=True)
    pa_lead = models.CharField(max_length=20, blank=True)
    reason = models.TextField()
    detected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["measure_version_id"]

    def __str__(self):
        return f"{self.measure_version_id}: {self.reason}"


# --- deemed load shapes


class LoadShapeUpload(models.Model):
    """One uploaded 8,760 file for one package, checked in four stages (file, table headers, each 8,760 series, eTRM alignment). By
    default one failure rejects the whole file and nothing is saved; `accept_partial` keeps the series that passed."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Waiting to be checked"
        CHECKING = "checking", "Being checked"
        REJECTED = "rejected", "Rejected: nothing saved"
        ACCEPTED = "accepted", "Accepted"
        GENERATING = "generating", "Generating impact profiles"
        GENERATED = "generated", "Impact profiles generated"
        FAILED = "failed", "Failed: processing error"

    package = models.ForeignKey(MeasurePackage, on_delete=models.PROTECT, related_name="uploads")
    package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, related_name="uploads")
    organisation = models.ForeignKey("accounts.Organisation", on_delete=models.PROTECT, related_name="+")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    file_name = models.CharField(max_length=255)
    stored_name = models.CharField(max_length=300, help_text="The file in default storage")
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    accept_partial = models.BooleanField(default=False, help_text="Keep the series that pass when others fail")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    stages = models.JSONField(default=dict, blank=True, help_text="Each stage's result: passed, failed or not run")
    series_count = models.PositiveIntegerField(default=0)
    accepted_count = models.PositiveIntegerField(default=0)
    checked_at = models.DateTimeField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    generation_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-uploaded_at", "-pk"]

    def __str__(self):
        return f"{self.file_name} for {self.package.measure_version_id}"


class UploadFinding(models.Model):
    """One finding, in the rule library's format (row = file line with the header as line 1, record_id = the series)."""

    class Severity(models.TextChoices):
        REJECT = "reject", "Rejects the file"
        WARNING = "warning", "Warning"

    upload = models.ForeignKey(LoadShapeUpload, on_delete=models.CASCADE, related_name="findings")
    stage = models.CharField(max_length=10)
    rule_id = models.CharField(max_length=20)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    row = models.PositiveIntegerField(null=True, blank=True)
    record_id = models.CharField(max_length=600, blank=True, help_text="The series")
    field = models.CharField(max_length=60, blank=True)
    value = models.CharField(max_length=200, blank=True)
    message = models.TextField()
    fix = models.TextField(blank=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return f"{self.rule_id} {self.message[:60]}"


class LoadShape(models.Model):
    """One load shape of a package, identified as the guide's matching key has it: Sector, BldgType, BldgVint, BldgHVAC, BldgLoc,
    TechGroup, TechType and TechID. Its values are in numbered versions; a re-upload adds a version and never edits one."""

    package = models.ForeignKey(MeasurePackage, on_delete=models.PROTECT, related_name="load_shapes")
    sector = models.CharField(max_length=40)
    bldg_type = models.CharField(max_length=40)
    bldg_vint = models.CharField(max_length=40)
    bldg_hvac = models.CharField(max_length=40)
    bldg_loc = models.CharField(max_length=20, help_text="The climate zone, without the CZ prefix")
    tech_group = models.CharField(max_length=80)
    tech_type = models.CharField(max_length=80)
    tech_id = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["package", "tech_id", "bldg_type", "bldg_loc"]
        constraints = [
            models.UniqueConstraint(
                fields=["package", "sector", "bldg_type", "bldg_vint", "bldg_hvac", "bldg_loc", "tech_group", "tech_type", "tech_id"],
                name="impact_profiles_one_load_shape_per_key",
            )
        ]

    def __str__(self):
        return "-".join(
            [self.sector, self.bldg_type, self.bldg_vint, self.bldg_hvac, self.bldg_loc, self.tech_group, self.tech_type, self.tech_id]
        )

    def current_version(self):
        return self.versions.filter(superseded_at__isnull=True).order_by("-number").first()


class LoadShapeVersion(models.Model):
    load_shape = models.ForeignKey(LoadShape, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    upload = models.ForeignKey(LoadShapeUpload, on_delete=models.PROTECT, related_name="load_shape_versions")
    shape = models.ForeignKey(Shape, on_delete=models.PROTECT, related_name="+")
    norm_unit = models.CharField(max_length=40, blank=True)
    shape_type = models.CharField(max_length=40, blank=True, help_text="Whole Building or End Use")
    source_year = models.PositiveSmallIntegerField(help_text="The calendar the shape is drawn on (2009: Thursday start, not a leap year)")
    created_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["load_shape", "-number"]
        constraints = [models.UniqueConstraint(fields=["load_shape", "number"], name="impact_profiles_one_load_shape_version_number")]

    def __str__(self):
        return f"{self.load_shape} v{self.number}"


# --- the profile catalog


class ImpactProfile(models.Model):
    """One impact profile, keyed as the engine and CET's Avoided Cost Combo key it: the profile ID (CET's E3MeaElecEndUseShape, ReCET's
    AC_Elec_Impact_Profile), the target sector and the IOU territory (blank = every territory). Its shapes are in numbered versions;
    runs and claims name a version, and a replacement adds one."""

    class Family(models.TextChoices):
        DEEMED = "deemed", "Deemed (from eTRM load shapes)"
        CUSTOM = "custom", "Custom (uploaded by a PA)"
        DEER = "deer", "DEER (legacy)"
        TOU = "tou", "TOU (legacy)"
        LEGACY = "legacy", "Imported from CEDARS"

    profile_key = models.CharField("profile ID", max_length=300)
    target_sector = models.CharField(max_length=60)
    iou_ac_territory = models.CharField("IOU territory", max_length=10, blank=True, help_text="Blank: every territory")
    family = models.CharField(max_length=10, choices=Family.choices)
    owner = models.ForeignKey(
        "accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="The PA that owns it"
    )
    package = models.ForeignKey(MeasurePackage, null=True, blank=True, on_delete=models.PROTECT, related_name="profiles")
    description = models.CharField(max_length=500, blank=True)
    legacy_id = models.PositiveIntegerField(null=True, blank=True, help_text="Its id in legacy CEDARS' Impact Profile Library")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    retire_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["profile_key", "target_sector", "iou_ac_territory"]
        constraints = [
            models.UniqueConstraint(fields=["profile_key", "target_sector", "iou_ac_territory"], name="impact_profiles_one_profile_per_key")
        ]
        indexes = [models.Index(fields=["family"]), models.Index(fields=["owner"])]

    def __str__(self):
        where = f" ({self.iou_ac_territory})" if self.iou_ac_territory else ""
        return f"{self.profile_key} / {self.target_sector}{where}"

    @property
    def retired(self) -> bool:
        return self.retired_at is not None

    def current_version(self):
        return self.versions.filter(superseded_at__isnull=True).order_by("-number").first()


class ProfileVersion(models.Model):
    """One version of a profile: an 8,760 per climate zone, the calendar they are drawn on, and where they came from. Never changed."""

    profile = models.ForeignKey(ImpactProfile, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    represents_year = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="The calendar year the shapes are drawn on (blank: 2009, the DEER convention)"
    )
    source = models.JSONField(default=dict, blank=True, help_text="Where it came from: upload, load-shape versions and UECs, or a file")
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["profile", "-number"]
        constraints = [models.UniqueConstraint(fields=["profile", "number"], name="impact_profiles_one_profile_version_number")]

    def __str__(self):
        return f"{self.profile} v{self.number}"


class ProfileZone(models.Model):
    version = models.ForeignKey(ProfileVersion, on_delete=models.CASCADE, related_name="zones")
    climate_zone = models.CharField(max_length=10, help_text="1 to 16, 3A, 3B or System")
    shape = models.ForeignKey(Shape, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["version", "climate_zone"]
        constraints = [models.UniqueConstraint(fields=["version", "climate_zone"], name="impact_profiles_one_zone_per_version")]

    def __str__(self):
        return f"{self.version} CZ {self.climate_zone}"


class ProfileMapping(models.Model):
    """A permutation's profile ID pointing at an existing profile instead of its own shapes: e.g. a measure's new-construction
    permutations (vintage New) using the existing-vintage profile, which today needs the vintage hard-coded to Ex in eTRM."""

    package = models.ForeignKey(MeasurePackage, on_delete=models.PROTECT, related_name="mappings")
    alias_key = models.CharField("profile ID it stands for", max_length=300)
    target_sector = models.CharField(max_length=60)
    target = models.ForeignKey(ImpactProfile, on_delete=models.PROTECT, related_name="aliases")
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["alias_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["alias_key", "target_sector"], condition=Q(ended_at__isnull=True), name="impact_profiles_one_open_mapping"
            )
        ]

    def __str__(self):
        return f"{self.alias_key} -> {self.target.profile_key}"


# --- jobs and library exports


class ProcessingJob(models.Model):
    """Work done outside the web request: the eTRM sync, checking an upload, generating profiles, writing the library. Each PA has its own
    queue (as the Load Shape Tool does), so one PA's large package never holds up another's; `run_impact_profile_jobs` runs them."""

    class Kind(models.TextChoices):
        SYNC = "sync", "eTRM sync"
        CHECK = "check", "Check an upload"
        GENERATE = "generate", "Generate impact profiles"
        EXPORT = "export", "Write the profile library"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    queue = models.CharField(max_length=20, help_text="The PA's code, or 'system'")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    upload = models.ForeignKey(LoadShapeUpload, null=True, blank=True, on_delete=models.PROTECT, related_name="jobs")
    params = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["status", "queue", "created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk} ({self.status})"


class LibraryExport(models.Model):
    """The current catalog written in the engine's library format (catalog.parquet and hourly.parquet, one row group per profile), so a
    run can price on it at once. Numbered and kept, and registered as a draft impact_profiles pack in the reference-data service, which
    runs pin."""

    number = models.PositiveIntegerField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    catalog_name = models.CharField(max_length=300)
    hourly_name = models.CharField(max_length=300)
    catalog_sha256 = models.CharField(max_length=64)
    hourly_sha256 = models.CharField(max_length=64)
    catalog_rows = models.PositiveIntegerField(default=0)
    profiles = models.PositiveIntegerField(default=0)
    zone_shapes = models.PositiveIntegerField(default=0)
    families = models.JSONField(default=dict, blank=True, help_text="Profiles per family")
    pack = models.ForeignKey(
        "refdata.PackVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Its reference-data pack version",
    )

    class Meta:
        ordering = ["-number"]

    def __str__(self):
        return f"Profile library export {self.number}"
