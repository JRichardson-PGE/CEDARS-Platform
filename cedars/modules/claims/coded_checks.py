"""The claims module's own checks (rule IDs CLM-*), listed on the Rules pages (rules.coded). Keep this list in step with service.py:
tests/test_coded_checks.py compares them."""

from cedars.services.rules.coded import AsRule as R
from cedars.services.rules.coded import CodedCheck as C
from cedars.services.rules.coded import choosable, written_as

ABOUT = (
    "Run on every claims upload beside the claims rule set (which covers Measure and ProgramCost): the template's columns and types for "
    "the other tables, keys and IDs, the claim year, the programs, the links between the tables over the year to date, withdrawals, "
    "NMEC true-up chains, CEDARS' data rules for the Claim and Site tables, and a deemed claim's values against eTRM."
)
CHECKS = [
    C("CLM-COL", "reject", "Any table", "Any required column",
      "The table lacks a column the template requires (for Measure and ProgramCost, a column of the claims specification, so their "
      "rules were not run).", "Add the column (it may be blank)."),
    C("CLM-REQ", "reject", "Tables the claims rule set does not cover", "Any required field", "A required field is blank.",
      "Fill in the field."),
    C("CLM-TYPE", "reject", "Tables the claims rule set does not cover", "Any typed field",
      "A value is not of the field's type (a number, a date, a flag, a year and quarter) or is too long.",
      "Enter the value in the field's type, as the template describes it."),
    C("CLM-KEY", "reject", "Tables keyed by their own records", "The table's key", "Two records of the table have the same key.",
      "Keep one record per key; to correct a record, change it in place."),
    C("CLM-ID", "reject", "Claim, Measure, Site", "ClaimID, SiteID",
      "A ClaimID does not start with the PA code and claim year (or SW and the year), or a SiteID with the PA code (or SW).",
      "Start each ClaimID with the PA code and claim year, for example PGE-2027-0001, and each SiteID with the PA code."),
    C("CLM-YEAR", "reject", "Measure, ProgramCost, Participant", "ClaimYearQuarter, PrgYear",
      "ClaimYearQuarter is not a quarter of the claim year up to this upload's quarter, or PrgYear is not the claim year.",
      "Use the claim year's quarters up to this one; later claims go in their own quarter's upload."),
    C("CLM-PRG", "reject", "Measure, ProgramCost, Participant, InfrastructureCost", "PrgID", "PrgID is not in the program registry.",
      "Use the PrgID of a program in the Programs catalog, or ask for the program to be added first."),
    C("CLM-SW", "reject", "Measure, ProgramCost, Participant, InfrastructureCost", "PrgID",
      "A statewide program in a PA's own claims, or a program that is not statewide in the statewide claims filing.",
      "Statewide programs are claimed by their lead PA in the statewide claims filing; claim the PA's own programs in its own claims."),
    C("CLM-ORG", "reject", "Measure, ProgramCost, Participant, InfrastructureCost", "PrgID",
      "The program is not one the filing PA may claim.", "Claim only your own programs and the statewide programs you lead."),
    C("CLM-REF", "reject", "Measure, Claim, ClaimSite, Participant and the tables keyed by claim", "ClaimID, SiteID, PrgID",
      "A record names a claim, site or program record that the year to date does not have: a measure without its claim or its "
      "program's ProgramCost record, a claim without its measure, a site without its Site record.",
      "Add the missing record (in this upload or an earlier confirmed quarter's)."),
    C("CLM-WD", "reject", "Withdrawal and the claim tables", "ClaimID",
      "A withdrawal of a claim not filed this year or already withdrawn, or records filed for a claim that is withdrawn.",
      "Withdraw a confirmed claim once and leave its records as they were filed; file it again under a new ClaimID."),
    C("CLM-CHAIN", "reject or warning", "Claim", "ParentClaimID",
      "A true-up's parent is the claim itself, a claim the PA has not filed, a withdrawn claim or one of its own true-ups (reject), "
      "or not a NMEC claim (warning).", "Give the ClaimID of the claim the true-up adjusts."),
    C("CLM-SITE", "reject", "Site", "SiteState, SiteZipCode, NAICSCode",
      "The site is not in California, its ZIP code is not a California ZIP code, or a non-residential site has no NAICS code.",
      "Enter CA, the site's five-digit California ZIP code and, for a non-residential site, its NAICS code."),
    C("CLM-NMEC", "reject", "Claim", "ParentClaimID, NMEC_TU_Flag",
      "A NMEC true-up has no ParentClaimID, or a NMEC claim has no NMEC_TU_Flag.",
      "Give the claim the true-up adjusts, and enter 1 or 0 in NMEC_TU_Flag."),
    C("CLM-SEM", "reject", "Claim", "ParticipantID, SEM_Phase",
      "A SEM claim lacks its ParticipantID or SEM_Phase, or SEM_Phase is filled in on a claim that is not SEM.",
      "Fill in both on a SEM claim (MeasImpactType Cust-SEM or Cust-SEM-FuelSub), and leave SEM_Phase blank otherwise."),
    C("CLM-DEEMED", "reject", "Claim", "MeasDetailID, ProjectID, MeasImpactCalcType",
      "A deemed claim lacks its MeasDetailID, ProjectID or MeasImpactCalcType.", "Fill in the field: deemed claims need it."),
    C("CLM-FUELSUB", "reject", "Claim", "TotalCustomerInfrastructureCosts",
      "A fuel substitution claim has no TotalCustomerInfrastructureCosts.",
      "Enter the customer's infrastructure costs (0 if there were none) and add its InfrastructureCost record."),
    C("CLM-PAYMENT", "reject", "Claim", "PartialPaymentPercent",
      "PartialPaymentPercent is not between 0 and 1, or is blank on a final partial payment.",
      "Enter the share paid as a fraction, for example 0.5 for half."),
    C("CLM-CCAREN", "reject", "Claim", "CCAREN_Flag", "CCAREN_Flag is not 1 on a claim filed by a CCA or REN.",
      "Enter 1: every claim a CCA or REN files has it."),
    C("CLM-ETRM", "reject or warning", "Claim, Measure", "MeasDetailID, the values eTRM gives a permutation, ClaimYearQuarter",
      "A deemed claim's MeasDetailID is not a permutation of its measure in eTRM, or a value differs from eTRM's (reject; a few "
      "fields only warn); or the measure version is not in the eTRM data yet, or the permutation is not in effect in the claim's "
      "quarter (warning).",
      "Enter eTRM's value or leave it blank to have it filled in, and give the MeasDetailID of the permutation installed."),
    C("CLM-RULES", "reject", "Measure, ProgramCost", "",
      "The claims rule set in force could not be applied, so the version cannot be called valid.",
      "Tell the CPUC's CEDARS support; the file may be fine."),
    C("CLM-LIST", "reject", "Any table", "A field whose codes come from a value list made for the specification",
      "The value is not a code of the value list, or not one accepted for the record's year.",
      "Use a code from the value list that is accepted for the year (the list's page shows each code's first and last year)."),
]  # fmt: skip

