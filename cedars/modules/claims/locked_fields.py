"""The claims fields the module's code reads, which a draft of the Quarterly claims specification cannot remove, rename or retype, nor
make optional where they are required (specifications.editor.register_locked_fields). tests/test_specification_editor.py fails on a field
name the module's code names that this list leaves out."""

LINKS = "CEDARS links a claim's records to each other by it."
PROGRAM = "CEDARS matches it to the program registry and the filing PA's programs."
PERIOD = "CEDARS places each record in the claim year and quarter with it."
VAULT = "CEDARS links the record to its PII vault record by it."
STORED = "CEDARS keeps it with each confirmed claim for published totals, dashboards and record pages."
CHECKED = "A built-in check reads it."
FILLED = "CEDARS reads it to fill in a deemed claim from eTRM or to total the claim."
WITHDRAWAL = "CEDARS withdraws a claim with it."

LOCKED = {
    "Claim": {
        "ClaimID": LINKS, "ParentClaimID": LINKS, "SiteID": VAULT, "ParticipantID": LINKS, "ProjectID": CHECKED, "ClaimType": CHECKED,
        "SEM_Phase": CHECKED, "MeasImpactCalcType": CHECKED, "MeasDetailID": FILLED, "NMEC_TU_Flag": CHECKED,
        "PartialPaymentPercent": CHECKED, "PartialPaymentFinal_Flag": CHECKED, "CCAREN_Flag": CHECKED,
        "TotalCustomerInfrastructureCosts": CHECKED,
        **{f: STORED for f in (
            "TotalFirstYearGrosskW", "TotalFirstYearGrosskWh", "TotalFirstYearGrossTherm", "TotalFirstYearNetkW", "TotalFirstYearNetkWh",
            "TotalFirstYearNetTherm", "TotalLifecycleNetkWh", "TotalLifecycleNetTherm", "TotalGrossMeasureCost", "TotalGrossIncentive",
        )},
    },
    "Measure": {
        "ClaimID": LINKS, "PrgID": PROGRAM, "ClaimYearQuarter": PERIOD, "MeasImpactType": CHECKED, "Residential_Flag": CHECKED,
        **{f: FILLED for f in (
            "AC_Elec_Impact_Profile", "AC_Elec_Target_Sector", "IOU_AC_Territory", "NumUnits", "UnitDirectInstallLab",
            "UnitDirectInstallMat", "UnitEndUserRebate", "UnitIncentiveToOthers",
        )},
    },
    "ProgramCost": {
        "PrgID": PROGRAM, "PrgYear": PERIOD, "ClaimYearQuarter": PERIOD, "PA": PROGRAM, "OnBillFinancing": STORED,
        **{f: STORED for f in (
            "AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation", "DIHardwareAndMaterials",
            "DIRebateAndInspection", "EMV", "UserInputIncentive", "CostsRecoveredFromOtherSources",
        )},
    },
    "Site": {"SiteID": VAULT, "SiteState": CHECKED, "SiteZipCode": CHECKED, "Residential_Flag": CHECKED, "NAICSCode": CHECKED},
    "ContactClaim": {"ClaimID": LINKS, "ContactID": VAULT},
    "Participant": {"PrgID": PROGRAM, "PrgYear": PERIOD, "SiteID": VAULT},
    "InfrastructureCost": {"ClaimID": LINKS, "SiteID": VAULT, "PrgID": PROGRAM},
    "ClaimSite": {"ClaimID": LINKS, "SiteID": VAULT},
    "Withdrawal": {"ClaimID": LINKS, "WithdrawalReason": WITHDRAWAL, "WithdrawnIn": WITHDRAWAL},
}  # fmt: skip
