from django.conf import settings
from django.db import models

# The ten ProgramCost fields CET adds up into a program's cost (ReCET OUTPUT_FIELDS, WeightedProgramCost: T_r), in the spec's order.
PROGRAM_COST_FIELDS = [
    ("AdminCostsOverheadAndGA", "Administration: overhead and G&A"),
    ("AdminCostsOther", "Administration: other"),
    ("MarketingOutreach", "Marketing and outreach"),
    ("DIActivity", "Direct implementation: activity"),
    ("DIInstallation", "Direct implementation: installation"),
    ("DIHardwareAndMaterials", "Direct implementation: hardware and materials"),
    ("DIRebateAndInspection", "Direct implementation: rebate and inspection"),
    ("EMV", "EM&V"),
    ("UserInputIncentive", "Incentives entered at program level"),
    ("CostsRecoveredFromOtherSources", "Costs recovered from other sources"),
]
FILING_KINDS = [
    ("Business plan application", "Business plan application"),
    ("True-up advice letter (TUAL)", "True-up advice letter (TUAL)"),
    ("Mid-cycle advice letter (MCAL)", "Mid-cycle advice letter (MCAL)"),
]


class FilingSetup(models.Model):
    """How one budget filing is priced: a filing is a deadline of the "budgets" module on a filing cycle (the submissions service's
    calendar); this says which ACC it is priced on and, optionally, which statewide allocation version (blank: the most recent confirmed
    version with factors for the program year, as any run). Set by a CPUC system administrator, a setting rather than code."""

    deadline = models.OneToOneField("submissions.Deadline", on_delete=models.PROTECT, related_name="budget_setup")
    acc_version = models.CharField("ACC", max_length=10)
    allocation_version = models.ForeignKey(
        "registry.AllocationVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="budget_filings"
    )
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.deadline.kind} {self.deadline.cycle}"


class BudgetLine(models.Model):
    """One program's (or portfolio cost ID's) filed budget for one program year in one version, read from the version's own files: the
    ProgramCost row's fields, plus the rebates and incentives of the program's measures in that year (NumUnits x the four unit incentive
    fields). Derived data of the version: replaced when its files are, deleted with it."""

    version = models.ForeignKey("submissions.SubmissionVersion", on_delete=models.CASCADE, related_name="budget_lines")
    prg_id = models.CharField("PrgID", max_length=60)
    year = models.PositiveSmallIntegerField()
    pa = models.CharField("PA", max_length=20, blank=True, help_text="The ProgramCost row's PA")
    program_costs = models.JSONField(default=dict, help_text="{ProgramCost field: dollars} for the ten cost fields")
    on_bill_financing = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    rebates_and_incentives = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    measures = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["prg_id", "year"]
        constraints = [models.UniqueConstraint(fields=["version", "prg_id", "year"], name="budgets_one_line")]

    def __str__(self):
        return f"{self.prg_id} {self.year}"


class BudgetResult(models.Model):
    """One program's priced results for one program year and credited PA in one version, summed from the run's group sums (the outputs
    of every measure, ratios recomputed from the sums). Kept per run: a re-priced version reads its new run's results."""

    version = models.ForeignKey("submissions.SubmissionVersion", on_delete=models.CASCADE, related_name="budget_results")
    run = models.CharField(max_length=40)
    pa = models.CharField("credited PA", max_length=20, blank=True)
    prg_id = models.CharField("PrgID", max_length=60)
    program_name = models.CharField(max_length=300, blank=True)
    year = models.PositiveSmallIntegerField(default=0, help_text="0 when the run gives none")
    measures = models.PositiveIntegerField(default=0)
    values = models.JSONField(default=dict, help_text="{output column: sum over the program's measures}")

    class Meta:
        ordering = ["prg_id", "year", "pa"]
        constraints = [models.UniqueConstraint(fields=["version", "run", "pa", "prg_id", "year"], name="budgets_one_result")]

    def __str__(self):
        return f"{self.prg_id} {self.year} {self.pa}"


class FundShift(models.Model):
    """A fund shift (BR-14.4): dollars moved between programs within a filing cycle, not part of any filing and with no
    cost-effectiveness run. Each PA's part of it adds up to zero. Moving money into or out of a statewide program changes every funding
    PA's portion of it by its allocation factor; each other funding PA (a co-funder) decides where its portion comes from or goes in its
    own portfolio and acknowledges the shift, which takes effect once all have (at once when there is none). The approved filed budget
    never changes; the operating budgets do (BR-04.7)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        WAITING = "waiting", "Waiting for co-funders"
        EFFECTIVE = "effective", "In effect"
        WITHDRAWN = "withdrawn", "Withdrawn"

    organisation = models.ForeignKey("accounts.Organisation", on_delete=models.PROTECT, related_name="fund_shifts")
    cycle = models.ForeignKey("registry.FilingCycle", on_delete=models.PROTECT, related_name="fund_shifts")
    title = models.CharField(max_length=200)
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    effective_at = models.DateTimeField(null=True, blank=True)
    shares = models.JSONField(
        default=dict,
        blank=True,
        help_text="Set on submission: {funding PA: {'PrgID year': its portion of the statewide line}}",
    )
    withdrawn_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.organisation.code} fund shift {self.pk}: {self.title}"

    @property
    def cofunders(self) -> list[str]:
        """The other funding PAs whose portion changes, in code order."""
        return sorted(c for c in (self.shares or {}) if c != self.organisation.code)


class FundShiftLine(models.Model):
    """One program-year's amount in a shift: negative moves dollars out of it, positive into it. `pa` is whose portfolio the line is
    in: the shift's PA for the lines it entered (statewide ones included), a co-funder for the lines that place its portion."""

    shift = models.ForeignKey(FundShift, on_delete=models.CASCADE, related_name="lines")
    pa = models.CharField("PA", max_length=20)
    prg_id = models.CharField("PrgID", max_length=60)
    year = models.PositiveSmallIntegerField()
    amount = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        ordering = ["pa", "year", "prg_id"]
        constraints = [
            models.UniqueConstraint(fields=["shift", "pa", "prg_id", "year"], name="budgets_one_shift_line"),
            models.CheckConstraint(condition=~models.Q(amount=0), name="budgets_shift_line_not_zero"),
        ]

    def __str__(self):
        return f"{self.prg_id} {self.year}: {self.amount}"


class FundShiftAcknowledgement(models.Model):
    """A co-funder's acknowledgement of a shift that moves its share of a statewide program."""

    shift = models.ForeignKey(FundShift, on_delete=models.CASCADE, related_name="acknowledgements")
    organisation = models.ForeignKey("accounts.Organisation", on_delete=models.PROTECT, related_name="+")
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    acknowledged_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shift", "organisation"], name="budgets_one_acknowledgement")]

    def __str__(self):
        return f"{self.organisation.code} acknowledged {self.shift_id}"
