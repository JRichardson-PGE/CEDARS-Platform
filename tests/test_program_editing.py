"""Adding a program on its page, correcting a version's values in place, and the delivery flags on the program forms."""

from datetime import date

import pytest

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program, ProgramChangeRequest

from .conftest import page_only
from .test_programs import programs  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db


@pytest.fixture
def approver(make_user, cpuc, cpuc_admin):
    u = make_user("approver@cpuc.example.org")
    access.grant(cpuc_admin, u, cpuc, "programs.approve")
    return u


def _new(**over) -> dict:
    data = {
        "administrator": "PGE",
        "prg_id": "PGE_Com_Light",
        "effective": "2028-01-01",
        "reason": "approved by advice letter 9999-E",
        "name": "Commercial Lighting",
        "status": "new",
        "segment": "resource_acquisition",
        "flag_resource": "on",
        "flag_downstream": "on",
        "flag_direct_install": "Partial",
        "start_year": "2028",
    }
    data.update(over)
    return data


# --- adding a program


def test_a_pa_admin_adds_their_own_pas_program_from_the_page(signed_in, pge_admin, programs):  # noqa: F811
    c = signed_in(pge_admin)
    html = c.get("/programs/").content.decode()
    assert 'href="/programs/new/"' in html
    page = c.get("/programs/new/").content.decode()
    assert "Add a program" in page and 'value="SW"' not in page  # only CPUC system administrators add statewide programs
    r = c.post("/programs/new/", _new())
    assert r.status_code == 302 and r["Location"] == "/programs/PGE_Com_Light/"
    p = Program.objects.get(prg_id="PGE_Com_Light")
    v = p.versions.get()
    assert p.administrator.code == "PGE" and not p.statewide and v.valid_from == date(2028, 1, 1)
    assert (v.name, v.status, v.start_year, v.created_by) == ("Commercial Lighting", "new", 2028, pge_admin)
    assert v.flags == {"Resource_Flag": "True", "Downstream_Flag": "True", "DirectInstall": "Partial"}
    e = AuditEvent.objects.get(action="program.created", object_id=str(p.pk))
    assert e.actor == pge_admin and e.reason == "approved by advice letter 9999-E"
    shown = c.get("/programs/PGE_Com_Light/").content.decode()
    assert "Downstream, Resource, Direct install: Partial" in shown


def test_a_new_prg_id_starts_with_the_pas_code_and_is_never_reused(signed_in, pge_admin, programs):  # noqa: F811
    c = signed_in(pge_admin)
    for prg_id, words in [
        ("SCE_Mine", "starts with PGE"),
        ("PGE res", "no spaces"),
        ("pge_res_hvac", "never reused"),  # any case
    ]:
        html = c.post("/programs/new/", _new(prg_id=prg_id)).content.decode()
        assert words in html, prg_id
    assert not Program.objects.filter(prg_id__in=["SCE_Mine", "PGE res"]).exists()
    assert registry.prg_id_problem("SW_New", None, statewide=True) == ""
    assert "starts with SW_" in registry.prg_id_problem("STATE_New", None, statewide=True)


def test_only_a_pas_own_admins_or_the_cpuc_add_its_programs(signed_in, make_user, pge_admin, cpuc_admin, programs):  # noqa: F811
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    assert not registry.can_add_program(pge_admin, sce) and registry.can_add_program(pge_admin, access.by_code("PGE"))
    assert not registry.can_add_program(pge_admin, None, statewide=True)
    with pytest.raises(AccessDenied):
        registry.create_program(pge_admin, prg_id="SCE_X", administrator=sce, valid_from=date(2028, 1, 1), name="X")
    c = signed_in(pge_admin)
    html = c.post("/programs/new/", _new(administrator="SCE", prg_id="SCE_X")).content.decode()
    assert "Select a valid choice" in html and not Program.objects.filter(prg_id="SCE_X").exists()
    member = make_user("someone@example.com")
    assert signed_in(member).get("/programs/new/").status_code == 404
    assert 'href="/programs/new/"' not in page_only(signed_in(member).get("/programs/").content.decode())


