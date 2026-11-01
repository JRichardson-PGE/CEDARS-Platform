"""Retiring dictionary fields and the per-version change log (gap 33, BR-08.4, BR-08.2): a field is retired after its usage, audience
and regulatory review, in a new catalog version, never dropped; it leaves the templates from its date; a later load keeps the
retirement; every version's changes are listed field by field on public pages and as CSV."""

import copy
import csv
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary import changes, service
from cedars.services.dictionary.models import CatalogVersion, Field
from cedars.services.releases import mandates

from .conftest import page_only

pytestmark = pytest.mark.django_db
CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.89.json"
REVIEWS = {
    "reason": "No PA files it and no report reads it.",
    "usage": "Blank on every record filed 2024-2026.",
    "audience": "No dashboard or download shows it.",
    "regulatory": "The SPM does not require it; no decision names it.",
}


@pytest.fixture
def payload():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


@pytest.fixture
def catalog(payload):
    return service.load_catalog(None, payload)


def today():
    return timezone.localdate()


def retire(user, name="MeasDescription", days=0, **kw):
    return changes.retire(user, name, retired_from=today() + timedelta(days=days), **{**REVIEWS, **kw})


def test_retiring_needs_a_system_administrator_and_all_three_reviews(catalog, cpuc_admin, pge_admin):
    with pytest.raises(AccessDenied):
        retire(pge_admin)
    with pytest.raises(changes.RetirementError, match="usage review, the regulatory review"):
        retire(cpuc_admin, usage=" ", regulatory="")
    with pytest.raises(changes.RetirementError, match="today or later"):
        retire(cpuc_admin, days=-1)
    with pytest.raises(changes.RetirementError, match="no field"):
        retire(cpuc_admin, name="NotAField")
    assert CatalogVersion.objects.count() == 1


def test_a_retirement_is_a_new_version_that_keeps_the_field(catalog, cpuc_admin):
    version, f = retire(cpuc_admin, days=30)
    assert service.current() == version and version.based_on == catalog and version.label == "ReCET 0.1.89 rev 2"
    assert version.fields.count() == catalog.fields.count()
    assert f.state == Field.State.RETIRED and f.review_regulatory.startswith("The SPM") and f.aliases.count() == 0
    old = catalog.fields.get(name="MeasDescription")
    assert old.state == Field.State.ACTIVE  # the earlier version is unchanged
    # the copy keeps every field's other names and uses
    zone = version.fields.get(name="AC_Climate_Zone")
    assert {a.name for a in zone.aliases.all()} == {a.name for a in catalog.fields.get(name="AC_Climate_Zone").aliases.all()}
    assert zone.usages.count() == catalog.fields.get(name="AC_Climate_Zone").usages.count()
    with pytest.raises(changes.RetirementError, match="already retired"):
        retire(cpuc_admin)
    e = AuditEvent.objects.get(action="dictionary.field_retired")
    assert e.reason == REVIEWS["reason"] and e.after["state"] == "retired" and e.after["version"] == version.label
    v3, f3 = changes.reinstate(cpuc_admin, "MeasDescription", reason="The 2029 claims need it again.")
    assert v3.label == "ReCET 0.1.89 rev 3" and f3.state == Field.State.ACTIVE and f3.retired_from is None
    assert AuditEvent.objects.filter(action="dictionary.field_reinstated").exists()


def test_templates_leave_a_retired_field_out_from_its_date(catalog, cpuc_admin):
    assert "MeasDescription" in service.template_csv("calculations", "Measure")
    retire(cpuc_admin, days=10)
    assert "MeasDescription" in service.template_csv("calculations", "Measure")  # not yet
    later = today() + timedelta(days=10)
    names = [f.name for f, _ in service.table_fields("calculations", "Measure", on=later)]
    assert "MeasDescription" not in names and "CEInputID" in names
    assert "MeasDescription" in [f.name for f, _ in service.table_fields("calculations", "Measure", on=later, retired=True)]


def test_an_upload_still_reads_a_retired_column(catalog, cpuc_admin):
    retire(cpuc_admin, name="NumUnits")
    headers = [f.name for f, _ in service.table_fields("calculations", "Measure", retired=True)]
    m = service.map_headers(headers, "calculations", "Measure")
    assert m.retired == ["NumUnits"] and "NumUnits" not in m.missing and "NumUnits" not in m.columns
    assert "NumUnits" not in service.json_schema("calculations", "Measure")["required"]
    assert service.resolve("NumUnits").state == Field.State.RETIRED  # the name still resolves


def test_a_later_load_keeps_the_retirement(catalog, cpuc_admin, payload):
    retire(cpuc_admin, name="MeasDescription")
    retire(cpuc_admin, name="Sector")
    later = copy.deepcopy(payload)
    later["source"] = "ReCET 0.1.88"
    later["fields"] = [f for f in later["fields"] if f["name"] != "Sector"]  # the export leaves a retired field out
    v = service.load_catalog(None, later)
    assert v.fields.get(name="MeasDescription").state == Field.State.RETIRED
    sector = v.fields.get(name="Sector")
    assert sector.state == Field.State.RETIRED and sector.usages.exists()
    assert AuditEvent.objects.filter(action="dictionary.published", object_id=str(v.pk)).get().after["retired_kept"] == ["Sector"]


