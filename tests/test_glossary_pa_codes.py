"""The glossary's program administrator codes come from the organizations on the site: renames, new PAs and merges show at once."""

from datetime import date

import pytest

from cedars.services.accounts import service as access

pytestmark = pytest.mark.django_db


def test_the_table_follows_the_organizations(client, cpuc_admin, pge):
    ebce = access.create_organisation(cpuc_admin, code="EBCE", name="East Bay Community Energy", kind="pa")
    access.create_organisation(cpuc_admin, code="BAY", name="Bay Area Regional Energy Network", kind="pa")
    access.create_organisation(cpuc_admin, code="NEWPA", name="A new PA", kind="pa")
    access.rename_organisation(cpuc_admin, ebce, code="AVA", name="Ava Community Energy", reason="renamed", effective=date(2024, 1, 1))
    table = access.pa_code_table()
    codes = [r[0] for r in table]
    assert codes[0] == "PGE" and codes.index("BAY") < codes.index("AVA") and codes[-1] == "NEWPA"
    rows = {r[0]: r for r in table}
    assert rows["AVA"][1:] == ("Ava Community Energy", "CCA")
    assert rows["EBCE"][1] == "Ava Community Energy's code before 2024 (now AVA)"
    assert rows["NEWPA"][2] == "Not set yet" and "SW" not in rows
    page = client.get("/help/glossary/").content.decode()
    assert "Ava Community Energy&#x27;s code before 2024 (now AVA)" in page and "A new PA" in page
