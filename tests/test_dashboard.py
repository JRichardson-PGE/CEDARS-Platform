from datetime import timedelta

import pytest
from django.http import QueryDict
from django.utils import timezone

from cedars.modules.data import dashboard
from cedars.services.registry.models import FilingCycle
from cedars.services.submissions import service as submissions

from .conftest import page_only
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
    [both_series] = c.downloads()  # one file for both series (Jake, 2026-11-02)
    href = both_series["links"][0][1]
    assert both_series["series"] == "Forecast and claimed" and "dataset=forecast&dataset=claims" in href and "by=sector" in href
    [claims_only] = dashboard.compare(view(by="sector", series="claims")).downloads()
    assert (
        claims_only["series"] == "Claimed"
        and "dataset=claims" in claims_only["links"][0][1]
        and "forecast" not in claims_only["links"][0][1]
    )


def test_download_these_rows_holds_both_series(both, client):
    """With forecast and claimed both shown, "Download these rows" is one file: a Series column first, each series' rows and total,
    each series' stamp; the records behind the rows stay one download per series."""
    page = client.get("/data/overview/?by=sector").content.decode()
    assert "Forecast and claimed: <a" in page and page.count("/data/records/download.csv?") == 2
    [d] = dashboard.compare(view(by="sector")).downloads()
    response = client.get(d["links"][0][1])
    assert response.status_code == 200 and "cedars_forecast_claims_release" in response["Content-Disposition"]
    text = response.content.decode()
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines[0].startswith("Series,Primary sector,")
    rows = [line.split(",") for line in lines[1:]]
    assert [r[:2] for r in rows] == [
        ["Forecast", "Commercial"],
        ["Forecast", "Residential"],
        ["Forecast", "Total"],
        ["Claimed", "Commercial"],
        ["Claimed", "Residential"],
        ["Claimed", "Total"],
    ]
    assert "# Forecast release:" in text and "# Claimed release:" in text
    for fmt in ("xlsx", "parquet"):
        assert client.get(d["links"][0][1].replace("download.csv", f"download.{fmt}")).status_code == 200
    import io

    import pyarrow.parquet as pq

    t = pq.read_table(io.BytesIO(client.get(d["links"][2][1]).content))
    assert t.column_names[0] == "Series" and set(t.column("Series").to_pylist()) == {"Forecast", "Claimed"}


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
    assert res["bars"][0] == {"series": "forecast", "x": 0.0, "w": 100.0, "y": 0, "missing": False, "value": 300.0}
    assert res["bars"][1]["w"] == 50.0 and res["bars"][1]["y"] == 14
    rows = [dashboard.Row("a", {"claims": -50}), dashboard.Row("b", {"claims": 150})]
    neg = dashboard.chart(dashboard.Comparison(c.view, {"claims": None}, rows, dashboard.Row("Total", {})))
    assert neg["negative"] and neg["zero"] == 25.0
    assert neg["groups"][0]["bars"][0]["x"] == 0.0 and neg["groups"][0]["bars"][0]["w"] == 25.0


def test_chart_values_in_words(both):
    """Each chart row's values as the pointer and keyboard show them and a screen reader reads them: every series, "none" where a
    series has no value (blank is not zero), and the marker (goal, operating budget) when the row has one."""
    c = dashboard.compare(view(by="sector"))
    g = dashboard.describe(dashboard.chart(c), lambda v: f"{v:,.0f} kWh")
    assert g["groups"][0]["tip"] == [("forecast", "Forecast", "300 kWh"), ("claims", "Claimed", "150 kWh")]
    rows = [dashboard.Row("a", {"claims": 5})]
    one = dashboard.chart(dashboard.Comparison(c.view, {"forecast": None, "claims": None}, rows, dashboard.Row("Total", {})))
    assert dashboard.describe(one, str)["groups"][0]["tip"] == [("forecast", "Forecast", "none"), ("claims", "Claimed", "5")]
    marked = {"series": one["series"], "goal": "Goal", "groups": [{"bars": one["groups"][0]["bars"], "goal_value": 7}]}
    assert dashboard.describe(marked, str)["groups"][0]["tip"][-1] == ("goal", "Goal", "7")


def test_charts_and_sections_on_the_page(both, client):
    """Hover values and folding sections (Jake 2026-11-01): without scripts the chart stays one image with the table as its text
    alternative and every section is open; each row carries its values for dashboard.js to show, and each section a key to remember."""
    page = client.get("/data/overview/?by=sector").content.decode()
    assert 'role="img"' in page and "data-chart" in page and "data/dashboard.js" in page
    assert '<span class="chart-tip sr-only"><strong class="tip-label">Residential</strong>' in page
    for key in ('data-part="filters"', 'data-part="overview:s1:chart"', 'data-part="overview:s1:table"'):
        assert key in page
    assert page.count('<details class="section dash-part" open') == 3
    assert "data-keep-open" not in page
    assert "data-keep-open" in client.get("/data/overview/?by=colour").content.decode()


