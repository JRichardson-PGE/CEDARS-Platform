from django.conf import settings
from django.db import models
from django.db.models import Q


class Specification(models.Model):
    """One kind of filing's specification (quarterly claims, budget and application filings, monthly reports): the module whose filings
    use it and its versions. The CPUC names each version; the newest published one is the specification's current version."""

    code = models.CharField(max_length=40, unique=True, help_text="claims, budget_filing or monthly; the name in its web addresses")
    module = models.CharField(max_length=40, unique=True, help_text="The module whose filings use it: claims, budgets or monthly")
    name = models.CharField(max_length=120, help_text="What filers call it, e.g. Quarterly claims")
    about = models.TextField(blank=True, help_text="What the filings are, in a sentence or two, shown above its versions")
    position = models.PositiveSmallIntegerField(default=0, help_text="Its place in the list of specifications")

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name


class SpecificationVersion(models.Model):
    """One version of a specification: its tables and fields, the rule set and built-in checks a filing on it meets, the engine release
    and cost-test set that price it, and its file format. Known by the name the CPUC gives it (unique in its specification, no number);
    once published it never changes (database triggers, migration 0001) and is withdrawn only if no filing ever used it. A draft is a
    copy of a published version."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PROPOSED = "proposed", "Proposed"
        PUBLISHED = "published", "Published"
        WITHDRAWN = "withdrawn", "Withdrawn"
        HISTORIC = "historic", "Legacy CEDARS layout"

    specification = models.ForeignKey(Specification, on_delete=models.PROTECT, related_name="versions")
    name = models.CharField(max_length=120, help_text="The name the CPUC gives the version, unique in its specification")
    slug = models.SlugField(max_length=120, help_text="The short form of the name in web addresses")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    summary = models.TextField(blank=True, help_text="What the version is and why it was made, shown at the top of its page")
    based_on = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="The version this one was copied from"
    )
    rule_set = models.ForeignKey(
        "rules.RuleSet",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="specification_versions",
        help_text="The rule set a filing on this version is checked against (blank: field checks only)",
    )
    rule_set_code = models.CharField(
        max_length=40, blank=True, help_text="Which rules (the rule library's code) the version takes, e.g. claims; blank: none"
    )
    engine_release = models.CharField(
        max_length=40, blank=True, help_text="The engine release that prices its filings, e.g. 0.1.94 (blank: the default release)"
    )
    cost_test_set = models.ForeignKey(
        "refdata.PackVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Its filings' cost-test set (blank: the filing set's, else the one in force for the year)",
    )
    file_format = models.JSONField(default=dict, blank=True, help_text="What files an upload may be and how they are read")
    source = models.CharField(
        max_length=300, blank=True, help_text="Where the version came from, e.g. generated from the platform's checks"
    )
    fingerprint = models.CharField(max_length=64, blank=True, help_text="SHA-256 of the version's content, set when it is published")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, help_text="Why the version is made, given when its draft is started")
    effective_on = models.DateField(
        null=True,
        blank=True,
        help_text="The first day filing sets are set up with it, set when it is published (blank: from its publication)",
    )
    short_notice_reason = models.TextField(blank=True, help_text="Why it takes effect with less than the usual notice")
    release_note = models.ForeignKey(
        "releases.ReleaseNote",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The release note that announced it",
    )
    withdrawn_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_reason = models.TextField(blank=True)
    imported = models.JSONField(
        default=dict,
        blank=True,
        help_text="An imported version's package: where, when and by whom it was exported, its SHA-256, fingerprint and document",
    )
    proposals = models.PositiveSmallIntegerField(
        default=0, help_text="How many times the version was proposed for comment (each time is a round of comments)"
    )
    proposed_at = models.DateTimeField(null=True, blank=True, help_text="When it was last proposed for comment")
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    proposal_note = models.TextField(blank=True, help_text="What the CPUC asks filers to look at, shown while it is proposed")
    legacy_layouts = models.JSONField(
        default=list,
        blank=True,
        help_text="A historic version's legacy CEDARS download layouts (dictionary.legacy.LAYOUTS), one per table",
    )
    comments_close_at = models.DateTimeField(
        null=True, blank=True, help_text="When comments on the proposed version close (the start of the day after the last day)"
    )

    class Meta:
        ordering = ["specification", "-published_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["specification", "name"], name="specifications_one_name_per_specification"),
            models.UniqueConstraint(fields=["specification", "slug"], name="specifications_one_slug_per_specification"),
            models.UniqueConstraint(
                fields=["specification"], condition=Q(status__in=["draft", "proposed"]), name="specifications_one_draft"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["draft", "proposed", "historic"]) | Q(published_at__isnull=False),
                name="specifications_published_has_date",
            ),
            models.CheckConstraint(condition=~Q(name=""), name="specifications_version_has_name"),
        ]

    def __str__(self):
        return f"{self.specification.name}: {self.name}"

    @property
    def frozen(self) -> bool:
        return self.status != self.Status.DRAFT

    @property
    def public(self) -> bool:
        """Readable without signing in: published and withdrawn versions, and the legacy CEDARS layouts."""
        return self.status in (self.Status.PUBLISHED, self.Status.WITHDRAWN, self.Status.HISTORIC)

    @property
    def historic(self) -> bool:
        """A legacy CEDARS download layout, recorded read-only for the years imported from it: never assigned, never checked."""
        return self.status == self.Status.HISTORIC

    @property
    def open_for_comment(self) -> bool:
        """Proposed, and its comment period has not ended."""
        from django.utils import timezone

        return self.status == self.Status.PROPOSED and self.comments_close_at is not None and timezone.now() < self.comments_close_at

    @property
    def comments_last_day(self):
        """The last day comments are taken (the day before comments_close_at, local time), or None."""
        from datetime import timedelta

        from django.utils import timezone

        if self.comments_close_at is None:
            return None
        return timezone.localtime(self.comments_close_at - timedelta(microseconds=1)).date()


class SpecTable(models.Model):
    """A table of a version, in upload order: the names an upload may give it, whether a filing must hold it, its key, how a later
    upload replaces earlier records, whether it is kept in the PII vault, and the columns an upload is refused without."""

    class Replace(models.TextChoices):
        KEY = "key", "Record by record, by the key"
        CLAIM = "claim", "All of a claim's rows as a set"

    version = models.ForeignKey(SpecificationVersion, on_delete=models.CASCADE, related_name="tables")
    position = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=60)
    other_names = models.JSONField(default=list, blank=True, help_text="Other file or sheet names accepted for it, any case")
    required = models.BooleanField(default=False, help_text="Every upload holds it (else it may be left out)")
    keys = models.JSONField(default=list, blank=True, help_text="The fields that identify a record")
    replace = models.CharField(max_length=10, choices=Replace.choices, default=Replace.KEY)
    vault = models.BooleanField(default=False, help_text="Kept in the PII vault and filed apart from the other tables")
    refuse_without = models.JSONField(
        default=list, blank=True, help_text="Columns an upload is refused without at once, before any record is checked"
    )
    guidance = models.TextField(blank=True, help_text="What filers should know about the table, shown above its fields")

    class Meta:
        ordering = ["version", "position"]
        constraints = [
            models.UniqueConstraint(fields=["version", "name"], name="specifications_one_table_name"),
            models.UniqueConstraint(fields=["version", "position"], name="specifications_one_table_position"),
        ]

    def __str__(self):
        return self.name


class SpecField(models.Model):
    """A field of a table: its requirement, its type from the fixed list (each type brings its own check and its own words), the value
    list its codes come from, what a blank means, other accepted names, an example value, a note for filers and its definition as the
    version was made. Its meaning beyond that is the data dictionary's."""

    class Requirement(models.TextChoices):
        REQUIRED = "required", "Required"
        OPTIONAL = "optional", "Optional"
        SYSTEM = "system", "Filled in by CEDARS"

    class Type(models.TextChoices):
        TEXT = "text", "Text"
        NUMBER = "number", "Number"
        INTEGER = "integer", "Whole number"
        FLAG = "flag", "Flag (0 or 1)"
        DATE = "date", "Date"
        YEAR_QUARTER = "year_quarter", "Year and quarter"
        YEAR_MONTH = "year_month", "Year and month"
        PHONE = "phone", "Phone number (digits)"
        EMAIL = "email", "Email address"

    table = models.ForeignKey(SpecTable, on_delete=models.CASCADE, related_name="fields")
    position = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=80)
    other_names = models.JSONField(default=list, blank=True, help_text="Column names from CEDARS or CET also accepted on upload")
    requirement = models.CharField(max_length=10, choices=Requirement.choices, default=Requirement.OPTIONAL)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.TEXT)
    max_length = models.PositiveIntegerField(null=True, blank=True, help_text="Text: the most characters (blank: any length)")
    decimals = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Number: the most decimal places (blank: any)")
    value_list = models.CharField(max_length=60, blank=True, help_text="The value list whose codes are accepted, by its name")
    default = models.CharField(max_length=200, blank=True, help_text="What a blank means, where a blank is allowed")
    example = models.CharField(max_length=200, blank=True, help_text="A valid value, shown on the page and in the template")
    unit = models.CharField(max_length=80, blank=True)
    definition = models.TextField(blank=True, help_text="What the field holds, as the version was made")
    guidance = models.TextField(blank=True, help_text="A note for filers beyond the definition")
    renamed_from = models.CharField(
        max_length=80, blank=True, help_text="The name it had in the version this one was copied from, when this version renamed it"
    )

    class Meta:
        ordering = ["table", "position"]
        constraints = [
            models.UniqueConstraint(fields=["table", "name"], name="specifications_one_field_name"),
            models.UniqueConstraint(fields=["table", "position"], name="specifications_one_field_position"),
        ]

    def __str__(self):
        return f"{self.table.name}.{self.name}"

    @property
    def type_words(self) -> str:
        """Its type in words, e.g. "Text (up to 255 characters)"."""
        from .service import type_words

        return type_words(self)