def test_the_cpuc_adds_a_statewide_program_and_a_pas_id_of_it_follows_it(signed_in, cpuc_admin, pge_admin, programs):  # noqa: F811
    c = signed_in(cpuc_admin)
    assert 'value="SW"' in c.get("/programs/new/").content.decode()
    r = c.post("/programs/new/", _new(administrator="SW", prg_id="SW_New", name="Statewide New", flag_direct_install=""))
    assert r.status_code == 302
    sw = Program.objects.get(prg_id="SW_New")
    assert sw.statewide and sw.administrator is None
    # a PA's ID of it: no attributes needed, they come from the statewide program, even though it has no IDs yet
    r = signed_in(pge_admin).post("/programs/new/", _new(prg_id="PGE_SW_New", name="", status=""))
    assert r.status_code == 302
    sub = Program.objects.get(prg_id="PGE_SW_New")
    assert sub.follows == sw and sub.versions.get().name == "Statewide New"
    assert sub.versions.get().statewide_program == "SW_New - Statewide New"


# --- correcting a version


def test_a_cpuc_admin_corrects_a_past_versions_values_in_place(signed_in, cpuc_admin, programs):  # noqa: F811
    old = programs["old"]
    first = old.versions.order_by("valid_from").first()
    assert first.valid_to == date(2021, 12, 31)
    with pytest.raises(AccessDenied, match="must start after"):  # a change cannot reach back
        registry.change_program(cpuc_admin, old, effective=date(2019, 1, 1), reason="r", primary_sector="Commercial")
    c = signed_in(cpuc_admin)
    assert f"/programs/PGE21001/versions/{first.pk}/correct" in c.get("/programs/PGE21001/").content.decode()
    form = c.get(f"/programs/PGE21001/versions/{first.pk}/correct").content.decode()
    assert "Save the correction" in form and 'value="Old Lighting"' in form
    data = {"reason": "the import had no sector", "name": "Old Lighting", "status": "active", "primary_sector": "Commercial"}
    r = c.post(f"/programs/PGE21001/versions/{first.pk}/correct", data)
    assert r.status_code == 302
    first.refresh_from_db()
    assert first.primary_sector == "Commercial" and first.valid_to == date(2021, 12, 31) and first.corrected_by == cpuc_admin
    assert old.versions.count() == 2  # no new version
    e = AuditEvent.objects.get(action="program.corrected")
    assert e.before["primary_sector"] == "" and e.after["primary_sector"] == "Commercial" and e.reason == "the import had no sector"
    assert "Corrected" in c.get("/programs/PGE21001/").content.decode()
    with pytest.raises(AccessDenied, match="Nothing was changed"):
        registry.correct_version(cpuc_admin, first, reason="again", primary_sector="Commercial")


def test_a_pa_admin_requests_a_correction_and_an_approver_decides_it(signed_in, pge_admin, approver, programs):  # noqa: F811
    hvac = programs["hvac"]
    v = hvac.versions.get()
    with pytest.raises(AccessDenied, match="ask for a correction"):
        registry.correct_version(pge_admin, v, reason="r", name="Residential HVAC Program")
    c = signed_in(pge_admin)
    form = c.get(f"/programs/PGE_Res_HVAC/versions/{v.pk}/correct").content.decode()
    assert "Send the correction for approval" in form
    r = c.post(
        f"/programs/PGE_Res_HVAC/versions/{v.pk}/correct",
        {
            "reason": "typo in the name",
            "name": "Residential HVAC Program",
            "status": "active",
            "segment": "resource_acquisition",
            "sectors": "Residential: 1",
            "start_year": "2024",
        },
    )
    req = ProgramChangeRequest.objects.get()
    assert r["Location"] == f"/programs/requests/{req.pk}/"
    assert req.corrects == v and req.changes == {"name": "Residential HVAC Program"} and req.before == {"name": "Residential HVAC"}
    v.refresh_from_db()
    assert v.name == "Residential HVAC"  # nothing changes until it is decided
    page = c.get(f"/programs/requests/{req.pk}/").content.decode()
    assert "Correction of" in page and "in place" in page
    registry.decide_change(approver, req, True, "ok")
    v.refresh_from_db()
    req.refresh_from_db()
    assert v.name == "Residential HVAC Program" and hvac.versions.count() == 1 and req.version == v and v.corrected_by == approver


