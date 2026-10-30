from django.db import models

# The program-level tables of a monthly report, in their files' column order. CEDARS' monthly Program table (2026-03-11 specification)
# split in two: the expenditures, and the commitments as their own file (D2, confirmed 2026-10-29). Its MonthFundShifts column is gone
# (fund shifts are made in Budgets, BR-14.4) and so are its savings columns: savings and TSB are estimated from the BCA inputs (BR-14.5).
EXPENDITURE_FIELDS = [
    ("MonthExpenditures", "Expenditures this month"),
    ("YTDExpenditures", "Expenditures, year to date"),
    ("MonthIncentivesPaid", "Incentives paid this month"),
    ("YTDIncentivesPaid", "Incentives paid, year to date"),
    ("Carryover", "Carryover"),
]
COMMITMENT_FIELDS = [
    ("PriorCommittedFundsBalance", "Committed funds from prior years, balance"),
    ("PriorYTDCommittedExpenditures", "Prior years' commitments spent, year to date"),
    ("YTDCommittedFunds", "Funds committed, year to date"),
    ("YTDIncentivesCommitted", "Incentives committed, year to date"),
    ("YTDCommittedGrosskWh", "Committed gross kWh, year to date"),
    ("YTDCommittedGrosskW", "Committed gross kW, year to date"),
    ("YTDCommittedGrossTherm", "Committed gross therms, year to date"),
    ("YTDCommittedNetkWh", "Committed net kWh, year to date"),
    ("YTDCommittedNetkW", "Committed net kW, year to date"),
    ("YTDCommittedNetTherm", "Committed net therms, year to date"),
]
TABLE_FIELDS = {"Expenditures": EXPENDITURE_FIELDS, "Commitments": COMMITMENT_FIELDS}


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
    """One program's row of a version's Expenditures or Commitments table, read from the version's own upload: derived data, replaced
    when the upload is, deleted with the version."""

    class Table(models.TextChoices):
        EXPENDITURES = "Expenditures", "Expenditures"
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
