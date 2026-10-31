from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import Func, Q


class DateRange(Func):
    """daterange(valid_from, valid_to, '[]'): a version's validity, both ends inclusive, open-ended when valid_to is NULL."""

    function = "daterange"
    output_field = DateRangeField()

    def __init__(self, start, end):
        super().__init__(start, end, models.Value("[]"))


class Program(models.Model):
    """A program, identified by its PrgID for good. Its attributes live in ProgramVersion rows; closed programs stay (BR-13.5)."""

    prg_id = models.CharField("PrgID", max_length=60, unique=True)
    administrator = models.ForeignKey(
        "accounts.Organisation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="programs",
        help_text="Blank for a statewide (SW) program",
    )
    statewide = models.BooleanField(default=False, help_text="A statewide program, funded by several PAs through the allocation factors")
    portfolio_cost = models.BooleanField(default=False, help_text="A portfolio-level cost ID, kept out of the program catalog (BR-02.6)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["prg_id"]

    def __str__(self):
        return self.prg_id


class ProgramVersion(models.Model):
    """A program's attributes from valid_from to valid_to (inclusive; open-ended when valid_to is blank). Versions never overlap and are
    never edited: a change closes the current version and starts a new one, possibly on a future date (BR-13.3, BR-12.7)."""

    program = models.ForeignKey(Program, on_delete=models.PROTECT, related_name="versions")
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    name = models.CharField("program name", max_length=300)
    commercial_name = models.CharField(max_length=300, blank=True)
    status = models.CharField(
        max_length=40, blank=True, help_text="CEDARS' values: new, active, closed_to_new_commitments, closure_pending, closed"
    )
    segment = models.CharField(max_length=60, blank=True)
    primary_sector = models.CharField(max_length=60, blank=True)
    sectors = models.JSONField(default=dict, blank=True, help_text='Budget share by sector, e.g. {"Commercial": 0.9, "Industrial": 0.1}')
    implementer = models.CharField(max_length=200, blank=True)
    category = models.CharField(max_length=200, blank=True)
    implementation_contractor = models.CharField(max_length=300, blank=True)
    start_year = models.PositiveSmallIntegerField(null=True, blank=True)
    end_year = models.PositiveSmallIntegerField(null=True, blank=True)
    flags = models.JSONField(default=dict, blank=True, help_text="Resource, deemed, upstream ... flags, as CEDARS' Programs list has them")
    statewide_program = models.CharField(max_length=200, blank=True, help_text="The statewide program this one delivers, if any")
    parent_program = models.CharField(max_length=100, blank=True)
    exclude_from_budget = models.BooleanField(default=False)
    exclude_from_ce = models.BooleanField("exclude from cost-effectiveness", default=False)
    exclude_from_statewide = models.BooleanField(default=False)
    website = models.URLField(blank=True)
    reason = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["program__prg_id", "valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=models.F("valid_from")), name="registry_version_ends_after_start"
            ),
            ExclusionConstraint(
                name="registry_versions_do_not_overlap",
                expressions=[("program", RangeOperators.EQUAL), (DateRange("valid_from", "valid_to"), RangeOperators.OVERLAPS)],
            ),
        ]

    def __str__(self):
        return f"{self.program.prg_id} from {self.valid_from}"


class FilingCycle(models.Model):
    """A four-year budget cycle (e.g. 2028-2031) that budgets, fund shifts, monthly reports and claims hang off."""

    first_year = models.PositiveSmallIntegerField(unique=True)
    last_year = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["first_year"]
        constraints = [models.CheckConstraint(condition=Q(last_year__gte=models.F("first_year")), name="registry_cycle_years_in_order")]

    def __str__(self):
        return f"{self.first_year}-{self.last_year}"

    def covers(self, year: int) -> bool:
        return self.first_year <= year <= self.last_year


class ReportingPeriod(models.Model):
    """A month, quarter or program year. Due dates belong to the module that reports on it (the submissions service's calendar)."""

    class Kind(models.TextChoices):
        MONTH = "month", "Month"
        QUARTER = "quarter", "Quarter"
        YEAR = "year", "Year"

    kind = models.CharField(max_length=8, choices=Kind.choices)
    year = models.PositiveSmallIntegerField()
    number = models.PositiveSmallIntegerField(default=0, help_text="Month 1-12 or quarter 1-4; 0 for a year")
    starts = models.DateField()
    ends = models.DateField()

    class Meta:
        ordering = ["year", "kind", "number"]
        constraints = [models.UniqueConstraint(fields=["kind", "year", "number"], name="registry_one_period")]

    def __str__(self):
        if self.kind == self.Kind.MONTH:
            return f"{self.year}-{self.number:02d}"
        if self.kind == self.Kind.QUARTER:
            return f"{self.year}Q{self.number}"
        return str(self.year)