def test_a_field_the_register_requires_is_not_retired(catalog, cpuc_admin):
    m = mandates.create_draft(
        cpuc_admin,
        reference="D.99-01-001",
        title="Tax credits in the TRC",
        requirement="Count tax credits as a TRC benefit.",
        effective_on=today(),
    )
    mandates.add_link(cpuc_admin, m, "field", "MeasDescription")
    retire(cpuc_admin, days=1)  # a draft entry does not hold it
    changes.reinstate(cpuc_admin, "MeasDescription", reason="Back")
    mandates.publish(cpuc_admin, m)
    with pytest.raises(changes.RetirementError, match="D.99-01-001"):
        retire(cpuc_admin)
    mandates.update(cpuc_admin, m, status="superseded", status_note="Replaced by D.99-02-002.", reason="Superseded")
    retire(cpuc_admin)


def test_the_change_log_lists_each_versions_changes(catalog, cpuc_admin, payload):
    first = changes.changes(catalog)
    assert first.previous is None and first.counts["added"] == catalog.fields.count()
    v2, _ = retire(cpuc_admin, days=5)
    ch = changes.changes(v2)
    assert ch.previous == catalog and ch.counts == {"added": 0, "retired": 1, "reinstated": 0, "changed": 0, "dropped": 0}
    c = ch.of("retired")[0]
    assert c.name == "MeasDescription" and c.note == REVIEWS["reason"] and c.details[0][0] == "Retired from"
    later = copy.deepcopy(payload)
    later["source"] = "ReCET 0.1.88"
    elec = next(f for f in later["fields"] if f["name"] == "ElecBen")
    elec["unit"] = "Dollars (2026)"
    elec["aliases"] = elec.get("aliases", []) + [{"name": "ElecBenefits", "system": "CET"}]
    later["fields"] = [f for f in later["fields"] if f["name"] != "PrgID"] + [
        {"name": "NewOne", "kind": "output", "definition": "A new output.", "usage": []}
    ]
    v3 = service.load_catalog(None, later)
    ch = changes.changes(v3)
    assert [c.name for c in ch.of("added")] == ["NewOne"] and [c.name for c in ch.of("dropped")] == ["PrgID"]
    changed = {c.name: c.details for c in ch.of("changed")}
    assert ("Unit", catalog.fields.get(name="ElecBen").unit, "Dollars (2026)") in changed["ElecBen"]
    assert ("Other names", "", "ElecBenefits (CET)") in changed["ElecBen"]
    assert not ch.of("retired")  # MeasDescription is carried, not retired again
    hist = changes.history("MeasDescription")
    assert [v.label for v, _ in hist] == [v2.label, catalog.label]
    rows = list(csv.DictReader(io.StringIO(changes.log_csv())))
    assert {"catalog_version": "ReCET 0.1.88", "field": "ElecBen", "change": "changed", "attribute": "Unit"}.items() <= next(
        r for r in rows if r["field"] == "ElecBen" and r["attribute"] == "Unit"
    ).items()


def test_the_pages(client, signed_in, catalog, cpuc_admin, pge_admin):
    v2, _ = retire(cpuc_admin, days=0)
    r = client.get("/dictionary/")
    assert (
        r.status_code == 200
        and ">MeasDescription<" not in page_only(r.content.decode())
        and "1 retired field is not shown" in r.content.decode()
    )
    r = client.get("/dictionary/?state=retired")
    assert ">MeasDescription<" in r.content.decode() and "Retired from" in r.content.decode()
    r = client.get("/dictionary/fields/MeasDescription/")
    body = page_only(r.content.decode())
    assert "Usage review" in body and REVIEWS["regulatory"] in body and "Retire or reinstate" not in body
    assert client.get("/dictionary/changes/").status_code == 200
    r = client.get(f"/dictionary/changes/{v2.pk}/")
    assert r.status_code == 200 and "MeasDescription" in r.content.decode()
    assert client.get("/dictionary/changes.csv")["Content-Disposition"].endswith('changes.csv"')
    assert client.get("/dictionary/fields/Sector/retire/").status_code == 302  # signed out: to sign in
    signed_in(pge_admin)
    assert client.get("/dictionary/fields/Sector/retire/").status_code == 403
    signed_in(cpuc_admin)
    assert "Review and retire" in client.get("/dictionary/fields/Sector/").content.decode()
    r = client.post("/dictionary/fields/Sector/retire/", {"retired_from": today().isoformat(), "reason": "x"})
    assert r.status_code == 200 and "This field is required" in r.content.decode()
    data = {
        "retired_from": today().isoformat(),
        "reason": REVIEWS["reason"],
        "review_usage": REVIEWS["usage"],
        "review_audience": REVIEWS["audience"],
        "review_regulatory": REVIEWS["regulatory"],
    }
    assert client.post("/dictionary/fields/Sector/retire/", data).status_code == 302
    assert service.resolve("Sector").state == Field.State.RETIRED
    r = client.post("/dictionary/fields/Sector/reinstate", {"reason": "Needed"})
    assert r.status_code == 302 and service.resolve("Sector").state == Field.State.ACTIVE