class CheckSetting(models.Model):
    """A built-in check (written in code) that a filing on the version meets, in the words its findings use, with the settings the
    check lets the CPUC change. `group` is the module or service that runs it (claims, vault, budgets, monthly)."""

    version = models.ForeignKey(SpecificationVersion, on_delete=models.CASCADE, related_name="checks")
    position = models.PositiveSmallIntegerField()
    group = models.CharField(max_length=40)
    rule_id = models.CharField(max_length=20)
    severity = models.CharField(max_length=20, help_text="reject, warning, or reject or warning (when the case decides)")
    tables = models.CharField(max_length=200, blank=True)
    fields = models.CharField(max_length=300, blank=True)
    finds = models.TextField()
    fix = models.TextField(blank=True)
    settings = models.JSONField(default=dict, blank=True, help_text="The values of the check the CPUC may change, e.g. a tolerance")

    class Meta:
        ordering = ["version", "position"]
        constraints = [models.UniqueConstraint(fields=["version", "rule_id"], name="specifications_one_check_per_version")]

    def __str__(self):
        return self.rule_id


class ExampleSet(models.Model):
    """A named set of example records for the version (e.g. deemed and custom claims), its rows per table, which a filer downloads as a
    workbook or a .zip of delimited files. Synthetic only: never a real filing's records."""

    version = models.ForeignKey(SpecificationVersion, on_delete=models.CASCADE, related_name="examples")
    position = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120)
    about = models.TextField(blank=True, help_text="What the example shows")
    rows = models.JSONField(default=dict, blank=True, help_text="{table: [{field: value, ...}, ...]}")
    generated = models.BooleanField(default=False, help_text="Written by CEDARS from the version's fields (examples.generate)")
    report = models.JSONField(
        default=dict,
        blank=True,
        help_text="Its last check: what it fails, the rules it does not show, the checks needing reference data",
    )
    checked = models.CharField(max_length=64, blank=True, help_text="The state of the draft it was last checked in (examples.state)")

    class Meta:
        ordering = ["version", "position"]
        constraints = [models.UniqueConstraint(fields=["version", "slug"], name="specifications_one_example_slug")]

    def __str__(self):
        return self.name


