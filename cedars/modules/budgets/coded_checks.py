"""The budget filing module's own checks (rule IDs BUD-*), listed on the Rules pages (rules.coded). Keep this list in step with
service.py: tests/test_coded_checks.py compares them."""

from cedars.services.rules.coded import CodedCheck as C
from cedars.services.rules.coded import choosable

ABOUT = (
    "Run on every budget filing upload before the budget filing rule set: the programs, the cycle's years and the numbers each budget "
    "line is built from."
)
CHECKS = [
    C("BUD-PRG", "reject", "ProgramCost, Measure", "PrgID", "PrgID is not in the program registry.",
      "Use the PrgID of a program in the Programs catalog, or ask for the program to be added first."),
    C("BUD-ORG", "reject", "ProgramCost, Measure", "PrgID",
      "The program is not one the filing PA may file: another PA's program, or a statewide program it does not lead.",
      "File only your own programs and the statewide programs you lead."),
    C("BUD-YEAR", "reject", "ProgramCost, Measure", "PrgYear, ClaimYearQuarter", "The year is not a year of the filing's cycle.",
      "Use a year from the cycle's first year to its last."),
    C("BUD-NUM", "reject", "ProgramCost, Measure", "The program cost fields, OnBillFinancing, NumUnits and the incentive fields",
      "A cost, count or incentive is not a number.", "Enter a plain number."),
    C("BUD-DUP", "reject", "ProgramCost", "PrgYear", "A program has two ProgramCost rows for one year.",
      "Keep one row per program and year."),
    C("BUD-NOCOST", "warning", "Measure", "PrgID",
      "A program's measures have no ProgramCost row for their year, so they are priced with no program costs.",
      "Add the program's ProgramCost row for that year if it has program costs."),
    C("BUD-COL", "reject", "ProgramCost, Measure", "Any column of the filing specification",
      "The table lacks a column of the filing specification, so its rules were not run.", "Add the column (it may be blank)."),
    C("BUD-RULES", "reject", "ProgramCost, Measure", "",
      "The rule set in force could not be applied to a year's rows, so the version cannot be called valid.",
      "Tell the CPUC's CEDARS support; the file may be fine."),
]  # fmt: skip

# The checks a specification version may leave out or make a warning or a reject:
# the warning about measures with no program costs.
# The others are CEDARS' own processing and always run.
CHOOSABLE = {"BUD-NOCOST"}
CHECKS = [choosable(c) if c.rule_id in CHOOSABLE else c for c in CHECKS]
