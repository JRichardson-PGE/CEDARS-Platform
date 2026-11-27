# ruff: noqa: F811 - fixtures imported from other test files
"""CEDARS' 2026 claims data rules on the Claim, ContactClaim and Participant tables, as the claims module's built-in checks: dates
within 9 years of the upload, the hard-to-reach criteria, a claim's customer records, at least one participant, a fuel substitution
claim's original meter year, and keys that start with the PA's code (SW only in the statewide claims filing, PII included)."""

from datetime import date, timedelta

import pytest
from django.utils import timezone

from cedars.modules.claims import service, upload
from cedars.services.accounts.models import Organisation
from cedars.services.registry import service as registry
from cedars.services.rules import coded
from cedars.services.submissions.models import SubmissionVersion
from cedars.services.vault import service as vault

from .test_claims import LAY, claim, claims_zip, deadline, filer, programs, row, tables, year  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


@pytest.fixture
def segmented(pge_admin, pge, programs):
    """A program with a segment and one in category Energy Savings Assistance (the test programs have neither)."""
    registry.create_program(pge_admin, prg_id="PGE_RA", administrator=pge, valid_from=date(2024, 1, 1), name="Resource",
                            segment="resource_acquisition", category="Residential")  # fmt: skip
    registry.create_program(pge_admin, prg_id="PGE_ESA", administrator=pge, valid_from=date(2024, 1, 1), name="ESA",
                            segment="equity", category="Energy Savings Assistance")  # fmt: skip
    registry.create_program(pge_admin, prg_id="PGE_CS", administrator=pge, valid_from=date(2024, 1, 1), name="C&S",
                            segment="codes_and_standards")  # fmt: skip


def test_cedars_2026_claim_rules(filer, year, pge, segmented):
    long_ago = (timezone.localdate() - timedelta(days=366 * 10)).isoformat()
    t = tables(
        claim("PGE-2028-001", prg="PGE_RA"),  # a custom claim on a program with a segment: no ContactClaim, no CMPA_ApplicationID
        claim("PGE-2028-002", prg="PGE_RA", site=""),  # its ContactClaim is there, its SiteID is not
        claim("PGE-2028-003-Audit", prg="PGE_RA"),  # an audit: not checked
        claim("PGE-2028-004", prg="PGE_ESA"),  # Energy Savings Assistance: not checked
        claim("PGE-2028-005"),  # a program without a segment: not checked
        claim("PGE-2028-009", prg="PGE_CS"),  # codes and standards: not checked
        claim("PGE-2028-006", prg="PGE_RA", MeasImpactType="Cust-NMEC-Pop"),  # a NMEC population claim: not checked
        claim("PGE-2028-007", MeasImpactType="Cust-FuelSub", DeliveryType="DnDeemDI"),  # no OriginalMeterInstallYear
        claim("PGE-2028-008", MeasImpactType="Cust-FuelSub", DeliveryType="UpDeemed"),  # upstream deemed: not required
        ProgramCost=[row("ProgramCost", PrgID=p) for p in ("PGE_Res", "PGE_RA", "PGE_ESA", "PGE_CS")],
        Site=[row("Site", SiteID="PGE-S1"), row("Site", SiteID="SW-S2")],  # SW only in the statewide claims filing
        ContactClaim=[row("ContactClaim", ClaimID="PGE-2028-002", ContactID="PGE-C1"),
                      row("ContactClaim", ClaimID="PGE-2028-005", ContactID="SCE-C2")],  # another PA's prefix
        Participant=[row("Participant", PrgID="PGE_Res", PrgYear="2028", ParticipantCount="0"),
                     row("Participant", PrgID="PGE_RA", PrgYear="2028", ParticipantCount="3")],
    )  # fmt: skip
    for r in t["Claim"]:
        r["TotalCustomerInfrastructureCosts"] = "0"
    extra = {
        "PGE-2028-001": {"InstallationDate": long_ago, "HTR_Flag": "1"},  # too old; HTR without a criterion
        "PGE-2028-002": {"PaidDate": "2041-01-01", "DAC_Flag": "1", "ResLI_Flag": "1"},  # too far ahead; a criterion without HTR
        "PGE-2028-005": {"CA_Flag": "1", "HTR_Flag": "1", "PaidDate": ""},  # meets a criterion and says so
        "PGE-2028-008": {"Lang_Flag": "1", "NRSize_Flag": "1", "HTR_Flag": "1"},  # two of the three: no criterion
    }
    for r in t["Claim"]:
        r.update(extra.get(r["ClaimID"], {}))
    own = {name: upload.Table(name, sorted({c for r in rows for c in r}), rows) for name, rows in t.items()}
    problems = service.derive_checks(own, own, pge, 2028, 1, lay=LAY)
    found = {(p.table, p.record_id.split(" / ")[0], p.field, p.rule_id) for p in problems}
    got = {f for f in found if f[3] in ("CLM-CONTACT", "CLM-DATES", "CLM-HTR", "CLM-COUNT", "CLM-FUELSUB", "CLM-ID")}
    assert got == {
        ("Claim", "PGE-2028-001", "ClaimID", "CLM-CONTACT"),
        ("Claim", "PGE-2028-001", "CMPA_ApplicationID", "CLM-CONTACT"),
        ("Claim", "PGE-2028-002", "SiteID", "CLM-CONTACT"),
        ("Claim", "PGE-2028-002", "CMPA_ApplicationID", "CLM-CONTACT"),
        ("Claim", "PGE-2028-001", "InstallationDate", "CLM-DATES"),
        ("Claim", "PGE-2028-002", "PaidDate", "CLM-DATES"),
        ("Claim", "PGE-2028-001", "HTR_Flag", "CLM-HTR"),
        ("Claim", "PGE-2028-002", "HTR_Flag", "CLM-HTR"),
        ("Claim", "PGE-2028-008", "HTR_Flag", "CLM-HTR"),
        ("Claim", "PGE-2028-007", "OriginalMeterInstallYear", "CLM-FUELSUB"),
        ("Participant", "PGE_Res", "ParticipantCount", "CLM-COUNT"),
        ("Site", "SW-S2", "SiteID", "CLM-ID"),
        ("ContactClaim", "PGE-2028-005", "ContactID", "CLM-ID"),
    }
    messages = {p.message for p in problems}
    assert "The claim meets the hard-to-reach criteria (DAC_Flag and ResLI_Flag), but HTR_Flag is not 1" in messages
    assert "Claim PGE-2028-001 has no ContactClaim record" in messages

    # a filing on a version listing them (the starting version, here) gets the same findings
    v = service.upload_claims(filer, deadline(), pge, claims_zip(t), "q1.zip")
    assert v.status == SubmissionVersion.Status.DRAFT
    filed = {(f["table"], f["record_id"].split(" / ")[0], f["field"], f["rule_id"]) for f in service.findings(v)}
    assert {f for f in filed if f[3] in {g[3] for g in got}} == got


