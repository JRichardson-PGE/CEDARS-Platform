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


def test_rule_pages_and_checker(client, signed_in, cpuc_admin, pge_admin, claims):
    service.publish(cpuc_admin, claims)
    signed_in(pge_admin)
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


def test_signed_out_visitors_do_not_see_the_rules(client, signed_in, cpuc_admin, pge_admin, claims):
    """Jake, 2026-11-02: the rule library is for signed-in people, so it is not in the signed-out header or footer, and its pages and
    the API's rule sets ask for a sign-in. The pre-upload checker always needed one."""
    service.publish(cpuc_admin, claims)
    rule_id = claims.rules.first().rule_id
    home = client.get("/").content.decode()
    assert 'href="/rules/"' not in home.split('<dialog id="page-help"', 1)[0]
    for path in (
        "/rules/",
        f"/rules/{claims.pk}/",
        f"/rules/{claims.pk}/{rule_id}/",
        "/rules/differences/",
        "/rules/check/",
        f"/rules/{claims.pk}/rules.json",
    ):
        r = client.get(path)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"], path
    assert client.get("/api/v1/rules/sets").status_code in (401, 403)
    signed_in(pge_admin)
    assert 'href="/rules/"' in client.get("/").content.decode()
    assert client.get("/rules/").status_code == 200


def test_drafts_are_shown_only_to_system_administrators(client, signed_in, cpuc_admin, pge_admin, claims):
    """A draft rule set is the administrators' work in progress: a signed-in PA sees only published versions."""
    service.publish(cpuc_admin, claims)
    draft = service.import_from_engine(cpuc_admin, "budget_filing")
    rule_id = draft.rules.first().rule_id
    signed_in(pge_admin)
    assert f"/rules/{draft.pk}/" not in client.get("/rules/").content.decode()
    assert client.get(f"/rules/{draft.pk}/").status_code == 404
    assert client.get(f"/rules/{draft.pk}/{rule_id}/").status_code == 404
    signed_in(cpuc_admin)
    assert f"/rules/{draft.pk}/" in client.get("/rules/").content.decode()
    assert client.get(f"/rules/{draft.pk}/{rule_id}/").status_code == 200


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
    assert any("the impact-profile library when the job is priced" in n for n in notes) and not any("not installed" in n for n in notes)
    assert service.canonical_text(service.canonical_text("E3TargetSector and E3TargetSector")) == (
        "AC_Elec_Target_Sector (E3TargetSector) and AC_Elec_Target_Sector"
    )


# --- administrators change rules as new versions


@pytest.fixture
def published_claims(claims, cpuc_admin):
    return service.publish(cpuc_admin, claims)


def test_a_new_version_copies_a_published_set(cpuc_admin, published_claims):
    from datetime import timedelta

    later = published_claims.effective_from + timedelta(days=90)
    with pytest.raises(service.RuleError, match="why"):
        service.new_version(cpuc_admin, published_claims, later, "")
    with pytest.raises(service.RuleError, match="after it"):
        service.new_version(cpuc_admin, published_claims, published_claims.effective_from, "Same day")
    new = service.new_version(cpuc_admin, published_claims, later, "D.26-01-001 changes")
    assert new.status == "draft" and new.based_on == published_claims and new.reason == "D.26-01-001 changes"
    assert set(new.rules.values_list("rule_id", flat=True)) == set(published_claims.rules.values_list("rule_id", flat=True))
    assert service.in_force("claims") == published_claims
    d = service.differences(published_claims, new)
    assert not d["only_a"] and not d["only_b"] and not d["changed"]


