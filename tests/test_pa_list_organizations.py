"""The PA value list follows the organizations (dictionary.value_lists.follow_organisations)."""

from datetime import date

import pytest

from cedars.services.accounts import service as access
from cedars.services.dictionary import value_lists
from tests.conftest import sign_in

pytest.importorskip("recet")


def _draft_rows():
    d = value_lists.draft_of(value_lists.get_list("pa"))
    return d, {c.code: c for c in d.codes.all()} if d else {}


def test_a_new_pa_opens_a_draft_with_its_code_name_and_group(cpuc_admin):
    access.create_organisation(cpuc_admin, code="NEWREN", name="New Regional Energy Network", kind="pa", pa_type="REN")
    d, rows = _draft_rows()
    assert d is not None and d.status == "draft"
    new = rows["NEWREN"]
    assert new.description == "New Regional Energy Network" and new.end_year is None
    assert new.start_year == date.today().year and new.extra["pa_group"] == "CCA_REN"
    assert rows["SW"].description == "Statewide"  # a code no organization has stays as it is


def test_a_rename_ends_the_former_code_and_a_retirement_ends_the_code(cpuc_admin):
    ebce = access.create_organisation(cpuc_admin, code="EBCE", name="East Bay Community Energy", kind="pa", pa_type="CCA")
    access.rename_organisation(cpuc_admin, ebce, code="AVA", name="Ava Community Energy", effective=date(2024, 1, 1), reason="renamed")
    _, rows = _draft_rows()
    assert rows["EBCE"].end_year == 2023 and "now AVA" in rows["EBCE"].description
    assert rows["AVA"].description == "Ava Community Energy" and rows["AVA"].end_year is None
    mce = access.create_organisation(cpuc_admin, code="MCE", name="MCE", kind="pa", pa_type="CCA")
    access.retire_organisation(cpuc_admin, mce, reason="no longer a PA")
    _, rows = _draft_rows()
    assert rows["MCE"].end_year == date.today().year - 1 and rows["MCE"].description == "MCE"


def test_changes_the_platform_makes_itself_wait_for_the_list_page(cpuc_admin):
    access.create_organisation(None, code="PCE", name="Peninsula Clean Energy", kind="pa", pa_type="CCA")
    assert value_lists.draft_of(value_lists.get_list("pa")) is None
    diff = value_lists.organisation_differences()
    assert "PCE" not in diff["added"]  # PCE is already in the shipped list; the name differs only if it does
    access.create_organisation(None, code="ZZPA", name="A new PA", kind="pa", pa_type="CCA")
    assert "ZZPA" in value_lists.organisation_differences()["added"]


def test_the_list_page_shows_the_difference_and_drafts_it(client, cpuc_admin):
    access.create_organisation(None, code="ZZPA", name="A new PA", kind="pa", pa_type="CCA")
    sign_in(client, cpuc_admin)
    page = client.get("/dictionary/lists/pa/").content.decode()
    assert "Follows the organizations" in page and "ZZPA" in page and "Draft from the organizations" in page
    r = client.post("/dictionary/lists/pa/follow-organizations")
    assert r.status_code == 302
    _, rows = _draft_rows()
    assert "ZZPA" in rows
    assert 'id="orgs-h"' not in client.get("/dictionary/lists/sector/").content.decode()


def test_pacificorp_is_a_known_iou():
    assert access.known_pa_type("PAC") == "IOU" and access.known_pa_name("PAC") == "PacifiCorp"
