import csv
import io
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from cedars.services.accounts import service as access
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from cedars.services.registry.importers import import_cedars_programs

pytestmark = pytest.mark.django_db
DATA = Path(__file__).parent / "data"


@pytest.fixture
def programs(pge, pge_admin, cpuc_admin):
    hvac = registry.create_program(
        pge_admin,
        prg_id="PGE_Res_HVAC",
        administrator=pge,
        valid_from=date(2024, 1, 1),
        name="Residential HVAC",
        status="active",
        segment="resource_acquisition",
        sectors={"Residential": 1.0},
        start_year=2024,
    )
    old = registry.create_program(
        pge_admin, prg_id="PGE21001", administrator=pge, valid_from=date(2018, 1, 1), name="Old Lighting", status="active"
    )
    registry.change_program(pge_admin, old, effective=date(2022, 1, 1), reason="closed by D.21-05-031", status="closed", end_year=2021)
    sw = registry.create_program(
        cpuc_admin, prg_id="SW_WP", administrator=None, statewide=True, valid_from=date(2024, 1, 1), name="Statewide Water Pumping"
    )
    return {"hvac": hvac, "old": old, "sw": sw}


def test_the_catalog_lists_every_program_closed_ones_included(signed_in, make_user, programs):
    c = signed_in(make_user("someone@example.com"))
    html = c.get("/programs/").content.decode()
    assert "3 programs" in html and "PGE_Res_HVAC" in html and "PGE21001" in html and "SW_WP" in html
    assert c.get("/programs/?state=closed").content.decode().count('<th scope="row">') == 1
    html = c.get("/programs/?q=hvac").content.decode()
    assert "PGE_Res_HVAC" in html and "PGE21001" not in html
    html = c.get("/programs/?q=pge21").content.decode()  # PrgID, case-insensitive
    assert "PGE21001" in html and "PGE_Res_HVAC" not in html
    html = c.get("/programs/?pa=SW").content.decode()
    assert "SW_WP" in html and "PGE_Res_HVAC" not in html


def test_the_catalog_shows_the_attributes_of_the_chosen_program_year(signed_in, make_user, programs):
    c = signed_in(make_user("someone@example.com"))
    assert "Old Lighting" in c.get("/programs/?year=2020&state=open").content.decode()  # still open in 2020
    assert "Old Lighting" not in c.get("/programs/?year=2023&state=open").content.decode()


