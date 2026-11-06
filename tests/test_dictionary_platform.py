"""The platform's own records in the data dictionary (dictionary/platform.py): every table described, every column defined."""

import json
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from cedars.services.dictionary import platform, service
from cedars.services.dictionary.models import CatalogVersion, Field
from tests.test_us_spelling import BRITISH

CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.93.json"


@pytest.fixture
def catalog(db):
    return service.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")), platform=True)


def test_every_model_is_described_or_left_out_with_a_reason():
    assert platform.undescribed() == [], "add the new model to platform.TABLES (with its audience) or NOT_DESCRIBED (with why)"
    assert not set(platform.TABLES) & set(platform.NOT_DESCRIBED)
    assert all(platform.NOT_DESCRIBED.values())


def test_every_column_has_a_definition():
    assert platform.missing_definitions() == [], "write a definition in dictionary/data/platform_fields.yaml"
    assert platform.stale_definitions() == [], "platform_fields.yaml defines a table or column that no longer exists"


def test_definitions_use_us_spelling():
    bad = {f["name"]: BRITISH.findall(f["definition"] + " " + f["group"]) for f in platform.catalog()}
    assert not {k: v for k, v in bad.items() if v}


def test_the_named_tables_are_there():
    names = {f["name"] for f in platform.catalog()}
    for name in [
        "budgets.BudgetLine.prg_id",
        "budgets.BudgetResult.values",
        "budgets.FundShift.status",
        "budgets.FundShiftLine.amount",
        "claims.ClaimRecord.claim_id",
        "monthly.ReportLine.table",
        "submissions.Deadline.due_at",
        "submissions.SubmissionVersion.status",
        "snapshots.PIIScan.outcome",
        "registry.ProgramVersion.primary_sector",
        "documents.Study.calmac_id",
    ]:
        assert name in names, name


def test_columns_carry_type_audience_and_values():
    by_name = {f["name"]: f for f in platform.catalog()}
    amount = by_name["budgets.FundShiftLine.amount"]
    assert amount["kind"] == "record" and amount["data_type"] == "number" and amount["audience"] == "public"
    assert amount["rounding"] == "stored to 2 decimal places" and "budgets_fundshiftline" in amount["source"]
    assert amount["group"] == "Budgets: fund shift lines"
    status = by_name["submissions.SubmissionVersion.status"]
    assert ["confirmed", "Confirmed"] in status["values"] and status["max_length"] == 10
    assert by_name["submissions.SubmissionVersion.confirmed_by"]["audience"] == "restricted"  # names a person
    assert by_name["submissions.SubmissionVersion.confirmed_by"]["data_type"] == "reference"
    assert by_name["vault.PIIContact.values"]["audience"] == "confidential"
    assert by_name["submissions.Comment.text"]["audience"] == "restricted"
    # the program fields guide's definition and valid values, so the two say the same
    sector = by_name["registry.ProgramVersion.primary_sector"]
    from cedars.services.registry import field_guide

    assert sector["definition"].startswith(field_guide.entry("primary_sector").definition[:30])
    assert sector["values"] == [[v.code, v.label] for v in field_guide.entry("primary_sector").values]
    # a help-text fragment gets its label
    assert by_name["registry.GoalSet.decision"]["definition"].startswith("Decision: e.g.")


def test_loaded_with_the_engine_catalog(catalog):
    assert catalog.label.startswith("ReCET 0.1.93 + platform ")
    records = catalog.fields.filter(kind=Field.Kind.RECORD)
    assert records.count() == len(platform.catalog()) and catalog.fields.exclude(kind=Field.Kind.RECORD).count() > 300
    # the engine's fields and the crosswalk are unchanged: platform names have no other names and resolve no upload header
    assert service.resolve("PrgYear").name == "PrgYear" and service.resolve("year") is None
    assert service.resolve("E3ClimateZone").name == "AC_Climate_Zone"
    assert not records.filter(aliases__isnull=False).exists()
    assert "budgets." not in service.crosswalk_csv()
    headers = [f.name for f, _ in service.table_fields("calculations", "ProgramCost")]
    assert service.map_headers(headers, "calculations", "ProgramCost").unknown == []
    assert "submissions.SubmissionVersion.status" in service.catalog_csv() and "confirmed = Confirmed" in service.catalog_csv()


def test_dictionary_pages_show_platform_records(client, catalog):
    r = client.get("/dictionary/?kind=record&group=Budgets: fund shift lines")
    page = r.content.decode()
    assert r.status_code == 200 and "budgets.FundShiftLine.amount" in page and "ElecBen" not in page
    assert "Platform records" in page
    r = client.get("/dictionary/fields/submissions.SubmissionVersion.status/")
    page = r.content.decode()
    assert r.status_code == 200 and "Allowed values" in page and "<code>confirmed</code>" in page
    assert "submissions.SubmissionVersion" in page and "Version n of a submission" in page
    # an engine field keeps its value list, a page at a time (#146)
    r = client.get("/dictionary/fields/AC_Elec_Impact_Profile/?page=2")
    assert r.status_code == 200 and "Value list" in r.content.decode() and "allowed-h" not in r.content.decode()


def test_load_command_adds_the_platform_unless_engine_only(db, capsys):
    call_command("load_dictionary", str(CATALOG))
    v = CatalogVersion.objects.get()
    assert v.fields.filter(kind="record").exists() and "platform records" in capsys.readouterr().out
    with pytest.raises(CommandError, match="already published"):
        call_command("load_dictionary", str(CATALOG))
    call_command("load_dictionary", str(CATALOG), "--engine-only", "--label", "engine only")
    assert not service.current().fields.filter(kind="record").exists()


def test_a_platform_change_shows_in_the_change_log(catalog):
    from cedars.services.dictionary import changes

    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    payload["fields"] += [
        {**f, "values": f["values"][:1]} if f["name"] == "submissions.SubmissionVersion.status" else f for f in platform.catalog()
    ]
    later = service.load_catalog(None, payload, label="later")
    c = changes.changes(later)
    status = next(x for x in c.of("changed") if x.name == "submissions.SubmissionVersion.status")
    assert ("Allowed values", "draft, validated, confirmed, published", "draft") in status.details
