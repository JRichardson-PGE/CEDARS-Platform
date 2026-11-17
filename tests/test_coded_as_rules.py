# ruff: noqa: F811 - fixtures imported from other test files
"""Claims built-in checks written as rules (rules.coded AsRule, claims coded_checks.AS_RULES) give the check's findings, record for
record and field for field, on records that take every branch of the check's code; a draft's editor replaces a listed check with its
rules (specifications.editor.write_as_rules), and a filing on the new version gets the same findings from the rules."""

import itertools

import pytest

from cedars.modules.claims import service as claims
from cedars.modules.claims.coded_checks import AS_RULES
from cedars.services.accounts.models import Organisation
from cedars.services.rules import coded

from .test_claims import claim, claims_zip, deadline, filer, programs, row, tables, year  # noqa: F401 - fixtures
from .test_rules_wider import draft, rules  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

IMPACTS = ["Cust-SEM", "Cust-SEM-FuelSub", "Deemed", "Cust-NMEC", "Deemed-FuelSub", "", None]  # None: the claim has no measure
VALUES = {
    "ParentClaimID": ["", "PGE-2028-900"],
    "NMEC_TU_Flag": ["", "1", "0", "yes", "x"],
    "ParticipantID": ["", "P1"],
    "SEM_Phase": ["", "2"],
    "ClaimType": ["Deemed", "deemed ", "Custom", ""],
    "MeasDetailID": ["", "M1"],
    "ProjectID": ["", "J1"],
    "MeasImpactCalcType": ["", "Calc"],
    "TotalCustomerInfrastructureCosts": ["", "0", "120"],
    "PartialPaymentPercent": ["", "0", "0.5", "1", "1.2", "-0.1", "abc"],
    "PartialPaymentFinal_Flag": ["", "1", "0"],
    "CCAREN_Flag": ["", "1", "0", "true", "2"],
}


def corpus():
    """Claims and their measures that take every branch: each impact type with each value of each field, the others cycling."""
    claims_rows, measures = [], []
    n = 0
    for impact in IMPACTS:
        for field, values in VALUES.items():
            for value in values:
                n += 1
                cid = f"PGE-2028-{n:04d}"
                r = {f: vs[(n + k) % len(vs)] for k, (f, vs) in enumerate(VALUES.items())}
                r.update({"ClaimID": cid, field: value})
                claims_rows.append(r)
                if impact is not None:
                    measures.append({"ClaimID": cid.lower() if n % 3 == 0 else cid, "MeasImpactType": impact})
    return claims_rows, measures


def coded_findings(check: str, claim_rows, measure_rows, pa_type: str) -> set[tuple[int, str]]:
    org = Organisation(code="MCE", name="x", pa_type=pa_type)
    found = []

    def add(table, i, rid, fld, rule, message, fix, value="", severity="reject"):
        found.append((i, fld, rule))

    by_claim = {claims._claim(m): m for m in measure_rows}
    for i, r in enumerate(claim_rows, start=2):
        claims._claim_rules(add, i, {k: v.strip() for k, v in r.items()}, by_claim.get(claims._claim(r), {}), org)
    return {(i, f) for i, f, rule in found if rule == check}


def rule_findings(rules, rs, ids, claim_rows, measure_rows, pa_type: str) -> set[tuple[int, str]]:
    import pandas as pd

    cols = sorted({k for r in claim_rows for k in r})
    df = pd.DataFrame([{c: r.get(c, "") for c in cols} for r in claim_rows]).astype(str)
    df = df.apply(lambda s: s.str.strip())
    ctx = rules.CheckContext(
        tables={"Measure": pd.DataFrame(measure_rows).astype(str)},
        keys={"Claim": ["ClaimID"], "Measure": ["ClaimID"]},
        filer={"PA": "MCE", "PAType": pa_type},
    )
    found, _notes = rules.check(rs, "Claim", df.replace("", pd.NA).fillna(""), filing_year=2028, id_column="ClaimID", context=ctx)
    return {(f.row, f.field) for f in found if f.rule_id in ids}


def add_as_rules(rules, admin, rs, check: str) -> set[str]:
    ids = set()
    for ar in AS_RULES[check]:
        d = rules.rule_definition(ar.field, ar.rule, ar.value, [(*c, "") for c in ar.conds], ar.connector, ar.table)
        ids.add(rules.add_rule(admin, rs, kind="rule", severity="reject", table=ar.table, field=ar.field, definition=d, reason="t").rule_id)
    return ids


