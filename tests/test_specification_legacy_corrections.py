"""Specification versions for correcting legacy CEDARS filings (cedars.services.specifications.legacy_corrections): generated, stored
beside the code, loaded published by a migration, never a specification's current version or a choice for a filing set, found by the
legacy year they correct, naming the CET replica that prices them, and shown apart on the pages."""

import pytest
from django.core.management import call_command
from django.db import IntegrityError, InternalError, ProgrammingError, transaction

from cedars.services.specifications import editor, legacy_corrections, moving, service
from cedars.services.specifications.models import Specification, SpecificationVersion

from .conftest import page_only

pytestmark = pytest.mark.django_db
REFUSED = (InternalError, ProgrammingError, IntegrityError)
URL = "/specifications/claims/legacy-corrections-2024-2025/"


def _v(first: int) -> SpecificationVersion:
    return SpecificationVersion.objects.get(specification__code="claims", legacy_first_year=first)


def test_the_stored_versions_are_the_generators():
    assert legacy_corrections.stored() == legacy_corrections.documents(), (
        "run: uv run python manage.py specification_legacy_corrections --write"
    )
    call_command("specification_legacy_corrections")  # the check form passes too


def test_each_correction_is_a_published_version_for_its_years_and_replica():
    a, b = _v(2024), _v(2026)
    assert (a.legacy_first_year, a.legacy_last_year, a.calculation) == (2024, 2025, "cet-24.3")
    assert (b.legacy_first_year, b.legacy_last_year, b.calculation) == (2026, 2027, "cet-25.1")
    for v in (a, b):
        assert v.status == "published" and v.corrects_legacy and v.published_at is not None
        assert v.fingerprint == service.fingerprint(service.as_document(v))
        assert [t.name for t in v.tables.all()] == ["Claim", "Measure", "ProgramCost", "Withdrawal"]
        assert not any(t.vault for t in v.tables.all())
    assert a.legacy_years == "2024-2025" and a.calculation_label == "CET 24.3 replica"
    assert legacy_corrections.load() == []  # already there: loading again adds nothing


def test_a_field_the_legacy_download_never_held_is_optional():
    v = _v(2024)
    measure = {f.name: f for f in v.tables.get(name="Measure").fields.all()}
    claim = {f.name: f for f in v.tables.get(name="Claim").fields.all()}
    assert measure["ClaimID"].requirement == "required" and measure["PrgID"].requirement == "required"
    held = {m.canonical for m in legacy_corrections.crosswalk.columns("claims_b").values()}
    loosened = [f for f in claim.values() if f.name not in held]
    assert loosened and all(f.requirement != "required" for f in loosened)
    assert all(legacy_corrections.NOT_IN_LEGACY in f.guidance for f in loosened if f.requirement == "optional" and f.guidance)
    starting = {t["name"]: t for t in service.stored_starting("claims")["tables"]}
    for f in starting["ProgramCost"]["fields"]:  # legacy years carry no program cost table: it is filed in the platform's format
        assert v.tables.get(name="ProgramCost").fields.get(name=f["name"]).requirement == f["requirement"]


def test_found_by_the_year_they_correct():
    assert service.for_legacy("claims", 2024) == _v(2024) and service.for_legacy("claims", 2025) == _v(2024)
    assert service.for_legacy("claims", 2026) == _v(2026) and service.for_legacy("claims", 2027) == _v(2026)
    assert service.for_legacy("claims", 2023) is None and service.for_legacy("monthly", 2025) is None
    assert service.calculation_of(_v(2026)) == "cet-25.1"
    assert service.calculation_of(service.current(Specification.objects.get(code="claims"))) == "recet"
    assert service.calculation_of(None) == "recet"


def test_never_current_coming_or_a_choice_for_a_filing_set(cpuc_admin):
    spec = Specification.objects.get(code="claims")
    corrections = set(service.legacy_corrections(spec))
    assert len(corrections) == 2
    assert service.current(spec) not in corrections
    assert not corrections & set(service.coming(spec))
    assert not corrections & set(moving.choices("claims"))
    assert not corrections & set(service.readable_versions(cpuc_admin, spec))
    assert moving.setup_problems(_v(2024), "claims")  # not one a filing set may use


def test_frozen_like_any_published_version():
    v = _v(2024)
    for change in (
        lambda: SpecificationVersion.objects.filter(pk=v.pk).update(calculation="cet-25.1"),
        lambda: SpecificationVersion.objects.filter(pk=v.pk).update(legacy_last_year=2030),
        lambda: SpecificationVersion.objects.filter(pk=v.pk).delete(),
    ):
        with pytest.raises(REFUSED), transaction.atomic():
            change()


def test_years_must_be_in_order():
    v = SpecificationVersion.objects.filter(status="published").exclude(legacy_first_year__isnull=False).first()
    draft = SpecificationVersion(
        specification=v.specification, name="Backwards", slug="backwards", legacy_first_year=2026, legacy_last_year=2025
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        draft.save()


def test_a_draft_copied_from_one_keeps_its_years_and_replica(cpuc_admin):
    draft = editor.start_draft(cpuc_admin, _v(2026), "Legacy corrections, 2026-2027, second", "A field's guidance is wrong")
    assert (draft.legacy_first_year, draft.legacy_last_year, draft.calculation) == (2026, 2027, "cet-25.1")


def test_the_pages_show_them_apart(client):
    page = client.get(URL)
    assert page.status_code == 200
    html = page_only(page.content.decode())
    assert "For correcting legacy CEDARS filings of 2024-2025 only" in html and "CET 24.3 replica" in html
    assert "The current version" not in html
    index = page_only(client.get("/specifications/").content.decode())
    assert "For correcting legacy filings" in index and URL in index and "2026-2027, priced on the CET 25.1 replica" in index
    versions_table = index.split("For correcting legacy filings")[0]
    assert "Legacy corrections, 2024-2025</a></th>" not in versions_table  # not listed with the ordinary versions


def test_loaded_published_while_the_specification_has_a_draft(cpuc_admin):
    spec = Specification.objects.get(code="claims")
    draft = editor.start_draft(cpuc_admin, service.current(spec), "Claims 2030", "Next year")
    doc = legacy_corrections.document(legacy_corrections.Correction("claims", 2028, 2029, "cet-25.1", "CET 25.1"))
    v = service.load(doc, status="published")
    assert v.specification == spec and v.status == "published" and v.tables.count() == 4
    draft.refresh_from_db()
    assert draft.status == "draft" and not Specification.objects.filter(code__startswith="loading-").exists()