def test_the_page(both, client):
    page = client.get("/data/overview/?by=sector").content.decode()
    assert "Portfolio dashboard pages" in page and "Forecast data as of " in page and "; Claimed data as of " in page
    assert "(as of" not in page  # said once above the tiles, not in every tile and header (UI review 2, #19)
    assert "% of forecast" in page and "50.0%" in page and 'href="?by=program&amp;sector=Residential"' in page
    assert (
        "Left out: Pension and Benefits programs and codes and standards." in page
        and "/data/download.csv?dataset=forecast&amp;dataset=claims" in page
    )
    assert client.get("/data/").status_code == 302
    assert client.get("/data/nope/").status_code == 404
    bad = client.get("/data/overview/?by=colour")
    assert bad.status_code == 200 and "could not be shown" in bad.content.decode()


def test_the_page_before_anything_is_published(client, db):
    page = client.get("/data/overview/").content.decode()
    assert "Nothing has been published in this dataset yet." in page and "Claimed data not published yet" in page


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

    second = make_user("second@cpuc.example.org", "Sam Second")
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
    assert {r.key: r.values["claims"] for r in c.rows} == {"Residential": 1.0}  # PGE_Com_Lt is flagged excluded from CE
    c = d.compare(d.parse(page, _qd({"no_ce": "include"})), "sector")
    assert {r.key: r.values["claims"] for r in c.rows} == {"Residential": 1.0, "Commercial": 2.0}
    assert c.totals.values["claims"] == pytest.approx(240 / 220)  # from sums, not the mean of 1.0 and 2.0
    assert view().query.exclude == ("pension", "cs")  # other pages count them
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


def test_program_and_measure_pages_drill_to_the_records(both, client, pge, pge_admin):
    from datetime import date

    from cedars.modules.data import dashboard as d
    from cedars.services.registry import service as registry

    programs = d.compare(d.parse(d.PAGES["programs"], _qd({})), "program", ("measures", "use_category"))
    assert programs.rows[0].links == []  # not in the program registry, so no program page to link (UAT 2, U06)
    registry.create_program(pge_admin, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1), name="Residential HVAC")
    programs = d.compare(d.parse(d.PAGES["programs"], _qd({})), "program", ("measures", "use_category"))
    hvac = programs.rows[0]
    assert hvac.key == "PGE_Res_HVAC" and hvac.drill == "/data/measures/?program=PGE_Res_HVAC&by=use_category&metric=tsb"
    assert hvac.links == [("Program page", "/programs/PGE_Res_HVAC/")]
    measures = d.compare(d.parse(d.PAGES["measures"], _qd({"program": "PGE_Res_HVAC"})), "measure")
    assert [r.key for r in measures.rows] == [None]  # the test file has no MeasureID column
    html = client.get("/data/programs/").content.decode()
    assert "Program page" in html and 'href="/data/measures/?program=PGE_Res_HVAC&amp;by=use_category&amp;metric=tsb"' in html


def test_records_sum_to_the_row_they_came_from(both, client):
    from cedars.modules.data import records

    params = {"dataset": ["claims"], "metric": ["tsb"], "program": ["PGE_Res_HVAC"], "cs": ["exclude"], "pension": ["exclude"]}
    p = records.page(records.from_params(params), limit=1, offset=1)
    assert p.count == 2 and p.rows[0][p.columns.index("CEInputID")] == "M1" and p.result.totals["tsb"] == 150
    page = client.get("/data/records/?dataset=claims&metric=tsb&program=PGE_Res_HVAC").content.decode()
    assert "Claimed records" in page and "M0" in page and "M1" in page and "Total system benefit ($): 150" in page
    forecast = client.get("/data/records/?dataset=forecast&metric=tsb&program=PGE_Res_HVAC").content.decode()
    assert "Forecast records" in forecast and "300" in forecast


def test_records_page_through(both, client, monkeypatch):
    from cedars.modules.data import views

    monkeypatch.setattr(views, "RECORDS_PER_PAGE", 1)
    first = client.get("/data/records/?metric=tsb&cs=exclude&pension=exclude").content.decode()
    assert "Records 1 to 1 of 3" in first and "page=2" in first and "Previous" not in first
    last = client.get("/data/records/?metric=tsb&cs=exclude&pension=exclude&page=3").content.decode()
    assert "Records 3 to 3 of 3" in last and "Next" not in page_only(last)
    assert "Records 1 to 1 of 5" in client.get("/data/records/?metric=tsb").content.decode()  # records leave nothing out unless asked


