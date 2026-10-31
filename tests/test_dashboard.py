from datetime import timedelta

import pytest
from django.http import QueryDict
from django.utils import timezone

from cedars.modules.data import dashboard
from cedars.services.registry.models import FilingCycle
from cedars.services.submissions import service as submissions

from .test_data_query import RECORDS, publish
from .test_snapshots import claims_deadline, filer  # noqa: F401 - filer is a fixture

pytestmark = pytest.mark.django_db

OVERVIEW = dashboard.PAGES["overview"]


@pytest.fixture
def both(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    """Claims for 2028, and a forecast twice as large."""
    publish(filer, claims_deadline(cpuc_admin, 1), pge, django_capture_on_commit_callbacks, "c1")
    cycle = FilingCycle.objects.create(first_year=2028, last_year=2031)
    tual = submissions.add_deadline(cpuc_admin, module="budgets", kind="TUAL", cycle=cycle, due_at=timezone.now() - timedelta(days=1))
    submissions.set_policy(cpuc_admin, "budgets", late_confirmation=True)
    publish(filer, tual, pge, django_capture_on_commit_callbacks, "t1", scale=2.0)


def view(**params):
    return dashboard.parse(OVERVIEW, _qd(params))


def _qd(params):
    q = QueryDict(mutable=True)
    for k, v in params.items():
        q.setlist(k, v if isinstance(v, list) else [v])
    return q


def test_defaults(both, monkeypatch):
    v = view()
    assert v.series == ("forecast", "claims") and v.by == "pa" and v.metric.code == "tsb"
    assert v.query.exclude == ("pension", "cs")
    monkeypatch.setattr(dashboard, "current_cycle", lambda: FilingCycle.objects.get())
    assert (view().query.year_from, view().query.year_to) == (2028, 2031)  # the current cycle
    assert view(year_from="2029").query.year_from == 2029


def test_forecast_and_claims_side_by_side(both):
    c = dashboard.compare(view(by="sector"))
    assert c.both and [r.key for r in c.rows] == ["Residential", "Commercial"]  # ordered by the claimed value
    res = c.rows[0]
    assert res.values == {"forecast": 300, "claims": 150} and res.difference == -150 and res.percent == 50
    assert c.totals.values == {"forecast": 360, "claims": 180} and c.totals.percent == 50
    assert res.drill == "?by=program&sector=Residential"
    assert "as of" in c.series_label("claims")
    [f, cl] = c.downloads()
    assert f["series"] == "Forecast" and "dataset=forecast" in f["links"][0][1] and "by=sector" in f["links"][0][1]
    assert "dataset=claims" in cl["links"][0][1]


def test_one_series_alone(both):
    c = dashboard.compare(view(series="claims"))
    assert not c.both and list(c.results) == ["claims"] and c.totals.values == {"claims": 180}
    assert c.totals.difference is None


def test_drill_goes_one_level_down_and_filters_come_off(both):
    v = view(by="program", sector="Residential", metric="kwh")
    c = dashboard.compare(v)
    assert [r.key for r in c.rows] == ["PGE_Res_HVAC"] and c.rows[0].values["claims"] == 3.0  # MWh
    assert c.rows[0].drill == "?by=measure&metric=kwh&sector=Residential&program=PGE_Res_HVAC"
    [chip] = dashboard.applied_filters(v)
    assert chip["label"] == "Primary sector" and chip["remove"] == "?by=program&metric=kwh"
    assert dashboard.compare(view(by="measure", program="PGE_Res_HVAC")).rows[0].drill == ""


def test_typed_filters_split_on_commas(both):
    v = view(program="PGE_Res_HVAC, PGE_Com_Lt")
    assert v.query.filter_map["program"] == ("PGE_Res_HVAC", "PGE_Com_Lt")


def test_bad_parameters_are_refused(both):
    for bad in ({"by": "colour"}, {"metric": "trc"}, {"year_from": "soon"}):
        with pytest.raises(dashboard.dq.QueryError):
            view(**bad)


def test_chart_geometry(both):
    c = dashboard.compare(view(by="sector"))
    g = dashboard.chart(c)
    assert [s["code"] for s in g["series"]] == ["forecast", "claims"] and not g["negative"]
    [res, com] = g["groups"]
    assert res["bars"][0] == {"series": "forecast", "x": 0.0, "w": 100.0, "y": 0, "missing": False}
    assert res["bars"][1]["w"] == 50.0 and res["bars"][1]["y"] == 14
    rows = [dashboard.Row("a", {"claims": -50}), dashboard.Row("b", {"claims": 150})]
    neg = dashboard.chart(dashboard.Comparison(c.view, {"claims": None}, rows, dashboard.Row("Total", {})))
    assert neg["negative"] and neg["zero"] == 25.0
    assert neg["groups"][0]["bars"][0]["x"] == 0.0 and neg["groups"][0]["bars"][0]["w"] == 25.0


def test_the_page(both, client):
    page = client.get("/data/overview/?by=sector").content.decode()
    assert "Portfolio dashboard pages" in page and "Forecast (as of" in page and "Claimed (as of" in page
    assert "% of forecast" in page and "50.0%" in page and 'href="?by=program&amp;sector=Residential"' in page
    assert "Left out: Codes and standards." in page and "/data/download.csv?dataset=claims" in page
    assert client.get("/data/").status_code == 302
    assert client.get("/data/nope/").status_code == 404
    bad = client.get("/data/overview/?by=colour")
    assert bad.status_code == 200 and "could not be shown" in bad.content.decode()


def test_the_page_before_anything_is_published(client, db):
    page = client.get("/data/overview/").content.decode()
    assert "Nothing has been published in this dataset yet." in page and "nothing published" in page


def test_nothing_hidden_in_the_rows(both):
    """The default exclusions are stated, and including them brings the rows back."""
    assert dashboard.compare(view(cs="include", pension="include")).totals.values["claims"] == sum(r[7] for r in RECORDS)


# --- page 1: goals; page 3: total system benefit


@pytest.fixture
def goal_set(cpuc_admin, make_user):
    from datetime import date

    from cedars.services.accounts import permissions as perms
    from cedars.services.accounts import service as accounts
    from cedars.services.registry import goals

    second = make_user("second@cpuc.ca.gov", "Sam Second")
    accounts.grant(cpuc_admin, second, accounts.cpuc(), perms.SYSTEM_ADMIN, reason="second administrator")
    header = "Utility,Kind,FirstYear,LastYear,ProgramTSB,ProgramGWh,ProgramMW,ProgramMMTherms,CSGWh,CSMW,CSMMTherms\n"
    content = (
        header + "PGE,annual,2028,2028,600,1,1,1,0.018,1,1\nPGE,cycle,2028,2031,2000,1,1,1,0.05,1,1\nSCE,annual,2028,2028,100,1,1,,1,1,\n"
    )
    s = goals.import_goals(
        cpuc_admin,
        content.encode(),
        decision="D.00-00-009",
        adopted=date(2026, 1, 1),
        rules=b"PA,Territory,Share,AppliesTo\nMCE,PGE,0.5,programs\n",
    )
    return goals.confirm(second, s)


def goals_view(**params):
    return dashboard.parse(dashboard.PAGES["goals"], _qd(params))


def test_goals_by_year(both, goal_set):
    from cedars.modules.data import goal_view

    c = goal_view.compare(goals_view())
    by = {r.key: r for r in c.rows}
    assert set(by) == {"PGE 2028", "SCE 2028"}
    pge = by["PGE 2028"]
    assert pge.goal == 600 and pge.values == {"forecast": 360.0, "claims": 180.0}  # TSB without C&S, ESA or pension
    assert pge.percent("claims") == 30 and pge.percent("forecast") == 60
    assert by["SCE 2028"].values == {"forecast": None, "claims": None} and by["SCE 2028"].goal == 100
    assert "pa=MCE&pa=PGE" in pge.drill and "year_from=2028" in pge.drill and pge.drill.startswith("/data/overview/?")
    assert c.shares["MCE"] == [("PGE", 0.5)]


def test_goals_by_cycle_and_codes_and_standards(both, goal_set):
    from cedars.modules.data import goal_view

    [pge] = goal_view.compare(goals_view(), "cycle").rows
    assert (pge.period, pge.goal, pge.values["claims"]) == ("2028-2031", 2000, 180.0)
    cs = {r.key: r for r in goal_view.compare(goals_view(metric="cs_gwh")).rows}
    assert cs["PGE 2028"].values["claims"] == pytest.approx(0.009)  # 9,000 kWh of C&S = 0.009 GWh
    assert cs["PGE 2028"].percent("claims") == pytest.approx(50)
    assert "segment=codes_and_standards" in cs["PGE 2028"].drill and "cs=include" in cs["PGE 2028"].drill


def test_counting_rules_share_a_cca_toward_its_territory(goal_set):
    from cedars.modules.data import goal_view
    from cedars.services.registry import goals

    shares = goal_view.shares(goals.counting_rules(), "programs")
    assert shares["MCE"] == [("PGE", 0.5)] and shares["PGE"] == [("PGE", 1.0)]
    assert "MCE" not in goal_view.shares(goals.counting_rules(), "cs")


def test_the_goals_page(both, goal_set, client):
    page = client.get("/data/goals/").content.decode()
    assert "against the goal, by year" in page and "MCE toward PGE at 0.5" in page and "30.0%" in page
    assert 'name="cs"' not in page  # no C&S toggle on the goals page: each metric sets it
    assert "against the goal, by cycle" in client.get("/data/goals/?period=cycle").content.decode()
    assert "No goals or results" in client.get("/data/goals/?year_from=2030&year_to=2030").content.decode()


def test_the_total_system_benefit_page(both, client):
    from cedars.modules.data import dashboard as d

    view = d.parse(d.PAGES["total-system-benefit"], _qd({}))
    m = d.matrix(view, "pa", "sector")
    assert m.series == "claims" and m.columns == ["Commercial", "Residential"]
    [pge] = m.rows
    assert pge["total"] == 180 and [c["share"] for c in pge["cells"]] == pytest.approx([100 / 6, 500 / 6])
    years = d.compare(view, "year")
    assert [r.key for r in years.rows] == [2028] and years.rows[0].drill == ""
    page = client.get("/data/total-system-benefit/").content.decode()
    for caption in ("by primary sector", "by year", "share of each PA", "by program"):
        assert caption in page
    assert 'href="/data/overview/?sector=Residential&amp;by=program&amp;metric=tsb"' in page
    assert "View by" not in page


def test_the_cost_effectiveness_page(both, client):
    from cedars.modules.data import dashboard as d

    page = d.PAGES["cost-effectiveness"]
    assert page.metrics[:2] == ("trc", "trc_no_admin") and len(page.metrics) == 10
    c = d.compare(d.parse(page, _qd({})), "sector")
    assert {r.key: r.values["claims"] for r in c.rows} == {"Residential": 1.0, "Commercial": 2.0}
    assert c.totals.values["claims"] == pytest.approx(240 / 220)  # from sums, not the mean of 1.0 and 2.0
    html = client.get("/data/cost-effectiveness/?metric=pac").content.decode()
    assert "PAC ratio" in html and "% of forecast" not in html and "Claimed less forecast" in html  # no % of a ratio
    assert client.get("/data/cost-effectiveness/?metric=tsb").status_code == 200  # refused in words, not an error page


def test_the_savings_and_environment_pages(both, client):
    from cedars.modules.data import dashboard as d

    view = d.parse(d.PAGES["energy-savings"], _qd({"metric": "kwh", "lifecycle": "1", "gross": "1"}))
    assert view.metric.code == "kwh_lifecycle_gross"
    assert d.parse(d.PAGES["energy-savings"], _qd({"metric": "kw", "lifecycle": "1"})).metric.code == "kw_first_year_net"
    assert d.parse(d.PAGES["environmental-impact"], _qd({"metric": "nox"})).metric.code == "nox_first_year_net"
    html = client.get("/data/energy-savings/?metric=therms").content.decode()
    assert "Gas savings, first year net (MMTherms) by primary sector" in html
    assert "(metric tons) by year" in client.get("/data/environmental-impact/").content.decode()


def test_program_and_measure_pages_drill_to_the_records(both, client):
    from cedars.modules.data import dashboard as d

    programs = d.compare(d.parse(d.PAGES["programs"], _qd({})), "program", ("measures", "use_category"))
    hvac = programs.rows[0]
    assert hvac.key == "PGE_Res_HVAC" and hvac.drill == "/data/measures/?program=PGE_Res_HVAC&by=use_category&metric=tsb"
    assert hvac.links == [("Program page", "/programs/PGE_Res_HVAC/")]
    measures = d.compare(d.parse(d.PAGES["measures"], _qd({"program": "PGE_Res_HVAC"})), "measure")
    assert [r.key for r in measures.rows] == [None]  # the test file has no MeasureID column
    html = client.get("/data/programs/").content.decode()
    assert "Program page" in html and 'href="/data/measures/?program=PGE_Res_HVAC&amp;by=use_category&amp;metric=tsb"' in html


def test_records_sum_to_the_row_they_came_from(both, client):
    from cedars.modules.data import query as dq

    q = dq.Query("claims", ("tsb", "kwh_first_year_net"), filters=(("program", ("PGE_Res_HVAC",)),))
    recs = dq.records(q, limit=1)
    assert recs.count == 2 and len(recs.rows) == 1 and recs.rows[0]["CEInputID"] == "M0"
    assert recs.result.totals["tsb"] == 150 == dq.run(q.but(by=("program",))).rows[0]["tsb"]
    assert dq.records(q, limit=1, offset=1).rows[0]["tsb"] == 50
    page = client.get("/data/records/?dataset=claims&metric=tsb&program=PGE_Res_HVAC").content.decode()
    assert "Claimed records" in page and "M0" in page and "M1" in page and "Total of all 2" in page and "150" in page
    forecast = client.get("/data/records/?dataset=forecast&metric=tsb&program=PGE_Res_HVAC").content.decode()
    assert "Forecast records" in forecast and "300" in forecast
    assert "could not be shown" in client.get("/data/records/?metric=nope").content.decode()
    assert "No records match." in client.get("/data/records/?metric=tsb&program=NOPE").content.decode()


def test_records_page_through(both, client, monkeypatch):
    from cedars.modules.data import views

    monkeypatch.setattr(views, "RECORDS_PER_PAGE", 1)
    first = client.get("/data/records/?metric=tsb").content.decode()
    assert "Records 1 to 1 of 3" in first and "page=2" in first and "Previous" not in first
    last = client.get("/data/records/?metric=tsb&page=3").content.decode()
    assert "Records 3 to 3 of 3" in last and "Next" not in last
