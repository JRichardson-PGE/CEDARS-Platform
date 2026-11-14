"""Legacy CEDARS' download layouts as historic specification versions (cedars.services.specifications.historic): generated from the
crosswalk, stored beside the code, loaded by a migration, read by anyone, never set up on a filing, checked against, edited or
exported, and linked from the imported years loaded in them."""

import pytest
from django.core.management import call_command
from django.db import IntegrityError, InternalError, ProgrammingError, transaction

from cedars.modules.claims import legacy as claims_legacy
from cedars.services.dictionary import legacy as crosswalk
from cedars.services.specifications import editor, historic, service, transfer
from cedars.services.specifications.models import Specification, SpecificationVersion

from .conftest import page_only
from .test_legacy_claims import _version, _year

pytestmark = pytest.mark.django_db
REFUSED = (InternalError, ProgrammingError, IntegrityError)
URL = "/specifications/claims/legacy-cedars-claims-2023-2026/"


def claims_b() -> SpecificationVersion:
    return SpecificationVersion.objects.get(specification__code="claims", name="Legacy CEDARS claims, 2023-2026")


def test_the_stored_layouts_are_the_crosswalks():
    assert historic.stored() == historic.documents(), "run: uv run python manage.py specification_historic --write"
    call_command("specification_historic")  # the check form passes too


def test_each_layout_is_a_historic_version_with_every_column_in_order():
    versions = list(historic.versions())
    assert len(versions) == len(historic.LAYOUTS) == 5
    v = claims_b()
    assert v.status == "historic" and v.historic and v.public and v.published_at is None
    assert v.legacy_layouts == ["claims_b", "participants"]
    claims_table = v.tables.get(name="Claims")
    labels = list(claims_table.fields.order_by("position").values_list("name", flat=True))
    assert labels == list(crosswalk.columns("claims_b"))
    assert claims_table.keys == ["Claim ID"]
    assert not v.tables.get(name="Participants").required
    assert historic.years_of(v) == "2023-2026"
    assert historic.load() == []  # already there: loading again adds nothing


def test_historic_versions_are_frozen_and_never_deleted():
    v = claims_b()
    for change in (
        lambda: SpecificationVersion.objects.filter(pk=v.pk).update(name="Changed"),
        lambda: v.tables.filter(name="Claims").update(guidance="Changed"),
        lambda: SpecificationVersion.objects.filter(pk=v.pk).delete(),
    ):
        with pytest.raises(REFUSED), transaction.atomic():
            change()


def test_they_are_never_current_set_up_edited_or_exported(cpuc_admin):
    v = claims_b()
    spec = Specification.objects.get(code="claims")
    assert service.current(spec) != v and v not in service.readable_versions(cpuc_admin, spec)
    with pytest.raises(editor.EditError):
        editor.start_draft(cpuc_admin, v, "Claims from legacy", "No")
    with pytest.raises(editor.EditError):
        transfer.export(cpuc_admin, v)


def test_the_pages_show_a_layout_read_only(client, signed_in, cpuc_admin):
    page = client.get(URL)
    assert page.status_code == 200
    html = page_only(page.content.decode())
    assert "Legacy CEDARS layout" in html and "Column, as CEDARS labels it" in html and "Years imported in this layout" in html
    assert "No year has been imported in this layout here yet." in html
    assert "Upload template" not in html and "Built-in checks" not in html and "Filings that use it" not in html
    assert "Loaded as" in html
    for path in ("template.xlsx", "schema/Claims.json"):
        assert client.get(URL + path).status_code == 404
    assert client.get(URL + "fields-and-rules.xlsx").status_code == 200 and client.get(URL + "specification.json").status_code == 200
    index = client.get("/specifications/").content.decode()
    assert "Legacy CEDARS layouts" in index and URL in index
    admin = page_only(signed_in(cpuc_admin).get(URL).content.decode())
    assert "Change this specification" not in admin and "Export" not in admin


def test_an_imported_year_links_its_layout_and_the_layout_lists_it(tmp_path, client, signed_in, pge_admin):
    _year(tmp_path)
    claims_legacy.load(2023)
    v = _version("PGE")
    page = signed_in(pge_admin).get(f"/claims/versions/{v.pk}/").content.decode()
    assert 'id="legacy-layout"' in page and URL in page
    layout = page_only(client.get(URL).content.decode())
    assert "PGE: 2023" in layout and "SCE: 2023" in layout
    assert "No year has been imported" not in layout