class AllocationVersion(models.Model):
    """One version of the statewide allocation factors (BR-06.7, BR-13.8). The factors change between filings and between a filing and
    its claims, so each version is its own immutable table: a draft can be replaced, a confirmed version never changes, and filing
    versions, claim versions and runs pin the version they were priced with. A run naming none takes the most recent confirmed version
    with factors for its program year."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CONFIRMED = "confirmed", "Confirmed"

    class Purpose(models.TextChoices):
        FILING = "filing", "Budget filing"
        CLAIMS = "claims", "Claims"

    label = models.CharField(max_length=120, unique=True)
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    filing_cycle = models.ForeignKey(FilingCycle, null=True, blank=True, on_delete=models.PROTECT, related_name="allocation_versions")
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    source_name = models.CharField(max_length=255, blank=True, help_text="The file the factors came from")
    source_sha256 = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-confirmed_at", "-created_at"]

    def __str__(self):
        return self.label


class AllocationEntry(models.Model):
    """One statewide program in one program year of a version, with its electric share of the budget."""

    version = models.ForeignKey(AllocationVersion, on_delete=models.CASCADE, related_name="entries")
    year = models.PositiveSmallIntegerField()
    program_id = models.CharField("statewide PrgID", max_length=60)
    program_name = models.CharField(max_length=300, blank=True)
    percent_electric = models.DecimalField(max_digits=7, decimal_places=4)

    class Meta:
        ordering = ["year", "program_id"]
        constraints = [models.UniqueConstraint(fields=["version", "year", "program_id"], name="registry_one_entry")]

    def __str__(self):
        return f"{self.program_id} {self.year}"


class AllocationShare(models.Model):
    """A funding PA's share of one entry: its electric, gas and weighted factors."""

    entry = models.ForeignKey(AllocationEntry, on_delete=models.CASCADE, related_name="shares")
    funding_pa = models.CharField(max_length=20, help_text="The funding PA's code, e.g. PGE")
    electric = models.DecimalField(max_digits=7, decimal_places=4)
    gas = models.DecimalField(max_digits=7, decimal_places=4)
    weighted = models.DecimalField(max_digits=7, decimal_places=4)

    class Meta:
        ordering = ["funding_pa"]
        constraints = [models.UniqueConstraint(fields=["entry", "funding_pa"], name="registry_one_share")]

    def __str__(self):
        return f"{self.funding_pa} share of {self.entry}"


class ProgramChangePolicy(models.Model):
    """Which program attributes are locked (BR-13.7): a program's administrators change the others themselves, and request a change to
    a locked one, which a CPUC programs approver (or their delegate) decides. A dated setting, not code: the policy in force on a day
    is the newest one effective by then; earlier ones are kept."""

    effective_from = models.DateField(unique=True)
    locked_fields = models.JSONField(default=list, blank=True, help_text="ProgramVersion attribute names")
    reason = models.TextField(blank=True)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from"]
        verbose_name_plural = "program change policies"

    def __str__(self):
        return f"locked program attributes from {self.effective_from}"


class ProgramChangeRequest(models.Model):
    """A requested change to a program that touches a locked attribute (BR-13.7). Approving it starts the new version, as a change by
    the program's administrators would; declining or withdrawing it changes nothing. Requests are kept, decided or not."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for a decision"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        WITHDRAWN = "withdrawn", "Withdrawn"

    program = models.ForeignKey(Program, on_delete=models.PROTECT, related_name="change_requests")
    organisation = models.ForeignKey("accounts.Organisation", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)
    effective = models.DateField()
    reason = models.TextField()
    changes = models.JSONField(help_text="{attribute: new value}")
    before = models.JSONField(default=dict, help_text="The same attributes in the latest version when the request was made")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_as_delegate_of = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    version = models.ForeignKey(ProgramVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"change to {self.program.prg_id} from {self.effective} ({self.get_status_display().lower()})"


class GoalSet(models.Model):
    """The energy efficiency goals one CPUC decision adopts (e.g. D.25-08-034): for each IOU, cumulative goals per cycle and annual
    targets. The CPUC resets goals by decision about every two years, and the newest confirmed set replaces the earlier ones for every
    year it covers (Jake, 2026-10-31); a superseded set stays, read-only, for the record. A draft can be discarded; a confirmed set never
    changes. One CPUC system administrator loads a set and a different one confirms it."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CONFIRMED = "confirmed", "Confirmed"

    decision = models.CharField(max_length=40, unique=True, help_text="e.g. D.25-08-034")
    adopted = models.DateField(help_text="The date the Commission adopted the decision")
    url = models.URLField(max_length=400, blank=True, help_text="The decision's published document")
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    source_name = models.CharField(max_length=255, blank=True)
    source_sha256 = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-confirmed_at", "-created_at"]

    def __str__(self):
        return f"Goals of {self.decision}"