def test_the_catalog_download_carries_the_same_filter(signed_in, make_user, programs):
    c = signed_in(make_user("someone@example.com"))
    r = c.get("/programs/programs.csv?pa=PGE")
    assert r["Content-Type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.content.decode())))
    assert [row["PrgID"] for row in rows] == ["PGE21001", "PGE_Res_HVAC"]
    assert rows[1]["BPSector"] == "Residential: 1.000000" and rows[0]["ProgramStatus"] == "closed"


def test_the_catalog_and_program_pages_are_public(client, programs):
    """BR-01.5 / BR-07.7: anyone finds a program by name or PrgID, signed in or not; changing one still needs an account."""
    page = client.get("/programs/?q=PGE21001")
    assert page.status_code == 200 and "PGE21001" in page.content.decode()
    assert "change requests" not in page.content.decode()
    assert client.get("/programs/programs.csv").status_code == 200
    html = client.get("/programs/PGE21001/").content.decode()
    assert "PGE21001" in html and "Versions" in html
    assert "@" not in html.split('id="ver-h"')[1].split("</section>")[0]  # nobody's email for the public
    assert "Change this program" not in html
    for path in ("/programs/PGE21001/change", "/programs/requests/", "/programs/locked/"):
        assert client.get(path).status_code == 302


def test_portfolio_cost_ids_are_not_programs(signed_in, make_user, pge, pge_admin):
    registry.create_program(
        pge_admin, prg_id="PGE_Admin", administrator=pge, valid_from=date(2024, 1, 1), name="Admin", portfolio_cost=True
    )
    c = signed_in(make_user("someone@example.com"))
    assert "PGE_Admin" not in c.get("/programs/").content.decode()
    assert c.get("/programs/PGE_Admin/").status_code == 404


def test_a_program_page_shows_its_definition_and_versions(signed_in, make_user, programs):
    html = signed_in(make_user("someone@example.com")).get("/programs/PGE21001/").content.decode()
    assert "PGE21001: Old Lighting" in html and "Closed" in html
    assert "closed by D.21-05-031" in html and "Status: active to closed" in html and "First version" in html
    assert "Change this program" not in html  # not PG&amp;E's admin
    assert "Not a statewide program" in html


def test_a_statewide_program_page_shows_its_allocation_history(signed_in, make_user, cpuc_admin, programs):
    content = (DATA / "statewide_factors_20260918.csv").read_bytes()
    first = content.split(b"\n")[1].split(b",")[1].decode()  # a statewide PrgID the factor file has
    p = registry.create_program(
        cpuc_admin, prg_id=first, administrator=None, statewide=True, valid_from=date(2024, 1, 1), name="From the factor file"
    )
    v = registry.import_allocation_csv(cpuc_admin, content, label="2028 TUAL", purpose="filing")
    registry.confirm_allocation(cpuc_admin, v)
    html = signed_in(make_user("someone@example.com")).get(f"/programs/{p.prg_id}/").content.decode()
    years = v.entries.filter(program_id=first).count()
    assert "2028 TUAL" in html and html.count('<th scope="row">2028 TUAL</th>') == years
    assert "PGE weighted" in html and "SCG weighted" in html


def test_the_program_administrator_changes_a_program_from_a_date(signed_in, pge_admin, programs):
    c = signed_in(pge_admin)
    assert "Change this program" in c.get("/programs/PGE_Res_HVAC/").content.decode()
    form = c.get("/programs/PGE_Res_HVAC/change").context["form"]
    data = {k: v for k, v in form.initial.items() if v is not None}
    effective = timezone.localdate() + timedelta(days=30)
    data.update(
        effective=effective.isoformat(),
        reason="advice letter 4900-E",
        implementer="Third-Party Solicited",
        sectors="Residential: 0.8, Commercial: 0.2",
    )
    data = {k: v for k, v in data.items() if v is not False}
    r = c.post("/programs/PGE_Res_HVAC/change", data)
    assert r.status_code == 302, r.context["form"].errors
    new = registry.version_on(programs["hvac"], effective)
    assert new.implementer == "Third-Party Solicited" and new.sectors == {"Residential": 0.8, "Commercial": 0.2}
    assert new.name == "Residential HVAC"  # unchanged attributes carry over
    e = AuditEvent.objects.filter(action="program.changed", object_id=str(programs["hvac"].pk)).get()
    assert e.reason == "advice letter 4900-E" and e.actor == pge_admin
    assert sorted(k for k in e.after if e.before.get(k) != e.after[k]) == ["implementer", "sectors", "valid_from"]


def test_a_change_that_changes_nothing_or_breaks_a_rule_is_refused(signed_in, pge_admin, programs):
    c = signed_in(pge_admin)
    form = c.get("/programs/PGE_Res_HVAC/change").context["form"]
    data = {k: v for k, v in form.initial.items() if v not in (None, False)}
    data.update(effective=(timezone.localdate() + timedelta(days=1)).isoformat(), reason="nothing")
    assert "Nothing was changed" in c.post("/programs/PGE_Res_HVAC/change", data).content.decode()
    data.update(effective="2023-06-01", name="Renamed")  # before the latest version started
    assert "must start after 2024-01-01" in c.post("/programs/PGE_Res_HVAC/change", data).content.decode()
    data.update(effective=(timezone.localdate() + timedelta(days=1)).isoformat(), sectors="Residential: 0.5")
    assert "add up to 0.5, not 1" in c.post("/programs/PGE_Res_HVAC/change", data).content.decode()
    assert programs["hvac"].versions.count() == 1


def test_only_the_programs_administrators_and_the_cpuc_can_change_it(signed_in, make_user, cpuc_admin, programs):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    sce_admin = make_user("admin@sce.com")
    access.grant(cpuc_admin, sce_admin, sce, "org.admin", reason="first admin")
    assert signed_in(sce_admin).get("/programs/PGE_Res_HVAC/change").status_code == 404
    assert signed_in(cpuc_admin).get("/programs/PGE_Res_HVAC/change").status_code == 200
    assert signed_in(cpuc_admin).get("/programs/SW_WP/change").status_code == 200


def test_a_programs_admin_grant_can_change_its_organisations_programs(signed_in, make_user, pge, pge_admin, programs):
    analyst = make_user("analyst@pge.com")
    access.grant(pge_admin, analyst, pge, "programs.admin")
    assert signed_in(analyst).get("/programs/PGE_Res_HVAC/change").status_code == 200
    reader = make_user("reader@pge.com")
    access.grant(pge_admin, reader, pge, "programs.read")
    assert signed_in(reader).get("/programs/PGE_Res_HVAC/change").status_code == 404


def test_the_catalog_reads_the_whole_cedars_list_in_a_few_queries(signed_in, make_user, cpuc_admin, django_assert_max_num_queries):
    import_cedars_programs((DATA / "cedars_programs_20261003.csv").read_bytes(), actor=None)
    c = signed_in(make_user("someone@example.com"))
    with django_assert_max_num_queries(20):
        r = c.get("/programs/?state=closed")
    assert r.status_code == 200 and "595 programs" in r.content.decode()


def test_catalog_versions_match_the_registry_rules(programs):
    rows = {p.prg_id: v for p, v in registry.catalog(2020)}
    assert rows["PGE21001"] == registry.version_for_year(programs["old"], 2020)
    assert rows["PGE_Res_HVAC"] is None  # not started yet in 2020
    rows = {p.prg_id: v for p, v in registry.catalog()}
    assert rows["PGE21001"] == registry.current_version(programs["old"])
