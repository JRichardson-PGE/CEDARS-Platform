import io
import json
from pathlib import Path

import pytest
from django.core.management import call_command
from openpyxl import load_workbook

from cedars.services.dictionary import service
from cedars.services.dictionary.models import CatalogVersion

pytestmark = pytest.mark.django_db
CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.86.json"


@pytest.fixture
def catalog(db):
    return service.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))


def test_the_catalog_carries_every_attribute(catalog):
    assert catalog.label == "ReCET 0.1.86"
    f = service.resolve("ElecBen")
    assert f.kind == "output" and "TRC" in f.cost_tests and f.unit.startswith("Dollars") and f.time_basis and f.aggregation
    assert catalog.fields.count() > 300


def test_cet_names_resolve_to_canonical_ones(catalog):
    assert service.resolve("E3ClimateZone").name == "AC_Climate_Zone"
    assert service.resolve(" e3climatezone ").name == "AC_Climate_Zone"
    assert service.resolve("NotAField") is None


def test_header_mapping_accepts_cet_names_and_ignores_others(catalog):
    headers = [f.name for f, _ in service.table_fields("calculations", "Measure")]
    headers = ["E3ClimateZone" if h == "AC_Climate_Zone" else h for h in headers if h != "NumUnits"] + ["MyOwnNotes", "ElecBen"]
    m = service.map_headers(headers, "calculations", "Measure")
    assert m.renamed == {"E3ClimateZone": "AC_Climate_Zone"}
    assert m.unknown == ["MyOwnNotes"] and m.not_used == ["ElecBen"]
    assert m.missing == ["NumUnits"]


def test_templates_follow_the_catalog(catalog):
    header = service.template_csv("calculations", "Measure").strip().split("|")
    assert header[:2] == ["CEInputID", "PrgID"] and "AC_Climate_Zone" in header and "E3ClimateZone" not in header
    wb = load_workbook(io.BytesIO(service.template_xlsx("calculations", "ProgramCost")))
    assert [c.value for c in wb["ProgramCost"][1]][:2] == ["PrgID", "PrgYear"]
    assert wb["Fields"]["A2"].value == "PrgID"
    schema = service.json_schema("calculations", "Measure")
    assert "AC_Climate_Zone" in schema["properties"] and "NumUnits" in schema["required"]


def test_catalog_and_crosswalk_downloads(catalog):
    assert "E3ClimateZone,CET,AC_Climate_Zone" in service.crosswalk_csv()
    assert service.catalog_csv().startswith("name,kind,group,definition")


def test_public_dictionary_pages(client, catalog):
    r = client.get("/dictionary/?q=climate")
    assert r.status_code == 200 and "AC_Climate_Zone" in r.content.decode()
    r = client.get("/dictionary/fields/AC_Climate_Zone/")
    assert "E3ClimateZone (CET)" in r.content.decode()
    assert client.get("/dictionary/templates/calculations/Measure.xlsx").status_code == 200
    assert client.get("/dictionary/templates/calculations/Measure.json").json()["title"] == "calculations Measure row"
    assert client.get("/dictionary/templates/claims/Nope.csv").status_code == 404


def test_load_command(db):
    call_command("load_dictionary", str(CATALOG), "--label", "test catalog")
    assert CatalogVersion.objects.get().label == "test catalog"


def test_the_shipped_catalog_matches_the_pinned_engine():
    """When the pinned ReCET is installed, the shipped catalog file must be the one it exports (re-run the export after a pin change)."""
    recet = pytest.importorskip("recet")
    import importlib.util

    spec = importlib.util.spec_from_file_location("export", Path(__file__).parent.parent / "scripts" / "export_recet_dictionary.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert CATALOG.name == f"recet-{recet.__version__}.json"
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        assert mod.main(Path(d)).read_text(encoding="utf-8") == CATALOG.read_text(encoding="utf-8")