class Goal(models.Model):
    """One IOU's goal for a cycle (cumulative, e.g. 2028-2031) or a target for one year, as the decision's table prints it. A blank
    is a value the decision does not set (SCE therms, SoCalGas electric), never zero."""

    class Kind(models.TextChoices):
        CYCLE = "cycle", "Cumulative cycle goal"
        ANNUAL = "annual", "Annual target"

    goal_set = models.ForeignKey(GoalSet, on_delete=models.CASCADE, related_name="goals")
    utility = models.CharField(max_length=10, help_text="The IOU territory: PGE, SCE, SDGE or SCG")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    first_year = models.PositiveSmallIntegerField()
    last_year = models.PositiveSmallIntegerField()
    program_tsb = models.DecimalField("incentive programs TSB ($)", max_digits=16, decimal_places=2, null=True, blank=True)
    program_gwh = models.DecimalField("incentive programs GWh", max_digits=12, decimal_places=3, null=True, blank=True)
    program_mw = models.DecimalField("incentive programs MW", max_digits=12, decimal_places=3, null=True, blank=True)
    program_mmtherms = models.DecimalField("incentive programs MMTherms", max_digits=12, decimal_places=3, null=True, blank=True)
    cs_gwh = models.DecimalField("codes and standards GWh", max_digits=12, decimal_places=3, null=True, blank=True)
    cs_mw = models.DecimalField("codes and standards MW", max_digits=12, decimal_places=3, null=True, blank=True)
    cs_mmtherms = models.DecimalField("codes and standards MMTherms", max_digits=12, decimal_places=3, null=True, blank=True)

    class Meta:
        ordering = ["utility", "kind", "first_year"]
        constraints = [
            models.UniqueConstraint(fields=["goal_set", "utility", "kind", "first_year"], name="registry_one_goal"),
            models.CheckConstraint(condition=Q(last_year__gte=models.F("first_year")), name="registry_goal_years_in_order"),
        ]

    def __str__(self):
        years = str(self.first_year) if self.first_year == self.last_year else f"{self.first_year}-{self.last_year}"
        return f"{self.utility} {years}"


class GoalCountingRule(models.Model):
    """Which PAs' claims count toward an IOU territory's goals, and at what share (D.23-08-005 sets them for CCAs and RENs; the goals
    tables themselves are silent). Rules belong to a goal set; the newest confirmed set that lists any rules governs."""

    class AppliesTo(models.TextChoices):
        ALL = "all", "All goals"
        PROGRAMS = "programs", "Incentive program goals"
        CS = "cs", "Codes and standards goals"

    goal_set = models.ForeignKey(GoalSet, on_delete=models.CASCADE, related_name="counting_rules")
    pa = models.CharField("PA", max_length=20, help_text="The claiming PA's code, e.g. MCE or BAYREN")
    territory = models.CharField(max_length=10, help_text="The IOU territory it counts toward: PGE, SCE, SDGE or SCG")
    share = models.DecimalField(max_digits=7, decimal_places=4, help_text="0 to 1")
    applies_to = models.CharField(max_length=10, choices=AppliesTo.choices, default=AppliesTo.ALL)
    source = models.CharField(max_length=60, blank=True, help_text="The decision that sets the rule, e.g. D.23-08-005")

    class Meta:
        ordering = ["territory", "pa"]
        constraints = [models.UniqueConstraint(fields=["goal_set", "pa", "territory", "applies_to"], name="registry_one_counting_rule")]

    def __str__(self):
        return f"{self.pa} toward {self.territory} at {self.share}"
