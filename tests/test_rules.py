from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from cedars.services.accounts.service import AccessDenied
from cedars.services.rules import service
from cedars.services.rules.models import RuleSet

pytestmark = pytest.mark.django_db
recet = pytest.importorskip("recet", reason="needs the pinned ReCET engine (engine/README.md)")


@pytest.fixture
def claims(cpuc_admin):
    return service.import_from_engine(cpuc_admin, "claims")


def _measure_csv(rows: list[dict]) -> bytes:
    from recet import templates

    cols = templates.MEASURE_COLUMNS
    lines = ["|".join(cols)] + ["|".join(str(r.get(c, "")) for c in cols) for r in rows]
    return ("\n".join(lines) + "\n").encode()


def test_import_publish_and_in_force(claims, cpuc_admin):
    assert claims.status == "draft" and claims.rules.count() > 150
    assert {r for r in claims.rules.values_list("severity", flat=True)} == {"reject", "warning"}
    assert all(claims.rules.values_list("fix", flat=True))
    assert service.in_force("claims") is None
    service.publish(cpuc_admin, claims)
    assert service.in_force("claims") == claims
    assert service.in_force("claims", date(2000, 1, 1)) is None
    with pytest.raises(AccessDenied, match="already in the library"):
        service.import_from_engine(cpuc_admin, "claims")


def test_published_rules_never_change(claims, cpuc_admin):
    rule = claims.rules.first()
    service.edit_rule(cpuc_admin, rule, fix="Clearer fix.", difference="Claims only: budgets have no ClaimYearQuarter.")
    service.publish(cpuc_admin, claims)
    with pytest.raises(AccessDenied, match="never changes"):
        service.edit_rule(cpuc_admin, rule, fix="Again")


def test_only_cpuc_admins_manage_rules(pge_admin):
    with pytest.raises(AccessDenied):
        service.import_from_engine(pge_admin, "claims")


def test_checking_a_file_gives_findings_with_fixes(claims):
    content = _measure_csv([{"CEInputID": "M1", "PrgID": "PGE_X", "ClaimYearQuarter": "2028Q1", "NumUnits": "abc"}])
    df = service.read_table(content)
    findings, notes = service.check(claims, "Measure", df)
    assert findings and all(f.record_id == "M1" and f.row == 2 for f in findings)
    assert findings[0].severity == "reject"  # rejects first
    numeric = [f for f in findings if f.field == "NumUnits" and "number" in f.message]
    assert numeric and numeric[0].fix.startswith("Enter a plain number") and numeric[0].value == "abc"
    assert service.findings_csv(findings).startswith("line,record_id,table,field,rule_id,severity,message,suggested_fix")


def test_cet_names_are_accepted_in_a_checked_file(claims):
    content = _measure_csv([{"CEInputID": "M1"}]).replace(b"AC_Climate_Zone", b"E3ClimateZone")
    df = service.read_table(content)
    assert "AC_Climate_Zone" in df.columns


def test_differences_between_sets(cpuc_admin, claims):
    budget = service.import_from_engine(cpuc_admin, "budget_filing")
    d = service.differences(claims, budget)
    assert d["only_a"] and d["only_b"]
    shared = set(claims.rules.values_list("rule_id", flat=True)) & set(budget.rules.values_list("rule_id", flat=True))
    assert shared  # a rule in both keeps its ID


def test_rule_pages_and_checker(client, signed_in, cpuc_admin, claims):
    service.publish(cpuc_admin, claims)
    assert "in force" in client.get("/rules/").content.decode()
    page = client.get(f"/rules/{claims.pk}/?severity=reject").content.decode()
    assert "Rejects the record" in page
    assert client.get(f"/rules/{claims.pk}/?format=csv").content.decode().startswith("rule_id,severity")
    c = signed_in(cpuc_admin)
    upload = SimpleUploadedFile("m.csv", _measure_csv([{"CEInputID": "M1", "NumUnits": "abc"}]))
    page = c.post("/rules/check/", {"rule_set": claims.pk, "table": "Measure", "file": upload}).content.decode()
    assert "would be rejected" in page and "Enter a plain number" in page
    upload = SimpleUploadedFile("m.csv", _measure_csv([{"CEInputID": "M1", "NumUnits": "abc"}]))
    r = c.post("/rules/check/", {"rule_set": claims.pk, "table": "Measure", "file": upload, "download": "1"})
    assert r["Content-Type"].startswith("text/csv") and b"M1" in r.content


def test_import_page_for_admins(signed_in, cpuc_admin):
    signed_in(cpuc_admin).post("/rules/import", {"code": "budget_filing"})
    assert RuleSet.objects.get(code="budget_filing").status == "draft"


def test_findings_use_the_data_dictionarys_field_names(claims, monkeypatch):
    """F14: the rule files name CET's fields (E3ClimateZone); findings and rule wording use the dictionary's (AC_Climate_Zone), with
    the CET name in brackets once. F15: the profile-combination rules say where the library is missing on this server."""
    assert not any(r.message.count("E3ClimateZone") > r.message.count("(E3ClimateZone)") for r in claims.rules.all())
    assert claims.rules.filter(message__contains="AC_Climate_Zone (E3ClimateZone)").exists()
    monkeypatch.setattr(service, "library_available", lambda: False)
    content = _measure_csv([{"CEInputID": "M1", "PrgID": "PGE_X", "ClaimYearQuarter": "2028Q1", "AC_Climate_Zone": "99"}])
    findings, notes = service.check(claims, "Measure", service.read_table(content))
    assert not any(f.field.startswith("E3") for f in findings)
    zone = [f for f in findings if f.field == "AC_Climate_Zone"]
    assert zone and "AC_Climate_Zone (E3ClimateZone)" in zone[0].message and "E3ClimateZone should" not in zone[0].message
    assert any("reference folder on this server" in n for n in notes) and not any("not installed" in n for n in notes)
    assert service.canonical_text(service.canonical_text("E3TargetSector and E3TargetSector")) == (
        "AC_Elec_Target_Sector (E3TargetSector) and AC_Elec_Target_Sector"
    )