# The checks a specification version may leave out or make a warning or a reject:
# CEDARS' data rules on the Claim and Site tables.
# The others are CEDARS' own processing and always run.
CHOOSABLE = {"CLM-SITE", "CLM-SEM", "CLM-FUELSUB", "CLM-PAYMENT", "CLM-NMEC", "CLM-CCAREN", "CLM-DEEMED"}
CHECKS = [choosable(c) if c.rule_id in CHOOSABLE else c for c in CHECKS]

# The checks a draft can write as rules of its rule set (specifications.editor.write_as_rules): each set of rules gives the check's
# findings, record for record and field for field (tests/test_coded_as_rules.py). A claim's measure is its Measure record, linked by
# ClaimID. CLM-SITE stays in code: its ZIP code test needs a pattern no rule test has yet.
SEM = "Cust-SEM, Cust-SEM-FuelSub"
AS_RULES = {
    "CLM-NMEC": (
        R("Claim", "ParentClaimID", "is not null", conds=(("", "NMEC_TU_Flag", "is true", ""),)),
        R("Claim", "NMEC_TU_Flag", "is not null", conds=(("Measure", "MeasImpactType", "contains", "NMEC"),)),
    ),
    "CLM-SEM": (
        R("Claim", "ParticipantID", "is not null", conds=(("Measure", "MeasImpactType", "in", SEM),)),
        R("Claim", "SEM_Phase", "is not null", conds=(("Measure", "MeasImpactType", "in", SEM),)),
        R("Claim", "SEM_Phase", "is null",
          conds=(("Measure", "MeasImpactType", "not in", SEM), ("Measure", "MeasImpactType", "is not null", ""))),
    ),
    "CLM-DEEMED": tuple(
        R("Claim", f, "is not null", conds=(("", "ClaimType", "equals", "'Deemed'"),))
        for f in ("MeasDetailID", "ProjectID", "MeasImpactCalcType")
    ),
    "CLM-FUELSUB": (
        R("Claim", "TotalCustomerInfrastructureCosts", "is not null",
          conds=(("Measure", "MeasImpactType", "ends with string", "FuelSub"),)),
    ),
    "CLM-PAYMENT": (
        # between 0 and 1 where it is a number (a blank or a text is not this check's)
        R("Claim", "PartialPaymentPercent", "is greater than or equal to", "0",
          conds=(("", "PartialPaymentPercent", "is less than or equal to", "1"),)),
        R("Claim", "PartialPaymentPercent", "is less than or equal to", "1",
          conds=(("", "PartialPaymentPercent", "is greater than or equal to", "0"),)),
        R("Claim", "PartialPaymentPercent", "is not null", conds=(("", "PartialPaymentFinal_Flag", "is true", ""),)),
    ),
    "CLM-CCAREN": (R("Claim", "CCAREN_Flag", "is true", conds=(("Filer", "PAType", "in", "CCA, REN"),)),),
}  # fmt: skip
CHECKS = [written_as(c, *AS_RULES[c.rule_id]) if c.rule_id in AS_RULES else c for c in CHECKS]
