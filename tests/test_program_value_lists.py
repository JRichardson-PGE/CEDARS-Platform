"""A program's status, segment, primary sector, implementer and category take their values from value lists kept on the data
dictionary's Value lists pages: the program fields guide's values are each list's first version, and the version in force is what the
change form offers and the guide shows."""

from datetime import timedelta

import pytest
from django.utils import timezone

from cedars.services.dictionary import value_lists as vls
from cedars.services.registry import field_guide

from .test_programs import programs  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()


def test_the_program_lists_are_on_the_value_lists_page_engine_or_not(client):
    assert set(field_guide.VALUE_LISTS.values()) <= set(vls.platform_lists())
    html = client.get("/dictionary/lists/").content.decode()
    for name in field_guide.VALUE_LISTS.values():
        assert f'href="/dictionary/lists/{name}/"' in html
    assert "Prg_ProgramStatus" in html and "As first listed" in html
    page = client.get("/dictionary/lists/program_status/").content.decode()
    assert "the values in the program fields guide is in force" in page
    assert "Active" in page  # the codes as first listed


def test_a_first_draft_copies_the_guides_values_with_their_meanings(cpuc_admin):
    vl = vls.get_list("program_status")
    assert vl.label == "Prg_ProgramStatus"
    d = vls.start_draft(cpuc_admin, vl)
    assert d.based_on == "the values in the program fields guide"
    first = field_guide.entry("status").values
    assert [c.code for c in d.codes.order_by("pk")] == [v.code for v in first]
    new = d.codes.get(code="new")
    assert new.description == "New" and new.extra == {"meaning": "Approved, and not yet taking participants."}
    assert vls.codes_in_force("program_status") is None  # nothing published: the guide's values stay in force
    assert field_guide.values("status") == first


def test_a_published_version_is_what_the_change_form_offers(signed_in, cpuc_admin, pge_admin, programs):  # noqa: F811
    vl = vls.get_list("program_status")
    d = vls.start_draft(cpuc_admin, vl)
    vls.add_code(cpuc_admin, d, "suspended", description="Suspended", reason="paused by the CPUC")
    vls.change_code(cpuc_admin, d.codes.get(code="new"), end_year=TODAY.year - 1, reason="no longer used")
    vls.publish(cpuc_admin, d, TODAY, "program statuses for this cycle")
    labels = {v.code: v for v in field_guide.values("status")}
    assert labels["suspended"].label == "Suspended" and labels["new"].retired_from.year == TODAY.year
    assert labels["active"].meaning  # meanings carried from the guide
    form = signed_in(pge_admin).get("/programs/PGE_Res_HVAC/change").content.decode()
    assert 'value="suspended"' in form and 'value="new"' not in form  # retired, and the program does not have it
    guide = signed_in(pge_admin).get("/programs/fields/").content.decode()
    assert "Suspended" in guide and 'href="/dictionary/lists/program_status/"' in guide
    assert "program_status" not in vls.engine_changes()  # never handed to the engine


def test_a_later_version_waits_for_its_date(cpuc_admin):
    vl = vls.get_list("program_category")
    d = vls.start_draft(cpuc_admin, vl)
    vls.add_code(cpuc_admin, d, "Pilot", description="Pilot")
    vls.publish(cpuc_admin, d, TODAY + timedelta(days=30), "from next month")
    assert "Pilot" not in {v.code for v in field_guide.values("category")}
