"""Correcting records, the pages (UI review 3, U3): Correct records opens on the records with findings, and a record's page lays its fields
out in the wide grid by section, fields with findings first, with each hint behind its "i" button, a box that finds a field, a value
list's codes suggested as you type, a Save bar that stays in view, and a way back to the filing once saved."""

import json
import re
from pathlib import Path

import pytest
from django.urls import reverse

from cedars.modules.claims import service
from cedars.services.dictionary import service as dictionary
from cedars.services.submissions import corrections

from .conftest import sign_in
from .test_claims import claim, claims_zip, deadline, filer, programs, row, tables, year  # noqa: F401
from .test_record_corrections import bad_q1

pytestmark = pytest.mark.django_db
CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.93.json"


@pytest.fixture
def catalog(db):
    return dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))


@pytest.fixture
def sector_list(monkeypatch):
    """Sector's value list, given here so the pages are tested without the engine (whose lists they read when it is installed): four
    codes, one retired before the filing's year."""
    from cedars.services.dictionary import value_lists

    codes = [("Res", None), ("Com", None), ("Ind", 2020), ("Ag", None)]
    monkeypatch.setattr(value_lists, "list_for_field", lambda f: "sector" if f.name == "Sector" else None)
    monkeypatch.setattr(value_lists, "codes_in_force", lambda name, on=None: [{"code": c, "end_year": e} for c, e in codes])


def main(html: str) -> str:
    return html.split('<main id="main"', 1)[-1].split("</main>", 1)[0]


def test_correct_records_opens_on_the_records_with_findings(client, filer, year, pge, programs):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    flagged, whole, n = corrections.with_findings(v)
    assert n == 1 and not whole and flagged[0].found.key == ("PGE-2028-002",) and flagged[0].rules == ["CLM-PRG"]
    assert flagged[0].rejected and flagged[0].found.line == 3
    sign_in(client, filer)
    body = main(client.get(reverse("submissions:records", args=[v.pk])).content.decode())
    assert "Records with findings: 1" in body and body.index('id="flagged-h"') < body.index('id="find-h"')
    assert f'href="{reverse("submissions:record", args=[v.pk, "Measure"])}?k=PGE-2028-002"' in body
    assert "CLM-PRG" in body and ">Rejected</span>" in body


def test_findings_about_a_whole_table_are_said_apart(client, filer, year, pge, programs):  # noqa: F811
    t = tables(claim("PGE-2028-001"), ProgramCost=[row("ProgramCost", PrgID="PGE_Res")], Site=[row("Site", SiteID="PGE-S1")])
    for r in t["Measure"]:
        r.pop("NumUnits")  # a required column the Measure table lacks
    thin = service.upload_claims(filer, deadline(), pge, claims_zip(t), "q1.zip")
    flagged, whole, _ = corrections.with_findings(thin)
    assert whole and all(f["row"] == 1 for f in whole) and {f["rule_id"] for f in whole} >= {"CLM-COL"}
    sign_in(client, filer)
    body = main(client.get(reverse("submissions:records", args=[thin.pk])).content.decode())
    assert 'id="whole-table"' in body and "About a whole table" in body


def test_a_record_s_fields_by_section_with_findings_first(client, filer, year, pge, programs, catalog, sector_list):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    sign_in(client, filer)
    body = main(client.get(reverse("submissions:record", args=[v.pk, "Measure"]) + "?k=PGE-2028-002").content.decode())
    legends = re.findall(r"<legend>([^<]+)</legend>", body)
    assert legends[:2] == ["Fields with findings", "Key"] and "Quantities" in legends and "Useful life" in legends
    found = body.split('id="fields-found"', 1)[1].split("</fieldset>", 1)[0]
    assert 'id="in-PrgID"' in found and 'aria-invalid="true"' in found
    # every hint behind its "i" button, still the control's description
    assert body.count('class="info-btn"') == body.count('class="info-pop"') == len(re.findall(r'id="in-(?!reason)', body))
    assert 'aria-describedby="help-NumUnits"' in body and 'id="help-NumUnits"' in body
    # a value list's codes are suggested as you type, and typing anything else is still allowed
    assert 'list="codes-Sector"' in body and 'type="text" id="in-Sector"' in body
    offered = re.findall(r'<option value="([^"]+)">', body.split('<datalist id="codes-Sector">', 1)[1].split("</datalist>", 1)[0])
    assert offered == ["Res", "Com", "Ag"]  # the code retired in 2020 is left out of a 2028 filing's suggestions
    # the Save bar and the box that finds a field
    assert (
        'id="save-bar"' in body
        and 'id="find-field"' in body
        and "core/find_field.js" in client.get(reverse("submissions:record", args=[v.pk, "Measure"]) + "?k=PGE-2028-002").content.decode()
    )


def test_a_long_value_list_links_to_its_page_instead(client, filer, year, pge, programs, catalog, settings, sector_list):  # noqa: F811
    settings.CEDARS_CORRECTION_LIST_CODES = 2  # Sector has three codes in use in 2028
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    sign_in(client, filer)
    body = main(client.get(reverse("submissions:record", args=[v.pk, "Measure"]) + "?k=PGE-2028-002").content.decode())
    assert 'list="codes-Sector"' not in body and "Look up a code<span" in body
    assert reverse("dictionary:list", args=["sector"]) in body


def test_find_a_field_without_scripts(client, filer, year, pge, programs):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    sign_in(client, filer)
    url = reverse("submissions:record", args=[v.pk, "Measure"])
    r = client.get(url, {"k": "PGE-2028-002", "find": "eul_y"})
    assert r.status_code == 302 and r.url == url + "?k=PGE-2028-002#f-eul_yrs"
    r = client.get(url, {"k": "PGE-2028-002", "find": "nothing like it"}, follow=True)
    assert "No field of the Measure table is named nothing like it." in r.content.decode()
    new = reverse("submissions:record_new", args=[v.pk, "Site"])
    assert client.get(new, {"find": "siteid"}).url == new + "#f-siteid"


def test_after_saving_the_page_leads_back_to_the_filing(client, filer, year, pge, programs):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad_q1(), "q1.zip")
    sign_in(client, filer)
    edit = reverse("submissions:record", args=[v.pk, "Measure"]) + "?k=PGE-2028-002"
    form = {f"f-{c}": val for c, val in service.own_tables(v)["Measure"].rows[1].items()}
    r = client.post(edit, {**form, "f-PrgID": "PGE_Res", "state": corrections.state(v), "k": "PGE-2028-002"})
    assert r.status_code == 302 and r.url.endswith("&saved=1")
    body = main(client.get(r.url).content.decode())
    period = reverse("claims:period", args=[deadline().pk])
    assert 'id="saved"' in body and "price it again" in body and f'href="{period}?org=PGE">Back to the filing' in body
