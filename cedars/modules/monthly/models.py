from django.db import models

# A month's spend is read from its year-to-date BCA inputs (Jake, 2026-10-30): each program's ProgramCost row (the ten fields CET adds up
# into program costs) plus its measures' rebates and incentives (NumUnits x the four unit fields), as Budgets counts a budget, so the
# share of the operating budget spent compares like with like. OnBillFinancing (loan pools) is kept apart, as in Budgets.
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
INCENTIVE_FIELDS = ["UnitIncentiveToOthers", "UnitDirectInstallLab", "UnitDirectInstallMat", "UnitEndUserRebate"]
SPEND_FIELDS = [f for f, _ in PROGRAM_COST_FIELDS] + ["RebatesAndIncentives", "OnBillFinancing", "YTDExpenditures"]
# The commitments, their own table (D2), with the committed columns of CEDARS' monthly Program table (2026-03-11 specification) as
# they are reported today (Jake, 2026-10-30).
COMMITMENT_FIELDS = [
    ("PriorCommittedFundsBalance", "Committed funds from prior years, balance"),
    ("PriorYTDCommittedExpenditures", "Prior years' commitments spent, year to date"),
    ("YTDCommittedFunds", "Funds committed, year to date"),
    ("YTDCommittedGrosskWh", "Committed gross kWh, year to date"),
    ("YTDCommittedGrosskW", "Committed gross kW, year to date"),
    ("YTDCommittedGrossTherm", "Committed gross therms, year to date"),
    ("YTDCommittedNetkWh", "Committed net kWh, year to date"),
    ("YTDCommittedNetkW", "Committed net kW, year to date"),
    ("YTDCommittedNetTherm", "Committed net therms, year to date"),
]


class YearSetup(models.Model):
    """How one program year's monthly reports are set up: their twelve deadlines (the submissions service's calendar, module "monthly",
    one per month) and what the year-to-date BCA inputs are priced on, the ACC and optionally a statewide allocation version (blank: the
    most recent confirmed version with factors for the year). Set by a CPUC system administrator: a setting, not code."""

    year = models.PositiveSmallIntegerField(unique=True)
    acc_version = models.CharField("ACC", max_length=10)
    allocation_version = models.ForeignKey(
        "registry.AllocationVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="monthly_years"
    )
    due_day = models.PositiveSmallIntegerField(help_text="Each month's report is due on this day of the following month")
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return f"{self.year} monthly reports"


class ReportLine(models.Model):
    """One program's spend (read from the version's BCA inputs) or its row of the Commitments table: derived data of the version's own
    upload, replaced when the upload is, deleted with the version."""

    class Table(models.TextChoices):
        SPEND = "Spend", "Spend (from the BCA inputs)"
        COMMITMENTS = "Commitments", "Commitments"

    version = models.ForeignKey("submissions.SubmissionVersion", on_delete=models.CASCADE, related_name="monthly_lines")
    table = models.CharField(max_length=20, choices=Table.choices)
    prg_id = models.CharField("PrgID", max_length=60)
    pa = models.CharField("PA", max_length=20, blank=True)
    values = models.JSONField(default=dict, help_text="{field: dollars or units, as text}")
    comments = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["table", "prg_id"]
        constraints = [models.UniqueConstraint(fields=["version", "table", "prg_id"], name="monthly_one_line")]

    def __str__(self):
        return f"{self.table} {self.prg_id}"


class MonthlyResult(models.Model):
    """One program's estimated results for one credited PA in one version, summed from the priced run's group sums: unvalidated
    estimates (BR-14.5), never part of a claim. Kept per run: a re-priced version reads its new run's results."""

    version = models.ForeignKey("submissions.SubmissionVersion", on_delete=models.CASCADE, related_name="monthly_results")
    run = models.CharField(max_length=40)
    pa = models.CharField("credited PA", max_length=20, blank=True)
    prg_id = models.CharField("PrgID", max_length=60)
    program_name = models.CharField(max_length=300, blank=True)
    measures = models.PositiveIntegerField(default=0)
    values = models.JSONField(default=dict, help_text="{output column: sum over the program's measures}")

    class Meta:
        ordering = ["prg_id", "pa"]
        constraints = [models.UniqueConstraint(fields=["version", "run", "pa", "prg_id"], name="monthly_one_result")]

    def __str__(self):
        return f"{self.prg_id} {self.pa}"


class ChangeLetter(models.Model):
    """The public change letter of a monthly report's correction (BR-09.3): written when a corrected version (version 2 or later) is
    published, dated, with the correction's reason and each program whose year-to-date spend, commitments or estimates changed against
    the version it replaces, before and after, with the totals (service.LETTER_FIGURES). Readable by anyone, like Claims' change
    letters. Append-only: never updated or deleted (a database trigger)."""

    version = models.OneToOneField("submissions.SubmissionVersion", on_delete=models.PROTECT, related_name="monthly_change_letter")
    previous = models.ForeignKey("submissions.SubmissionVersion", on_delete=models.PROTECT, related_name="+")
    reason = models.TextField(help_text="The correction's reason, as given when it was reopened")
    published_at = models.DateTimeField()
    changes = models.JSONField(default=dict, help_text="programs and totals, as service.correction_changes writes them")

    class Meta:
        ordering = ["-published_at", "-pk"]

    def __str__(self):
        return f"Change letter for version {self.version.number} of {self.version.submission}"
