"""The monthly report module's own checks (rule IDs MON-*), listed on the Rules pages (rules.coded). Keep this list in step with
service.py: tests/test_coded_checks.py compares them."""

from cedars.services.rules.coded import CodedCheck as C

ABOUT = (
    "Run on every monthly report upload: the programs, the report's PA, month and year, the numbers, and the year to date against "
    "the previous month's report."
)
CHECKS = [
    C("MON-PRG", "reject", "Commitments, ProgramCost, Measure", "PrgID", "PrgID is not in the program registry.",
      "Use the PrgID of a program in the Programs catalog, or ask for the program to be added first."),
    C("MON-ORG", "reject", "Commitments, ProgramCost, Measure", "PrgID", "The program is another program administrator's program.",
      "Report only your own programs and the statewide programs you lead."),
    C("MON-NUM", "reject", "Commitments, ProgramCost, Measure",
      "The commitment fields, the program cost fields, OnBillFinancing, NumUnits and the incentive fields",
      "A value is not a number (a commitment value may not be blank).", "Enter a plain number (0 when there is nothing to report)."),
    C("MON-PA", "reject", "Commitments", "PA", "PA is not the filing PA.", "Put the filing PA's code in PA: a report holds only its rows."),
    C("MON-MONTH", "reject", "Commitments", "YearMonth", "YearMonth is not the report's month.",
      "Use the report's year and month, written as YYYYMM."),
    C("MON-DUP", "reject", "Commitments, ProgramCost", "PrgID", "A program has two rows in the table.", "Keep one row per program."),
    C("MON-YEAR", "reject", "ProgramCost, Measure", "PrgYear, ClaimYearQuarter",
      "PrgYear is not the report's year, or ClaimYearQuarter is not a quarter of that year up to the report's quarter.",
      "Use the report's year: the program costs and BCA inputs are the year to date."),
    C("MON-NOCOST", "warning", "Measure", "PrgID",
      "A program's measures have no ProgramCost row, so they are priced with no program costs.",
      "Add the program's ProgramCost row for the year if it has program costs."),
    C("MON-YTD", "warning", "ProgramCost", "PrgID",
      "A program's spend year to date is below the previous month's report, or a program with spend last month is missing.",
      "Check the program's ProgramCost row and measures: the BCA inputs are the year to date, so spend should not fall."),
]  # fmt: skip