@pytest.mark.parametrize("check", sorted(AS_RULES))
def test_the_rules_give_the_checks_findings(rules, draft, cpuc_admin, check):
    rs, _version = draft
    ids = add_as_rules(rules, cpuc_admin, rs, check)
    claim_rows, measure_rows = corpus()
    for pa_type in ("IOU", "CCA", "REN"):
        want = coded_findings(check, claim_rows, measure_rows, pa_type)
        got = rule_findings(rules, rs, ids, claim_rows, measure_rows, pa_type)
        assert got == want, (check, pa_type, sorted(got - want)[:5], sorted(want - got)[:5])
        assert want or (check == "CLM-CCAREN" and pa_type == "IOU")  # every check finds something in the corpus


def test_only_choosable_claims_checks_are_written_as_rules():
    checks = coded.by_id()
    assert set(AS_RULES) == {c.rule_id for c in checks.values() if c.as_rules}
    assert all(checks[k].optional for k in AS_RULES) and "CLM-SITE" not in AS_RULES
    assert all(isinstance(r, coded.AsRule) for r in itertools.chain.from_iterable(AS_RULES.values()))


@pytest.fixture
def published(rules, cpuc_admin):  # noqa: F811
    """A published claims version naming a published claims rule set (CEDARS' own rules), with the built-in checks it lists."""
    from .test_specification_filings import version_of

    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "claims"))
    return version_of("claims", "Claims with built-in checks", rule_set=rs)


def test_a_draft_writes_a_check_as_rules_and_a_filing_gets_its_findings_from_them(
    rules,
    published,
    cpuc_admin,
    filer,
    year,
    pge,
    programs,
    client,  # noqa: F811
):
    from cedars.services.specifications import editor
    from cedars.services.submissions.models import Deadline

    from .conftest import sign_in

    assert published.checks.filter(rule_id="CLM-SEM").exists()
    Deadline.objects.filter(pk=deadline().pk).update(specification_version=published)
    data = claims_zip(
        full=True,
        t=tables(
            claim("PGE-2028-001", MeasImpactType="Cust-SEM"),
            claim("PGE-2028-002", MeasImpactType="Deemed"),
            ProgramCost=[row("ProgramCost", PrgID="PGE_Res")],
            Site=[row("Site", SiteID="PGE-S1")],
        ),
    )
    v = claims.upload_claims(filer, deadline(), pge, data, "q1.zip")
    coded_sem = {(f["row"], f["field"]) for f in claims.findings(v) if f["rule_id"] == "CLM-SEM"}
    assert coded_sem  # claim 1 is SEM and lacks its ParticipantID and SEM_Phase

    draft = editor.start_draft(cpuc_admin, published, "Claims with SEM as rules", "why")
    assert "CLM-SEM" in {c.rule_id for c in editor.writable_as_rules(draft)}
    sign_in(client, cpuc_admin)
    page = client.get(f"/specifications/claims/{draft.slug}/edit/checks/").content.decode()
    assert 'id="as-rules-form"' in page and 'value="CLM-SEM"' in page
    with pytest.raises(editor.EditError, match="Say why"):
        editor.write_as_rules(cpuc_admin, draft, "CLM-SEM", "")
    r = client.post(
        f"/specifications/claims/{draft.slug}/edit/checks/as-rules/", {"check": "CLM-SEM", "reason": "the CPUC keeps it as data"}
    )
    assert r.status_code == 302
    draft.refresh_from_db()
    assert not draft.checks.filter(rule_id="CLM-SEM").exists()
    written = draft.rule_set.rules.filter(table="Claim", fix=coded.by_id()["CLM-SEM"].fix)
    assert written.count() == len(AS_RULES["CLM-SEM"]) and {w.severity for w in written} == {"reject"}
    with pytest.raises(editor.EditError, match="does not run CLM-SEM"):
        editor.write_as_rules(cpuc_admin, draft, "CLM-SEM", "again")
    with pytest.raises(editor.EditError, match="cannot be written as rules"):
        editor.write_as_rules(cpuc_admin, draft, "CLM-SITE", "why")

    # the filing's records checked under the draft: no CLM-SEM, and the rules find the same records and fields
    found = claims.impact_findings(draft, v, claims.impact_rows(v))
    ids = set(written.values_list("rule_id", flat=True))
    assert not [f for f in found if f.rule_id == "CLM-SEM"]
    assert {(f.row, f.field) for f in found if f.rule_id in ids} == coded_sem
