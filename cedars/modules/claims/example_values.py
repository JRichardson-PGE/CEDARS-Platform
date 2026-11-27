"""The values the claims module gives a specification version's example records (specifications.examples) for the fields its checks
and CEDARS' rules read, so the example is a fully valid claim. They only describe fields: a value for a field a draft takes away is
not used, so the fields named here need no lock (the fields the module's code reads are in locked_fields)."""

# what the module's checks and CEDARS' rules accept for the fields they read: a site in California; a directly installed measure
# that is not fuel substitution (so CEDARS' rules on profiles and net-to-gross apply) in an IOU's territory; no remaining useful
# life (so no second baseline), no water energy and no miscellaneous costs or benefits (so nothing to describe for them); a standard
# practice described; and the example's year ({year}) in its quarter
EXAMPLE_VALUES = {
    "SiteState": "CA", "SiteZipCode": "94105", "MeasImpactType": "Cust-Gen", "DeliveryType": "DnDeemDI", "IOU_AC_Territory": "PGE",
    "ClaimYearQuarter": "{year}Q1", "RUL_Yrs": "0", "UnitkW2ndBaseline": "0", "UnitkWh2ndBaseline": "0", "UnitTherm2ndBaseline": "0",
    "UnitMeaCost2ndBaseline": "0", "UnitkWhIOUWater1stBaseline": "0", "UnitkWhIOUWater2ndBaseline": "0", "UnitMiscCosts": "0",
    "UnitMiscBens": "0", "StdDesc": "Example standard practice", "AC_Elec_Impact_Profile": "DEER:Indoor_CFL_Ltg",
    "AC_Elec_Target_Sector": "COMMERCIAL", "AC_Gas_Impact_Profile": "Annual", "AC_Gas_Target_Sector": "Commercial",
    "NTG_ID": "Com-Default>2yrs",
}  # fmt: skip
