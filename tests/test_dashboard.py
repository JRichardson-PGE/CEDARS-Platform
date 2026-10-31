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
