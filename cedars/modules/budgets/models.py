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