class ImpactTest(models.Model):
    """One run of the impact test on a version (specifications.impact): how the latest confirmed filings of each organization would
    fare under it, against the version each was checked with. Nothing it tests is changed; its report is kept so staff can read it
    before publishing and the published version keeps the test it was published on."""

    version = models.ForeignKey(SpecificationVersion, on_delete=models.CASCADE, related_name="impact_tests")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    state = models.CharField(max_length=64, help_text="The state of the version it tested (impact.state)")
    report = models.JSONField(
        default=dict, blank=True, help_text="Per filing and per rule: records newly rejected or warned, no longer rejected, fields missing"
    )

    class Meta:
        ordering = ["version", "-created_at", "-pk"]

    def __str__(self):
        return f"Impact test of {self.version} at {self.created_at:%Y-%m-%d %H:%M}"


class SpecComment(models.Model):
    """A comment on a proposed version, by a filer (for their organization) or by CPUC staff, about the whole version, a table or a
    field. A CPUC answer to a filer's comment is a comment of its own that replies to it. Never changed (the database refuses, migration
    0008): a correction is another comment. Read by anyone signed in, like the proposed version; kept with the version once it is
    published, as the record of its consultation. Removed only with a draft that is discarded."""

    version = models.ForeignKey(SpecificationVersion, on_delete=models.CASCADE, related_name="comments")
    round = models.PositiveSmallIntegerField(help_text="Which proposal of the version it was written on (1 for the first)")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    author_name = models.CharField(max_length=200, help_text="As the author was named when they wrote it")
    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="+", help_text="The organization it was written for"
    )
    reply_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies", help_text="The comment a CPUC answer replies to"
    )
    table = models.CharField(max_length=60, blank=True, help_text="The table it is about (blank: the whole version)")
    field = models.CharField(max_length=80, blank=True, help_text="The field of that table it is about (blank: the table)")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"comment on {self.version} by {self.author_name}"

    @property
    def about(self) -> str:
        """What it is about, in words: "Measure.NumUnits", "the Measure table" or "the whole version"."""
        if self.table and self.field:
            return f"{self.table}.{self.field}"
        if self.table:
            return f"the {self.table} table"
        return "the whole version"
