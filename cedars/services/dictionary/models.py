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
