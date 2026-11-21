"""Filed fields (dictionary/filed.py) and "same as" links (platform.SAME_AS) in the data dictionary."""

import json

import pytest

from cedars.services.dictionary import filed, platform, service
from cedars.services.dictionary.models import Field
from tests.test_dictionary_platform import CATALOG
from tests.test_us_spelling import BRITISH


@pytest.fixture
def catalog(db):
    return service.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")), platform=True)


def test_the_specifications_fields_the_engine_lacks_are_filed_fields(catalog):
    by_name = {f.name: f for f in catalog.fields.all()}
    for name in ["ClaimID", "SiteID", "ParticipantID", "TotalFirstYearNetkWh", "YTDCommittedFunds", "WithdrawalReason", "PanelLaborCost"]:
        assert by_name[name].kind == Field.Kind.FILED, name
        assert by_name[name].definition, name
        assert by_name[name].source.startswith("filing specification: "), name
    # a field the engine has keeps the engine's entry
    assert by_name["PrgID"].kind == Field.Kind.INPUT and by_name["Comments"].kind == Field.Kind.INPUT
    # the vault's fields are confidential, and a field several tables have is one field naming each table
    assert by_name["ContactEmail"].audience == Field.Audience.CONFIDENTIAL
    assert by_name["SiteAddress"].audience == Field.Audience.CONFIDENTIAL
    assert "table Site" in by_name["SiteID"].source and "table ClaimSite" in by_name["SiteID"].source
    # the starting claims specification calls the measure key ClaimID and accepts CEInputID: CEInputID stays the engine's field
    assert by_name["CEInputID"].kind == Field.Kind.INPUT
    # filed fields add no upload-template usages: the specification's own templates are the templates for those tables
    assert not by_name["ClaimID"].usages.exists()


def test_filed_definitions_use_us_spelling(db):
    bad = {f["name"]: BRITISH.findall(f["definition"] + " " + f["group"]) for f in filed.catalog()}
    assert not {k: v for k, v in bad.items() if v}


def test_every_same_as_name_is_in_the_catalog(catalog):
    names = {f.name.lower() for f in catalog.fields.all()}
    missing = {f.name: [n for n in f.same_as if n.lower() not in names] for f in catalog.fields.filter(kind=Field.Kind.RECORD) if f.same_as}
    assert not {k: v for k, v in missing.items() if v}, "a SAME_AS name the catalog does not have"
    written = set(platform.SAME_AS) - {f["name"] for f in platform.catalog()}
    assert not written, "platform.SAME_AS names a column that no longer exists"


def test_same_as_links_records_to_the_fields_they_store(catalog):
    by_name = {f.name: f for f in catalog.fields.all()}
    assert by_name["registry.ProgramVersion.name"].same_as == ["Prg_ProgramName"]
    assert by_name["registry.Program.prg_id"].same_as == ["PrgID"]
    assert by_name["budgets.BudgetResult.pa"].same_as == ["CreditedPA"]
    assert by_name["claims.ClaimRecord.claim_id"].same_as == ["ClaimID"]
    assert by_name["impact_profiles.Permutation.sector"].same_as == ["DEER_Sector"]
    assert len(by_name["registry.ProgramVersion.flags"].same_as) == 10
    assert by_name["budgets.BudgetResult.values"].same_as == []
    assert "CreditedPA" in by_name["budgets.BudgetResult.pa"].definition


def test_field_pages_show_same_as_and_stored_in(client, catalog):
    page = client.get("/dictionary/fields/budgets.BudgetResult.pa/").content.decode()
    assert "Same as" in page and 'href="/dictionary/fields/CreditedPA/"' in page
    page = client.get("/dictionary/fields/CreditedPA/").content.decode()
    assert "Stored in" in page and "budgets.BudgetResult.pa" in page and "claims.ClaimResult.pa" in page
    page = client.get("/dictionary/fields/ClaimID/").content.decode()
    assert "Filed field" in page and "claims.ClaimRecord.claim_id" in page
    assert "ClaimID" in client.get("/dictionary/catalog.csv").content.decode()


def test_the_index_filters_filed_fields(client, catalog):
    page = client.get("/dictionary/?kind=filed&q=ParticipantID").content.decode()
    assert "ParticipantID" in page and "Filed fields" in page