def test_adding_changing_and_removing_rules_in_a_draft(cpuc_admin, published_claims):
    from datetime import timedelta

    from cedars.services.audit.models import AuditEvent

    new = service.new_version(cpuc_admin, published_claims, published_claims.effective_from + timedelta(days=1), "Test")
    definition = service.rule_definition("NumUnits", "is greater than", "0", [("MeasAppType", "equals", "'AR'")])
    r = service.add_rule(cpuc_admin, new, kind="rule", severity="reject", table="Measure", field="NumUnits", definition=definition)
    assert r.rule_id.startswith("V") and r.message.startswith("When MeasAppType equals 'AR', NumUnits should be greater than 0")
    assert r.fix and r.modules == ["claims", "calculations"] and r.source == "Added by an administrator"
    with pytest.raises(service.RuleError, match="already has that rule"):
        service.add_rule(cpuc_admin, new, kind="rule", severity="reject", table="Measure", field="NumUnits", definition=definition)
    with pytest.raises(service.RuleError, match="not a test"):
        service.add_rule(
            cpuc_admin, new, kind="rule", severity="reject", table="Measure", field="NumUnits",
            definition=service.rule_definition("NumUnits", "is roughly", "1"),
        )  # fmt: skip
    with pytest.raises(service.RuleError, match="Give the value"):
        service.add_rule(
            cpuc_admin, new, kind="rule", severity="reject", table="Measure", field="NumUnits",
            definition=service.rule_definition("NumUnits", "equals", ""),
        )  # fmt: skip
    spec = service.add_rule(
        cpuc_admin, new, kind="spec", severity="reject", table="Measure", field="MeasDescription",
        definition=service.check_definition("max_length", n=80),
    )  # fmt: skip
    assert spec.rule_id.startswith("S") and spec.message == "MeasDescription should be at most 80 characters"
    with pytest.raises(service.RuleError, match="value list"):
        service.add_rule(
            cpuc_admin, new, kind="spec", severity="reject", table="Measure", field="Sector",
            definition=service.check_definition("value_list", list="no_such_list"),
        )  # fmt: skip

    # a changed test is a different rule: a new ID that records the one it replaces
    old = new.rules.filter(kind="rule", severity="warning").first()
    old_id = old.rule_id
    changed = service.edit_rule(cpuc_admin, old, severity="reject", reason="Now a reject")
    assert changed.rule_id != old_id and changed.rule_id.startswith("V") and changed.replaces == old_id
    d = service.differences(published_claims, new)
    assert [(a.rule_id, b.rule_id) for a, b in d["changed"]] == [(old_id, changed.rule_id)]
    assert {x.rule_id for x in d["only_b"]} == {r.rule_id, spec.rule_id} and not d["only_a"]

    # wording only keeps the ID
    same = service.edit_rule(cpuc_admin, spec, fix="Keep the description short.")
    assert same.rule_id == spec.rule_id and same.fix == "Keep the description short."

    gone = new.rules.exclude(pk__in=[r.pk, spec.pk, changed.pk]).first()
    service.remove_rule(cpuc_admin, gone, reason="Retired by D.26-01-001")
    assert service.differences(published_claims, new)["only_a"] == [published_claims.rules.get(rule_id=gone.rule_id)]
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"rule_set.version_started", "rule.added", "rule.edited", "rule.removed"} <= actions

    # the new rule runs in the checker once published
    service.publish(cpuc_admin, new)
    content = _measure_csv([{"CEInputID": "M1", "ClaimYearQuarter": "2028Q1", "MeasAppType": "AR", "NumUnits": "0"}])
    findings, _ = service.check(new, "Measure", service.read_table(content))
    assert any(f.rule_id == r.rule_id for f in findings)