def test_budget_and_spending(both, pge, pge_admin, monkeypatch, client):
    from datetime import date
    from decimal import Decimal

    from cedars.modules.data import budget_view
    from cedars.services.registry import service as registry

    def budget(org, cycle):
        assert (cycle.first_year, cycle.last_year) == (2028, 2031)
        return [
            {"prg_id": "PGE_Res_HVAC", "year": 2028, "operating": Decimal(200)},
            {"prg_id": "PGE_Com_Lt", "year": 2028, "operating": Decimal(50)},
            {"prg_id": "PGE_Res_HVAC", "year": 2029, "operating": Decimal(250)},
        ]

    def spend(org, year):
        return [{"prg_id": "PGE_Res_HVAC", "ytd": Decimal(60), "through": "2028-06"}] if year == 2028 else []

    monkeypatch.setattr(registry, "_operating_budget_sources", [budget])
    monkeypatch.setattr(registry, "_reported_spend_sources", [spend])
    registry.create_program(pge_admin, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1), name="Residential HVAC")
    view = d_parse("budget", year_from="2028", year_to="2029", pa="PGE")
    s = budget_view.compare(view)
    assert s.organisations == ["PGE"]
    y28, y29 = s.by_year
    assert (y28.budget, y28.monthly, y28.values) == (250, 60, {"forecast": 220.0, "claims": 110.0})  # pension and C&S left out
    assert y28.claimed_share == 44 and y28.through == "PGE 2028-06"
    assert (y29.budget, y29.values["claims"], y29.cumulative_budget, y29.cumulative_claims) == (250, None, 500, 110)
    hvac = next(r for r in s.by_program if r.key == "PGE_Res_HVAC")
    assert (hvac.budget, hvac.monthly, hvac.values["claims"], hvac.monthly_share) == (450, 60, 100.0, 60 / 450 * 100)
    # a PrgID not in the registry has no program page to link (UAT 2, U06)
    assert hvac.drill == "/programs/PGE_Res_HVAC/" and next(r for r in s.by_program if r.key == "PGE_Com_Lt").drill == ""
    by_sector = {r["sector"]: r for r in s.elements.rows}
    assert by_sector["Residential"]["incentives"] == 50 and by_sector["Residential"]["expenditure"] == 100
    g = budget_view.chart(s)
    assert g["goal"] == "Operating budget" and g["groups"][0]["goal"] == 100.0
    page = client.get("/data/budget/?year_from=2028&year_to=2029&pa=PGE").content.decode()
    assert "Monthly reported (unvalidated)" in page and "through PGE 2028-06" in page and "Cost elements by primary sector, claimed" in page
    assert "not yet validated" in page
    assert "PGE_Com_Lt <span" in page and "(not in the program registry)" in page and "/programs/PGE_Com_Lt/" not in page


def test_budget_page_without_budgets(both, client):
    page = client.get("/data/budget/?year_from=2040&year_to=2040").content.decode()
    assert "No filing cycle covers these years" in page


def d_parse(slug, **params):
    return dashboard.parse(dashboard.PAGES[slug], _qd(params))


def test_monthly_reports_give_their_spending_to_the_registry(pge):
    from cedars.services.registry import service as registry

    assert registry.reported_spend(pge, 2028) == []  # the Monthly reports module's source, with nothing published


def test_the_default_years_follow_the_forecast_when_the_current_cycle_has_none(both, monkeypatch):
    """F09: a page opens on years that hold a forecast, not on a current cycle nothing has been filed for."""
    old = FilingCycle.objects.create(first_year=2024, last_year=2027)
    monkeypatch.setattr(dashboard, "current_cycle", lambda today=None: old)
    assert (view().query.year_from, view().query.year_to) == (2028, 2031)
    assert dashboard.default_cycle().first_year == 2028


def test_percent_of_forecast_covers_only_the_years_both_series_have(both, filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    """F10: claims for 2028 and 2029 against a forecast for 2028 alone compare 2028 with 2028, and the page says so."""
    later = [r[:5] + ("2029",) + r[6:] for r in RECORDS]
    publish(filer, claims_deadline(cpuc_admin, 1, year=2029), pge, django_capture_on_commit_callbacks, "c2", records=later)
    c = dashboard.compare(view(year_from="2028", year_to="2031"))
    assert c.totals.values == {"forecast": 360, "claims": 360}  # 2028 and 2029 claims against the 2028 forecast
    assert c.totals.percent == 50 and c.shared_years == [2028] and c.series_years == {"claims": [2028, 2029], "forecast": [2028]}
    assert c.percent_note == "% of forecast compares the years both series cover, 2028 (claims 2028-2029, forecast 2028)."
    assert c.rows[0].values["claims"] == 2 * c.rows[0].shared["claims"] and c.rows[0].percent == 50
    none = dashboard.compare(view(year_from="2029", year_to="2031"))
    assert none.totals.percent is None and none.percent_note.startswith("% of forecast is blank: no year has both")
    by_year = dashboard.compare(view(), by="year")
    assert by_year.shared_years is None and [r.key for r in by_year.rows] == [2028, 2029]  # by year, each year stands alone


def test_registry_codes_read_as_words():
    assert dashboard.code_label("codes_and_standards") == "Codes and standards"
    assert dashboard.code_label("Residential") == "Residential" and dashboard.code_label(None) is None