def test_correcting_a_statewide_program_reaches_the_ids_that_follow_it(cpuc_admin, pge_admin, programs):  # noqa: F811
    sw = programs["sw"]
    sub = registry.create_program(
        pge_admin, prg_id="PGE_SW_WP", administrator=access.by_code("PGE"), valid_from=date(2024, 1, 1), follow=True, name=""
    )
    assert sub.follows == sw
    registry.correct_version(cpuc_admin, sw.versions.get(), reason="name as approved", name="Statewide Water Pumping Program")
    assert sub.versions.get().name == "Statewide Water Pumping Program"
    assert not registry.can_correct(cpuc_admin, sub)  # an ID that follows is corrected through its statewide program
    with pytest.raises(AccessDenied, match="follows"):
        registry.correct_version(cpuc_admin, sub.versions.get(), reason="r", name="X")


def test_someone_who_cannot_change_the_program_cannot_correct_it(signed_in, make_user, programs):  # noqa: F811
    v = programs["hvac"].versions.get()
    c = signed_in(make_user("someone@example.com"))
    assert c.get(f"/programs/PGE_Res_HVAC/versions/{v.pk}/correct").status_code == 404
    assert "/correct" not in page_only(c.get("/programs/PGE_Res_HVAC/").content.decode())


# --- delivery flags on the change form


def test_the_change_form_changes_the_delivery_flags(signed_in, pge_admin, programs):  # noqa: F811
    hvac = programs["hvac"]
    registry.change_program(
        pge_admin, hvac, effective=date(2025, 1, 1), reason="import", flags={"Deemed_Flag": "True", "Financing": "OBF", "Extra": "x"}
    )
    c = signed_in(pge_admin)
    form = c.get("/programs/PGE_Res_HVAC/change").content.decode()
    assert "<legend>Delivery</legend>" in form and 'name="flag_deemed" checked' in form.replace(' id="id_flag_deemed"', "")
    r = c.post(
        "/programs/PGE_Res_HVAC/change",
        {
            "effective": "2026-01-01",
            "reason": "now custom too",
            "name": "Residential HVAC",
            "status": "active",
            "segment": "resource_acquisition",
            "sectors": "Residential: 1",
            "start_year": "2024",
            "flag_custom": "on",
            "flag_financing": "OBF",
        },
    )
    assert r.status_code == 302
    v = registry.version_on(hvac, date(2026, 1, 1))
    # the unticked flag it had becomes False; a key the form does not know stays; financing kept
    assert v.flags == {"Deemed_Flag": "False", "Financing": "OBF", "Extra": "x", "Custom_Flag": "True"}
    assert any(f == "flags" for f, _, _ in registry.history(hvac)[-1][1])


def test_the_delivery_flags_can_be_locked_like_any_attribute(signed_in, pge_admin, cpuc_admin, programs):  # noqa: F811
    registry.set_locked_fields(cpuc_admin, ["flags"], effective=date.today(), reason="flags decide reporting")
    html = signed_in(pge_admin).get("/programs/PGE_Res_HVAC/change").content.decode()
    assert "Locked: Delivery" in html
    assert registry.needs_approval(pge_admin, {"flags": {}}) == ["flags"]
