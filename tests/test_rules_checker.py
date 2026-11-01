"""The pre-upload checker (BR-03.4, gap 23): the page, the API and the check_file command run the module's own rule-set step, so a file
gets the same findings there as on upload; and each rule set downloads as JSON with how each rule is checked."""

import io
import json
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client

from cedars.modules.claims import service as claims
from cedars.services.accounts import tokens
from cedars.services.rules import precheck
from cedars.services.rules import service as rules
from cedars.services.rules.models import RuleSet

from .test_claims import claim, claims_zip, deadline, filer, programs, q1, row, tables, year  # noqa: F401

pytestmark = pytest.mark.django_db
pytest.importorskip("recet.filing_rules", reason="the rule library runs on the pinned ReCET engine")
API = "/api/v1/rules"


@pytest.fixture
def claims_rules(cpuc_admin):
    return rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "claims"))


@pytest.fixture
def key(filer, pge):  # noqa: F811
    return tokens.create_token(filer, pge, name="claims system", scopes=["claims"], days=30)[1]


def bearer(key):
    return {"HTTP_AUTHORIZATION": f"Bearer {key}"}


def table_of(zip_bytes: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return z.read(f"{name}.csv")


def post(key, **data):
    name = data.pop("name", "Measure.csv")
    data["file"] = SimpleUploadedFile(name, data.pop("content"))
    return Client().post(f"{API}/check", data, **bearer(key))


def rule_set_part(found: list[dict], table: str, rs: RuleSet) -> list[tuple]:
    """The findings the rule-set step gives: the set's own rules, and the step's column and could-not-run findings."""
    ids = set(rs.rules.values_list("rule_id", flat=True)) | {"CLM-COL", "CLM-RULES", "BUD-COL", "BUD-RULES"}
    return sorted(
        (f["row"], f["record_id"], f["field"], f["rule_id"], f["severity"], f["message"])
        for f in found
        if f["table"] == table and f["rule_id"] in ids
    )


def api_part(body: dict) -> list[tuple]:
    return sorted((f["line"], f["record_id"], f["field"], f["rule_id"], f["severity"], f["message"]) for f in body["findings"])


def test_the_api_gives_a_claims_upload_s_own_rule_findings(filer, year, pge, programs, claims_rules, key):  # noqa: F811
    upload = q1(full=True)
    v = claims.upload_claims(filer, deadline(), pge, upload, "q1.zip")
    filed = claims.findings(v)
    for table in ("Measure", "ProgramCost"):
        r = post(key, module="claims", table=table, filing_year=2028, content=table_of(upload, table), name=f"{table}.csv")
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["rule_set"]["id"] == claims_rules.pk and body["rule_set"]["in_force"] and body["module"] == "claims"
        assert body["findings_count"] == len(body["findings"]) and body["records"] == 2
        assert api_part(body) == rule_set_part(filed, table, claims_rules)
        assert body["findings"] or table == "ProgramCost"
    # a Measure table without the specification's columns is refused on them, as on upload
    thin = claims.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    body = post(key, module="claims", table="Measure", filing_year=2028, content=table_of(q1(), "Measure")).json()
    assert {f["rule_id"] for f in body["findings"]} == {"CLM-COL"}
    assert api_part(body) == rule_set_part(claims.findings(thin), "Measure", claims_rules)
    assert [n for n in body["notes"] if "Measure rules were not run" in n]


def test_the_claim_year_defaults_to_the_one_year_of_the_records(filer, year, claims_rules, key):  # noqa: F811
    content = table_of(q1(full=True), "Measure")
    body = post(key, module="claims", table="Measure", content=content).json()
    assert any("Checked as 2028 claims" in n for n in body["notes"])
    assert api_part(body) == api_part(post(key, module="claims", table="Measure", filing_year=2028, content=content).json())


def test_csv_findings_and_the_check_file_command(claims_rules, key, tmp_path):
    content = table_of(q1(full=True), "Measure")
    r = post(key, module="claims", table="Measure", filing_year=2028, content=content, format="csv")
    assert r["Content-Type"].startswith("text/csv")
    assert r.content.decode().startswith("line,record_id,table,field,rule_id,severity,message,suggested_fix")
    f, out = tmp_path / "Measure.csv", tmp_path / "findings.csv"
    f.write_bytes(content)
    call_command("check_file", str(f), "--rules", "claims", "--filing-year", "2028", "--out", str(out), stdout=io.StringIO())
    assert out.read_text(encoding="utf-8") == r.content.decode()


def test_any_published_version_can_be_named(cpuc_admin, claims_rules, key):
    from datetime import date, timedelta

    later = rules.new_version(cpuc_admin, claims_rules, date.today() + timedelta(days=365), "Next year's rules")
    content = table_of(q1(full=True), "Measure")
    r = post(key, rule_set=later.pk, table="Measure", content=content)
    assert r.status_code == 400 and "draft" in r.json()["detail"]
    rules.publish(cpuc_admin, later)
    body = post(key, rule_set=later.pk, table="Measure", filing_year=2028, content=content).json()
    assert body["rule_set"]["id"] == later.pk and not body["rule_set"]["in_force"] and body["module"] == "claims"
    listed = Client().get(f"{API}/sets").json()["results"]  # public
    assert {s["id"] for s in listed} >= {claims_rules.pk, later.pk}
    assert [s for s in listed if s["id"] == claims_rules.pk][0]["in_force"]


def test_refusals(cpuc_admin, claims_rules, key, filer, pge):  # noqa: F811
    content = table_of(q1(full=True), "Measure")
    assert post(key, module="nonesuch", table="Measure", content=content).status_code == 400
    assert post(key, rule_set=999999, table="Measure", content=content).status_code == 400
    budget = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "budget_filing"))
    r = post(key, module="claims", rule_set=budget.pk, table="Measure", content=content)
    assert r.status_code == 400 and "not the claims rules" in r.json()["detail"]
    # a token reaches only its scopes: a claims token cannot check budget filings
    r = post(key, module="budgets", table="Measure", content=content)
    assert r.status_code == 403 and "budgets scope" in r.json()["detail"]
    # signed-in calls only
    anon = Client().post(f"{API}/check", {"module": "claims", "table": "Measure", "file": SimpleUploadedFile("m.csv", content)})
    assert anon.status_code in (401, 403)
    # a file that cannot be read is the caller's to fix
    r = post(key, module="claims", table="Measure", content=b"ClaimID|ClaimID\nA|B\n")
    assert r.status_code == 400 and "twice" in r.json()["detail"]


