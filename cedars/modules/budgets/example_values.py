"""The values the budgets module gives a specification version's example records (specifications.examples) for the fields CEDARS'
rules read, so the example is a fully valid budget filing. They only describe fields: a value for a field a draft takes away is not
used, so the fields named here need no lock (the fields the module's code reads are in locked_fields)."""

# what CEDARS' rules accept for the fields they read: a directly installed measure described by codes from its lists, its profiles,
# an IOU's territory, no remaining useful life (so no second baseline), and each program cost given (CEDARS' published rules want
# them filled; a version that follows its fields reads a blank as 0)
EXAMPLE_VALUES = {
    "Sector": "Agricultural", "DeliveryType": "DnDeemDI", "BldgType": "AgOth", "MeasAppType": "AOE", "MeasDescription": "Example measure",
    "MeasImpactType": "Cust-Gen", "TechType": "Agitation", "UseSubCategory": "AirDist", "NormUnit": "Area-100Win",
    "AC_Elec_Impact_Profile": "DEER:Indoor_CFL_Ltg", "AC_Elec_Target_Sector": "COMMERCIAL", "IOU_AC_Territory": "PGE",
    "NTG_ID": "Com-Default>2yrs", "RUL_Yrs": "0", "UnitkW2ndBaseline": "0", "UnitkWh2ndBaseline": "0", "UnitTherm2ndBaseline": "0",
    "UnitMeaCost2ndBaseline": "0", "UnitkWhIOUWater2ndBaseline": "0",
    **dict.fromkeys(("AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation",
                     "DIHardwareAndMaterials", "DIRebateAndInspection", "EMV", "UserInputIncentive", "OnBillFinancing",
                     "CostsRecoveredFromOtherSources"), "0"),
}  # fmt: skip
