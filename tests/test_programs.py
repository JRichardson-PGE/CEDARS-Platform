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

from .conftest import page_only

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
    assert "change requests" not in page_only(page.content.decode())
    assert client.get("/programs/programs.csv").status_code == 200
    html = client.get("/programs/PGE21001/").content.decode()
    assert "PGE21001" in html and "Versions" in html
    assert "@" not in html.split('id="ver-h"')[1].split("</section>")[0]  # nobody's email for the public
    assert "Change this program" not in page_only(html)
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
    assert "Change this program" not in page_only(html)  # not PG&amp;E's admin
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


def test_other_filters_keep_programs_on_any_attribute(signed_in, make_user, cpuc_admin):
    """The Other filters panel: choices (any of the values ticked), contains-text, year ranges and yes/no flags, all applied together,
    the same in the CSV download."""
    import_cedars_programs((DATA / "cedars_programs_20261003.csv").read_bytes(), actor=None)
    c = signed_in(make_user("someone@example.com"))
    every = {p.prg_id: v for p, v in registry.catalog() if v is not None}

    def kept(query):
        r = c.get(f"/programs/programs.csv?{query}")
        return {row["PrgID"] for row in csv.DictReader(io.StringIO(r.content.decode()))}

    assert kept("implementer=REN/CCA") == {k for k, v in every.items() if v.implementer == "REN/CCA"}
    both = kept("implementer=REN/CCA&implementer=Non-Program")
    assert both == {k for k, v in every.items() if v.implementer in ("REN/CCA", "Non-Program")}
    assert kept("primary_sector=Industrial&upstream=yes") == {
        k for k, v in every.items() if v.primary_sector == "Industrial" and v.flags.get("Upstream_Flag") == "True"
    }
    assert kept("deemed=no&pa=PGE") == {k for k in kept("pa=PGE") if every[k].flags.get("Deemed_Flag") != "True"}
    assert kept("start_year_from=2024&start_year_to=2025") == {k for k, v in every.items() if v.start_year in (2024, 2025)}
    assert kept("start_year_from=2025&start_year_to=2024") == kept("start_year_from=2024&start_year_to=2025")  # ends swapped
    open_ended = kept("end_year_from=2030")
    assert open_ended == {k for k, v in every.items() if v.end_year is None or v.end_year >= 2030}
    assert kept("end_year_to=2020") == {k for k, v in every.items() if v.end_year is not None and v.end_year <= 2020}
    assert kept("parent_program=third party") == {k for k, v in every.items() if "third party" in v.parent_program.lower()}
    assert kept("bp_sector=Industrial") == {k for k, v in every.items() if "Industrial" in v.sectors}
    assert kept("exclude_budget=yes") == {k for k, v in every.items() if v.exclude_from_budget}
    assert kept("financing=OBF&status=closed") == {
        k for k, v in every.items() if v.flags.get("Financing") == "OBF" and v.status == "closed"
    }
    assert kept("start_year_from=20x6") == set(every)  # unreadable: left out, with a note on the page
    assert "is not a year" in c.get("/programs/?start_year_from=20x6").content.decode()


def test_other_filters_panel_chips_and_links(client, programs):
    """Every filter is in the link; applied ones show as chips, each with a link that removes it; the panel opens on its field without
    a script ("Show its options"), and its options are the values the main filters keep."""
    html = client.get("/programs/?pa=PGE&status=active&status=closed&start_year_from=2024&field=status&pick=1").content.decode()
    assert '<details class="other-filters" open>' in html and "(2 applied)" in html
    assert '<div class="other-field" data-field="status">' in html  # the chosen field is shown
    assert '<div class="other-field" data-field="bp_sector" hidden>' in html
    assert 'name="status" value="active" checked' in html and 'name="status" value="closed" checked' in html
    assert 'name="bp_sector" value="Residential"' in html  # PG&E's HVAC program
    assert "PA: <strong>PGE</strong>" in html and "Status: <strong>Closed</strong>" in html
    assert "Start year: <strong>2024 or later</strong>" in html
    # Removing one value of a choice keeps the other; the page's own parameters (field, pick, page) never reach a link.
    assert 'href="?pa=PGE&amp;status=active&amp;start_year_from=2024"' in html
    assert 'href="?status=active&amp;status=closed&amp;start_year_from=2024"' in html
    assert "programs.csv?pa=PGE&amp;status=active&amp;status=closed&amp;start_year_from=2024" in html
    assert "1 program." in html and "PGE_Res_HVAC" in html  # Old Lighting is closed but started before 2024
    plain = client.get("/programs/").content.decode()
    assert '<details class="other-filters">' in plain and "applied)" not in plain and "Clear all filters" not in plain


def test_other_filter_parsing():
    from django.http import QueryDict

    from cedars.modules.programs import filters

    active, notes = filters.parse(
        QueryDict("status=active&status=&status=active&resource=YES&end_year_to=2031&commercial_name=+ x +&deemed=maybe")
    )
    assert active == {"status": ["active"], "commercial_name": "x", "end_year": (None, 2031), "resource": True}
    assert notes == ["Deemed: “maybe” is not yes or no, so that filter was left out."]
    assert filters.clean_query(QueryDict("q=&pa=PGE&page=2&field=status&pick=1&status=new")).urlencode() == "pa=PGE&status=new"
    assert {f.code for f in filters.FIELDS}.isdisjoint({"q", "pa", "state", "segment", "year", *filters.PAGE_PARAMS})