def test_the_page_checks_against_any_published_version(signed_in, pge_admin, claims_rules):
    c = signed_in(pge_admin)
    page = c.get("/rules/check/").content.decode()
    assert "(in force)" in page and "Other ways to check" in page and "/api/v1/rules/check" in page
    assert f"/rules/{claims_rules.pk}/rules.json" in page
    upload = SimpleUploadedFile("Measure.csv", table_of(q1(full=True), "Measure"))
    page = c.post("/rules/check/", {"rule_set": claims_rules.pk, "table": "Measure", "filing_year": "2028", "file": upload})
    assert "would be rejected" in page.content.decode() and "Quarterly claims, filing year 2028" in page.content.decode()


def test_the_rule_set_downloads_as_json_with_how_to_check_it(client, claims_rules):
    r = client.get(f"/rules/{claims_rules.pk}/rules.json")  # public, as the rule library is
    assert r["Content-Type"].startswith("application/json") and "attachment" in r["Content-Disposition"]
    d = json.loads(r.content)
    assert d["format"] == precheck.FORMAT and d["format_version"] == 1
    assert d["rule_set"]["code"] == "claims" and d["rule_set"]["module"] == "claims" and d["rule_set"]["in_force"]
    assert len(d["rules"]) == claims_rules.rules.count()
    assert {r["kind"] for r in d["rules"]} == {"rule", "spec"} and all(r["definition"] for r in d["rules"])
    # every kind of check the rules use is specified
    checks = {r["definition"]["check"] for r in d["rules"] if r["kind"] == "spec"}
    assert checks <= set(d["how_to_check"]["spec_checks"])
    tests = {r["definition"]["rule"] for r in d["rules"] if r["kind"] == "rule"}
    tests |= {c[2].lower() for r in d["rules"] if r["kind"] == "rule" for c in r["definition"]["conds"]}
    assert all(t in d["how_to_check"]["rule_tests"]["comparisons"] for t in tests), tests
    # the value lists the rules read are in the file, retired codes kept with their end year
    lists = {r["definition"]["list"] for r in d["rules"] if r["kind"] == "spec" and r["definition"]["check"] == "value_list"}
    assert (lists - {"PrgID", "AvoidedCostCombo"}) <= set(d["value_lists"]) and d["value_lists"]
    assert "PrgID" not in d["value_lists"]
    some = next(iter(d["value_lists"].values()))
    assert set(some[0]) == {"code", "description", "start_year", "end_year"}
    assert d["tables"]["Measure"]["columns"] == rules.spec_columns(claims_rules, "Measure")
    # the API serves the same file; drafts are not offered
    api = Client().get(f"{API}/sets/{claims_rules.pk}").json()
    assert api["rules"] == d["rules"] and api["how_to_check"] == d["how_to_check"]
    draft = rules.import_from_engine(claims_rules.created_by, "budget_filing")
    assert client.get(f"/rules/{draft.pk}/rules.json").status_code == 404
    assert Client().get(f"{API}/sets/{draft.pk}").status_code == 404
    assert "rules.json" in client.get(f"/rules/{claims_rules.pk}/").content.decode()


def test_the_checker_s_findings_parse_as_findings(claims_rules):
    """claims and budgets register their steps; an unknown table is refused before the file is read."""
    assert {c.module for c in precheck.checkers()} == {"claims", "budgets"}
    with pytest.raises(precheck.CheckError, match="not 'Claim'"):
        precheck.check("claims", "Claim", b"")
    content = claims_zip(tables(claim("PGE-2028-001", NumUnits="abc"), ProgramCost=[row("ProgramCost", PrgID="PGE_Res")]), full=True)
    result = precheck.check("claims", "Measure", table_of(content, "Measure"), filing_year=2028)
    numeric = [f for f in result.findings if f.field == "NumUnits" and f.value == "abc"]
    assert numeric and numeric[0].record_id == "PGE-2028-001" and numeric[0].fix.startswith("Enter a plain number")
