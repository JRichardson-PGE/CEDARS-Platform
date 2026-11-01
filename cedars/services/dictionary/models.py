from django.conf import settings
from django.db import models
from django.db.models.functions import Lower


class CatalogVersion(models.Model):
    """One published version of the field catalog. Versions are never edited; a change is a new version, and the current catalog is
    the newest published one. Templates, schemas and pages say which version made them."""

    label = models.CharField(max_length=80, unique=True, help_text="e.g. ReCET 0.1.75")
    notes = models.TextField(blank=True)
    loaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    loaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-loaded_at", "-pk"]

    def __str__(self):
        return self.label


class Field(models.Model):
    """A field under its canonical name, with the data-dictionary attributes of BR-08.1."""

    class Kind(models.TextChoices):
        INPUT = "input", "Input"
        OUTPUT = "output", "Output"

    class Audience(models.TextChoices):
        PUBLIC = "public", "Public"
        RESTRICTED = "restricted", "Restricted"
        CONFIDENTIAL = "confidential", "Confidential (PII)"

    version = models.ForeignKey(CatalogVersion, on_delete=models.CASCADE, related_name="fields")
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=6, choices=Kind.choices)
    group = models.CharField(max_length=200, blank=True)
    definition = models.TextField(blank=True)
    doc_link = models.CharField(max_length=400, blank=True, help_text="The ReCET documentation entry, e.g. OUTPUT_FIELDS.md#elecben")
    data_type = models.CharField(max_length=20, default="text", help_text="text, number, boolean or year_quarter")
    max_length = models.PositiveIntegerField(null=True, blank=True)
    unit = models.TextField(blank=True)
    source = models.TextField(blank=True)
    source_version = models.CharField("version", max_length=120, blank=True)
    cost_tests = models.TextField(blank=True)
    time_basis = models.TextField(blank=True)
    aggregation = models.TextField(blank=True)
    rounding = models.TextField(blank=True)
    years_in_use = models.TextField(blank=True)
    audience = models.CharField(max_length=12, choices=Audience.choices, default=Audience.PUBLIC)
    required_by = models.TextField(
        blank=True, help_text="The decision or resolution that requires the field, so a retirement review (BR-08.4) can tell"
    )

    class Meta:
        ordering = ["kind", "group", "name"]
        constraints = [models.UniqueConstraint("version", Lower("name"), name="dictionary_one_name_per_version")]

    def __str__(self):
        return self.name


class Alias(models.Model):
    """Another name a field is known by: CET's E3 names, earlier ReCET names. Accepted on upload, case-insensitively."""

    field = models.ForeignKey(Field, on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField(max_length=80)
    system = models.CharField(max_length=60, help_text="Where the name is used, e.g. CET")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.system}) = {self.field.name}"


class Usage(models.Model):
    """A field's place in a module's upload table, in column order."""

    field = models.ForeignKey(Field, on_delete=models.CASCADE, related_name="usages")
    module = models.CharField(max_length=40)
    table = models.CharField(max_length=60)
    position = models.PositiveIntegerField()
    required = models.BooleanField(default=False)

    class Meta:
        ordering = ["module", "table", "position"]

    def __str__(self):
        return f"{self.module}.{self.table}[{self.position}] {self.field.name}"


class ValueList(models.Model):
    """A code set the rules and the input editor check values against (BR-12.6), under the engine's name for it (e.g. `sector`,
    `ac_climate_zone`). Until a version is published here, the list the pinned engine ships is the one in force."""

    name = models.CharField(max_length=60, unique=True, help_text="The engine's name for the list (its file in recet/data/value_lists)")
    label = models.CharField(max_length=120, blank=True, help_text="The field it fills, e.g. Sector")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.label or self.name


class ValueListVersion(models.Model):
    """One version of a value list: every code it has, retired ones included. An administrator edits a draft (copied from the version
    in force) and publishes it with the date it takes effect and the reason; a published version never changes and its codes are never
    deleted (database triggers, migration 0002). The version in force on a date is the newest published one effective by then."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    value_list = models.ForeignKey(ValueList, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    effective_from = models.DateField(null=True, blank=True, help_text="Set when it is published")
    reason = models.TextField(blank=True, help_text="Why this version was made, given when it is published")
    based_on = models.CharField(max_length=120, blank=True, help_text='e.g. "version 2", or "the list shipped with ReCET 0.1.87"')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["value_list", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["value_list", "number"], name="dictionary_one_list_version_number"),
            models.UniqueConstraint(fields=["value_list"], condition=models.Q(status="draft"), name="dictionary_one_draft_per_list"),
            models.UniqueConstraint(
                fields=["value_list", "effective_from"],
                condition=models.Q(status="published"),
                name="dictionary_one_list_version_per_date",
            ),
            models.CheckConstraint(
                condition=models.Q(status="draft") | models.Q(effective_from__isnull=False),
                name="dictionary_published_list_has_date",
            ),
        ]

    def __str__(self):
        return f"{self.value_list} version {self.number}{' (draft)' if self.status == self.Status.DRAFT else ''}"


class Code(models.Model):
    """One code of a value list version: active for claim years start_year to end_year (blank: open). A retired code keeps its row with
    an end year; codes are never deleted from a published version, and a draft may drop only a code it added itself."""

    version = models.ForeignKey(ValueListVersion, on_delete=models.CASCADE, related_name="codes")
    code = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    start_year = models.PositiveSmallIntegerField(null=True, blank=True)
    end_year = models.PositiveSmallIntegerField(null=True, blank=True)
    extra = models.JSONField(default=dict, blank=True, help_text="The list's other columns as shipped (claim_spec, pa_group, ...)")
    added_in_draft = models.BooleanField(default=False, help_text="Added in this version, so the draft may still drop it")

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint("version", Lower("code"), name="dictionary_one_code_per_version"),
            models.CheckConstraint(
                condition=models.Q(start_year__isnull=True)
                | models.Q(end_year__isnull=True)
                | models.Q(end_year__gte=models.F("start_year")),
                name="dictionary_code_years_in_order",
            ),
        ]

    def __str__(self):
        return self.code

    def active_in(self, year: int) -> bool:
        return (self.start_year is None or self.start_year <= year) and (self.end_year is None or self.end_year >= year)
