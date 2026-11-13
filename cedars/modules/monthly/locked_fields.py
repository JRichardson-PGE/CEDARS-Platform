"""The monthly report fields the module's code reads, which a draft of the monthly reports specification cannot remove, rename or
retype, nor make optional where they are required (specifications.editor.register_locked_fields). tests/test_specification_editor.py
fails on a field name the module's code names that this list leaves out."""

PROGRAM = "CEDARS matches it to the program registry and the filing PA's programs."
PERIOD = "CEDARS places each record in its month or year with it."
STORED = "CEDARS keeps it with each confirmed report for the spending dashboard and the 13th-month report."
KEY = "CEDARS identifies each measure by it."

LOCKED = {
    "Commitments": {
        "PA": PROGRAM, "PrgID": PROGRAM, "YearMonth": PERIOD, "Comments": STORED,
        **{f: STORED for f in (
            "PriorCommittedFundsBalance", "PriorYTDCommittedExpenditures", "YTDCommittedFunds", "YTDCommittedGrosskWh",
            "YTDCommittedGrosskW",
            "YTDCommittedGrossTherm", "YTDCommittedNetkWh", "YTDCommittedNetkW", "YTDCommittedNetTherm",
        )},
    },
    "Measure": {
        "CEInputID": KEY, "PrgID": PROGRAM, "ClaimYearQuarter": PERIOD, "Version": STORED, "Comments": STORED,
        **{f: STORED for f in ("NumUnits", "UnitDirectInstallLab", "UnitDirectInstallMat", "UnitEndUserRebate", "UnitIncentiveToOthers")},
    },
    "ProgramCost": {
        "PrgID": PROGRAM, "PrgYear": PERIOD, "ClaimYearQuarter": PERIOD, "PA": PROGRAM, "OnBillFinancing": STORED,
        **{f: STORED for f in (
            "AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation", "DIHardwareAndMaterials",
            "DIRebateAndInspection", "EMV", "UserInputIncentive", "CostsRecoveredFromOtherSources",
        )},
    },
}  # fmt: skip
