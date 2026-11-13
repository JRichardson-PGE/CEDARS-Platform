"""The budget filing fields the module's code reads, which a draft of the budget and application filings specification cannot remove,
rename or retype, nor make optional where they are required (specifications.editor.register_locked_fields). tests/test_specification_
editor.py fails on a field name the module's code names that this list leaves out."""

PROGRAM = "CEDARS matches it to the program registry and the filing PA's programs."
PERIOD = "CEDARS places each record in its program year with it."
STORED = "CEDARS keeps it with each confirmed filing for published budgets, dashboards and fund shifts."
KEY = "CEDARS identifies each measure by it."

LOCKED = {
    "Measure": {
        "CEInputID": KEY, "PrgID": PROGRAM, "ClaimYearQuarter": PERIOD, "Version": STORED,
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