def test_the_hard_to_reach_criteria_are_cedars_fifteen():
    assert len(service.HTR_PATHWAYS) == len(set(service.HTR_PATHWAYS)) == 15
    assert service.htr_pathway({"OMBCSA_Flag": "1", "LG_Flag": "true"}) == ("OMBCSA_Flag", "LG_Flag")
    assert service.htr_pathway({"Lang_Flag": "1", "ResLI_Flag": "1", "ResMFMH_Flag": "1"}) == ("Lang_Flag", "ResLI_Flag", "ResMFMH_Flag")
    assert service.htr_pathway({"Lang_Flag": "1", "ResLI_Flag": "1", "NRLease_Flag": "1"}) is None
    assert service.htr_pathway({"DAC_Flag": "1"}) is None


def test_the_date_window_counts_years_from_the_upload():
    found = []

    def add(table, i, rid, fld, rule, *rest, **kw):
        found.append((fld, rule))

    today = date(2028, 2, 29)
    for d, bad in (("2019-02-28", False), ("2019-02-27", True), ("2037-02-28", False), ("2037-03-01", True), ("02/27/2019", True)):
        found.clear()
        service._claim_rules(add, 2, {"ClaimID": "PGE-2028-1", "InstallationDate": d}, {}, Organisation(code="PGE", pa_type="IOU"), today)
        assert (("InstallationDate", "CLM-DATES") in found) is bad, d


def test_sw_keys_only_in_the_statewide_claims_filing(pge, programs):
    own = {
        "Claim": upload.Table("Claim", ["ClaimID"], [{"ClaimID": "SW-2028-1"}]),
        "Site": upload.Table("Site", ["SiteID"], [{"SiteID": "SW-S1"}]),
        "ContactClaim": upload.Table("ContactClaim", ["ClaimID", "ContactID"], [{"ClaimID": "SW-2028-1", "ContactID": "SW-C1"}]),
    }

    def ids(statewide):
        problems = service.derive_checks(own, own, pge, 2028, 1, statewide=statewide, lay=LAY)
        return {(p.table, p.field) for p in problems if p.rule_id == "CLM-ID"}

    assert ids(False) == {("Claim", "ClaimID"), ("Site", "SiteID"), ("ContactClaim", "ContactID")}
    assert ids(True) == set()
    pii = {"PII_Contact": [{"ContactID": "SW-C1"}], "PII_Site": [{"SiteID": "SW-S1", "ElectricServiceAccountID": "1"}]}
    known = {"PII_Contact": {"sw-c1"}, "PII_Site": {"sw-s1"}}
    assert {f.rule for f in vault.check(pii, "PGE", known)} == {"PII-ID"}
    assert {f.rule for f in vault.check(pii, "PGE", known, statewide=True)} == set()


def test_the_new_checks_are_choosable():
    checks = coded.by_id()
    for rid in ("CLM-DATES", "CLM-HTR", "CLM-CONTACT", "CLM-COUNT"):
        assert checks[rid].optional and checks[rid].severities == coded.CHOOSABLE
