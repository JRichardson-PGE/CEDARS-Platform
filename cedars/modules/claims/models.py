from django.db import models

# The ten ProgramCost fields CET adds up into a program's cost, in the specification's order (as Budgets reads them).
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
# The savings and cost totals a PA files on each claim (Claim table), summed per program.
FILED_TOTALS = [
    "TotalFirstYearGrosskWh",
    "TotalFirstYearGrosskW",
    "TotalFirstYearGrossTherm",
    "TotalFirstYearNetkWh",
    "TotalFirstYearNetkW",
    "TotalFirstYearNetTherm",
    "TotalLifecycleNetkWh",
    "TotalLifecycleNetTherm",
    "TotalGrossMeasureCost",
    "TotalGrossIncentive",
]


class ClaimYear(models.Model):
    """One claim (program) year: its five confirmations are deadlines of the "claims" module on the year's quarters and on the year
    (Q1 to Q4, then the final annual one), and this says how its claims are priced: the ACC (claims use the ACC of the year's final
    budget filing) and, optionally, the statewide allocation version (blank: the most recent confirmed version with factors for the
    year). Set by a CPUC system administrator, a setting rather than code."""

    year = models.PositiveSmallIntegerField(unique=True)
    acc_version = models.CharField("ACC", max_length=10)
    allocation_version = models.ForeignKey(
        "registry.AllocationVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="claim_years"
    )
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return f"{self.year} claims"


class ClaimProgramLine(models.Model):
    """One program's year-to-date claims in one version, read from the version's compiled tables (the earlier confirmed quarters with
    this version's records over them): the ProgramCost row's fields, the measures' rebates and incentives (NumUnits x the four unit
    incentive fields), and the totals filed on its claims. Derived data of the version: replaced when its files are, deleted with it."""

    version = models.ForeignKey("submissions.SubmissionVersion", on_delete=models.CASCADE, related_name="claim_lines")
    prg_id = models.CharField("PrgID", max_length=60)
    pa = models.CharField("PA", max_length=20, blank=True, help_text="The ProgramCost row's PA")
    program_costs = models.JSONField(default=dict, help_text="{ProgramCost field: dollars} for the ten cost fields")
    on_bill_financing = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    rebates_and_incentives = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    claims = models.PositiveIntegerField(default=0)
    filed = models.JSONField(default=dict, help_text="{Claim total field: sum over the program's claims} as filed")

    class Meta:
        ordering = ["prg_id"]
        constraints = [models.UniqueConstraint(fields=["version", "prg_id"], name="claims_one_line")]

    def __str__(self):
        return self.prg_id


class ClaimResult(models.Model):
    """One program's priced year-to-date results for one credited PA in one version, summed from the run's group sums. Kept per run: a
    re-priced version reads its new run's results."""

    version = models.ForeignKey("submissions.SubmissionVersion", on_delete=models.CASCADE, related_name="claim_results")
    run = models.CharField(max_length=40)
    pa = models.CharField("credited PA", max_length=20, blank=True)
    prg_id = models.CharField("PrgID", max_length=60)
    program_name = models.CharField(max_length=300, blank=True)
    measures = models.PositiveIntegerField(default=0)
    values = models.JSONField(default=dict, help_text="{output column: sum over the program's measures}")

    class Meta:
        ordering = ["prg_id", "pa"]
        constraints = [models.UniqueConstraint(fields=["version", "run", "pa", "prg_id"], name="claims_one_result")]

    def __str__(self):
        return f"{self.prg_id} {self.pa}"


class ChangeLetter(models.Model):
    """The public change letter of a claims correction (BR-09.3): written when a corrected version (version 2 or later) of a confirmation
    is published, dated, with the correction's reason and what it changed against the version it replaces: the claims added, changed,
    removed, withdrawn (with their reasons) or reinstated, each table's records, and each program's year to date before and after.
    Readable by every signed-in user, like the versions it compares. Append-only: never updated or deleted (a database trigger)."""

    version = models.OneToOneField("submissions.SubmissionVersion", on_delete=models.PROTECT, related_name="change_letter")
    previous = models.ForeignKey("submissions.SubmissionVersion", on_delete=models.PROTECT, related_name="+")
    reason = models.TextField(help_text="The correction's reason, as given when it was reopened")
    published_at = models.DateTimeField()
    changes = models.JSONField(default=dict, help_text="claims, tables, programs and totals, as service.correction_changes writes them")

    class Meta:
        ordering = ["-published_at", "-pk"]

    def __str__(self):
        return f"Change letter for version {self.version.number} of {self.version.submission}"