def test_published_rule_sets_never_change_in_the_database(published_claims):
    from django.db import DatabaseError, transaction

    from cedars.services.rules.models import Rule

    rule = published_claims.rules.first()
    for statement in (
        lambda: Rule.objects.filter(pk=rule.pk).update(message="changed"),
        lambda: Rule.objects.filter(pk=rule.pk).delete(),
        lambda: Rule.objects.create(rule_set=published_claims, rule_id="S000000", severity="reject", table="Measure", field="X"),
        lambda: RuleSet.objects.filter(pk=published_claims.pk).update(label="changed"),
        lambda: RuleSet.objects.filter(pk=published_claims.pk).delete(),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            statement()


def test_published_sets_refuse_rule_changes(cpuc_admin, published_claims):
    rule = published_claims.rules.first()
    with pytest.raises(AccessDenied, match="never changes"):
        service.edit_rule(cpuc_admin, rule, severity="warning")
    with pytest.raises(AccessDenied, match="never changes"):
        service.remove_rule(cpuc_admin, rule)
    with pytest.raises(AccessDenied, match="never changes"):
        service.discard_draft(cpuc_admin, published_claims)


def test_discarding_a_draft_set(cpuc_admin, claims):
    service.discard_draft(cpuc_admin, claims, "Wrong file")
    assert not RuleSet.objects.filter(pk=claims.pk).exists()


def test_every_imported_rule_can_be_re_saved(cpuc_admin, claims):
    """The checks on a changed test accept every rule CEDARS' own files have, so changing one rule's severity never trips on its
    spelling of the test."""
    budget = service.import_from_engine(cpuc_admin, "budget_filing")
    for r in [*claims.rules.all(), *budget.rules.all()]:
        service.validate(r.kind, r.severity, r.table, r.field, r.definition)


def test_rule_admin_pages(client, signed_in, cpuc_admin, pge_admin, published_claims):
    assert 'id="version-h"' not in client.get(f"/rules/{published_claims.pk}/").content.decode()
    assert signed_in(pge_admin).get(f"/rules/{published_claims.pk}/add/").status_code == 403
    c = signed_in(cpuc_admin)
    page = c.get(f"/rules/{published_claims.pk}/").content.decode()
    # the claims rules change with a version of the claims specification, started from its page, not on their own date
    assert 'id="version-h"' in page and "/specifications/claims/current/" in page
    c.post(f"/rules/{published_claims.pk}/new-version", {"effective_from": "2027-01-01", "reason": "D.26-01-001"})
    assert not RuleSet.objects.filter(code="claims", status="draft").exists()
    # a draft made in the library itself (not a specification's) is still changed and published here
    new = service.new_version(cpuc_admin, published_claims, date(2027, 1, 1), "D.26-01-001")
    page = c.get(f"/rules/{new.pk}/").content.decode()
    assert 'id="draft-h"' in page and "What this version changes" in page
    assert "Add a rule" in c.get(f"/rules/{new.pk}/add/").content.decode()

    # a test on a record, with a condition
    form = {
        "kind": "rule", "severity": "warning", "table": "Measure", "field": "NumUnits", "rule": "is greater than", "value": "0",
        "connector": "all", "cond1_field": "MeasAppType", "cond1_rule": "in", "cond1_value": "AR, ER", "modules": ["claims"],
        "reason": "New check",
    }  # fmt: skip
    r = c.post(f"/rules/{new.pk}/add/", form)
    added = new.rules.get(source="Added by an administrator")
    assert r.status_code == 302 and added.definition["conds"][0] == ["Measure", "MeasAppType", "in", "['AR', 'ER']"]
    assert added.message.startswith("When MeasAppType in ['AR', 'ER'], NumUnits should be greater than 0")
    # a rule the checker cannot run is refused with the reason, on the form
    bad = c.post(f"/rules/{new.pk}/add/", form | {"rule": "is less than", "value": "'abc'"}).content.decode()
    assert "The checker cannot run this rule" in bad and new.rules.filter(source="Added by an administrator").count() == 1

    # a field constraint changed to a warning: a new ID that replaces the old one
    spec = new.rules.filter(kind="spec", definition__check="numeric").first()
    page = c.get(f"/rules/{new.pk}/{spec.rule_id}/edit/").content.decode()
    assert "Save the change" in page
    from cedars.services.rules.forms import initial_for

    data = {k: v for k, v in initial_for(spec).items() if v not in (None, False)}
    r = c.post(f"/rules/{new.pk}/{spec.rule_id}/edit/", data | {"fix": "Only digits.", "reason": "Wording"})
    spec.refresh_from_db()
    assert r.status_code == 302 and spec.fix == "Only digits." and not spec.replaces  # wording only: same ID
    old_id = spec.rule_id
    c.post(f"/rules/{new.pk}/{spec.rule_id}/edit/", data | {"check": "max_length", "n": "12", "message": "", "reason": "Shorter"})
    spec.refresh_from_db()
    assert spec.rule_id != old_id and spec.replaces == old_id and spec.message.endswith("should be at most 12 characters")
    assert "replaces rule" in c.get(f"/rules/{new.pk}/{spec.rule_id}/").content.decode()
    page = client.get(f"/rules/differences/?a={published_claims.pk}&b={new.pk}").content.decode()
    assert "Changed in" in page and old_id in page and spec.rule_id in page

    gone = new.rules.exclude(pk__in=[spec.pk, added.pk]).first()
    c.post(f"/rules/{new.pk}/{gone.rule_id}/remove", {"reason": "Retired"})
    assert not new.rules.filter(pk=gone.pk).exists()
    c.post(f"/rules/{new.pk}/publish")
    new.refresh_from_db()
    assert new.status == "published"
    assert c.get(f"/rules/{new.pk}/add/").status_code == 302


def test_add_a_rule_asks_the_kind_first_and_picks_fields(signed_in, cpuc_admin, published_claims):
    import json
    from pathlib import Path

    from cedars.services.dictionary import service as dictionary

    catalog = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.94.json"
    dictionary.load_catalog(None, json.loads(catalog.read_text(encoding="utf-8")))
    draft = service.new_version(cpuc_admin, published_claims, date(2027, 1, 1), "D.26-01-001")
    c = signed_in(cpuc_admin)
    page = c.get(f"/rules/{draft.pk}/add/").content.decode()
    # the kind comes first and decides which part shows (core/pickers.js); fields are picked from the dictionary's input fields
    assert page.index('name="kind"') < page.index('name="table"') < page.index('name="field"') < page.index('data-reveal-for="rule"')
    assert 'name="kind" value="rule" data-reveal=""' in page.replace(' required id="id_kind_0"', "").replace(' id="id_kind_0"', "")
    assert 'data-reveal-for="spec"' in page and page.index('data-reveal-for="spec"') < page.index('name="severity"')
    assert 'list="id_field-list"' in page and '<option value="NumUnits">' in page and 'list="id_cond1_field-list"' in page
    assert '<details class="other-filters more-conditions">' in page and 'name="cond2_field"' in page.split("more-conditions", 1)[1]

    form = {
        "kind": "rule", "severity": "warning", "table": "Measure", "field": "NumUnit", "rule": "is greater than", "value": "0",
        "connector": "all", "modules": ["claims"], "reason": "New check",
    }  # fmt: skip
    # a field that names nothing gets the near matches, on the form
    bad = c.post(f"/rules/{draft.pk}/add/", form).content.decode()
    assert "No field is “NumUnit”. Did you mean NumUnits" in bad.replace("&ldquo;", "“").replace("&rdquo;", "”")
    # any name the dictionary lists is kept as given (CET's E3 name here), so the rule's test is what was typed
    used = {"field": "NumUnits", "cond1_field": "E3MeaElecEndUseShape", "cond1_rule": "is not null", "n": "x"}  # n: the other kind's part
    r = c.post(f"/rules/{draft.pk}/add/", form | used)
    assert r.status_code == 302, r.context["form"].errors
    added = draft.rules.get(source="Added by an administrator")
    assert added.field == "NumUnits" and added.definition["conds"][0][1] == "E3MeaElecEndUseShape"
    # a filled-in folded condition opens the fold when the form comes back
    again = c.post(f"/rules/{draft.pk}/add/", form | {"cond3_field": "NumUnits", "cond3_rule": "", "cond3_value": "1"}).content.decode()
    assert '<details class="other-filters more-conditions" open>' in again and "1 in use" in again

    # the part for the other kind is not read: what is left in it never stops the save
    spec = {
        "kind": "spec", "severity": "reject", "table": "Measure", "field": "NumUnits", "check": "max_length", "n": "17",
        "cond1_field": "Nonsense", "rule": "nonsense", "modules": ["claims"], "reason": "Constraint",
    }  # fmt: skip
    r = c.post(f"/rules/{draft.pk}/add/", spec)
    assert r.status_code == 302, r.context["form"].errors
    assert draft.rules.filter(source="Added by an administrator", kind="spec", field="NumUnits").exists()


def test_the_rule_as_data_is_folded(signed_in, cpuc_admin, published_claims):
    rule = published_claims.rules.first()
    page = signed_in(cpuc_admin).get(f"/rules/{published_claims.pk}/{rule.rule_id}/").content.decode()
    folded = page.split('<details class="section" id="as-data">', 1)[1].split("</details>", 1)[0]
    assert '<h2 id="data-h">The rule as data</h2>' in folded and "<pre" in folded
