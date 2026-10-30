import csv
import io

import pytest
from django.test import Client

from cedars.modules.calculations import dispatch, results, service
from cedars.modules.calculations.models import Run

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

rv = pytest.importorskip("recet.results_view", reason="the Results pages use the ReCET engine's results code")
pytestmark = pytest.mark.django_db


def finished(user, org, release, tmp_path, acc="2024", name="2028 claims"):  # noqa: F811
    service.create_run(user, org, request(release, acc_version=acc, name=name), job_zip(tmp_path, name=f"{name}.zip"))
    return dispatch.run_local(dispatch.claim_next("test"), poll=0.05)


@pytest.fixture
def web(analyst):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    return c


def test_results_are_read_from_the_kept_group_sums(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    run = finished(analyst, pge, release, tmp_path)
    assert run.status == Run.Status.SUCCEEDED
    names = [f["name"] for f in service.output_files(run)]
    assert "measure_level_run.csv" in names and not any(n.startswith("_results/") for n in names)  # the Results pages' files are hidden
    res = results.load(run)
    assert res.n == 6 and res.pas() == [rv.ALL_PAS, "PGE", "SCE"] and res.years() == ["2028", "2029"]
    assert res.path is None  # the measure-level parquet is fetched only for the measure level

    page = web.get(f"/calculations/runs/{run.pk}/results/")
    assert page.status_code == 200
    html = page.content.decode()
    assert "Total system benefit" in html and "$23,100" in html  # 1,100 x (1 + ... + 6)
    assert "Total system benefit by program sector" in html and "Industrial" in html

    page = web.get(f"/calculations/runs/{run.pk}/results/", {"level": "program", "pa": "SCE", "years": ["2029"]})
    html = page.content.decode()
    assert "SCE_Ind" in html and "PGE_Res_HVAC" not in html and "2029" in html

    page = web.get(f"/calculations/runs/{run.pk}/results/", {"level": "program", "sort": "TotalSystemBenefit", "desc": "1"})
    body = page.content.decode()
    assert body.index("SCE_Ind") < body.index("PGE_Res_HVAC") and 'aria-sort="descending"' in body

    page = web.get(f"/calculations/runs/{run.pk}/results/", {"level": "program", "q": "com_ltg"})
    assert "PGE_Com_Ltg" in page.content.decode() and "SCE_Ind" not in page.content.decode()

    page = web.get(f"/calculations/runs/{run.pk}/results/", {"level": "measure"})
    assert "measure 5" in page.content.decode()  # a small run shows the measure level, from its kept parquet
    assert results.load(run).path is not None


def test_results_csv_and_columns(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    run = finished(analyst, pge, release, tmp_path)
    r = web.get(f"/calculations/runs/{run.pk}/results.csv", {"level": "year", "col": ["TRC_ratio", "ElecBen"]})
    assert r.status_code == 200 and r["Content-Disposition"].endswith('_year.csv"')
    rows = list(csv.DictReader(io.StringIO(r.content.decode())))
    assert [row["Program year"] for row in rows] == ["2028", "2029"]
    assert set(rows[0]) == {"Program year", "Measures", "TRC", "Electric benefits ($)"}
    # ratios are recomputed from the summed benefits and costs, not averaged
    trc = float(rows[0]["TRC"])
    assert trc == pytest.approx((1100 * (1 + 3 + 4)) / (800 * (1 + 3 + 4)), rel=1e-9)


def test_compare_two_runs(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    a = finished(analyst, pge, release, tmp_path, acc="2024", name="on 2024")
    b = finished(analyst, pge, release, tmp_path, acc="2026", name="on 2026")
    page = web.get(f"/calculations/runs/{a.pk}/compare/")
    assert page.status_code == 200 and "Pick a run" in page.content.decode() and "on 2026" in page.content.decode()
    page = web.get(f"/calculations/runs/{a.pk}/compare/", {"with": str(b.pk), "level": "program"})
    html = page.content.decode()
    assert page.status_code == 200 and "+10.0%" in html and "SCE_Ind" in html
    r = web.get(f"/calculations/runs/{a.pk}/compare.csv", {"with": str(b.pk)})
    row = next(csv.DictReader(io.StringIO(r.content.decode())))
    assert float(row["Total system benefit ($) (difference)"]) == pytest.approx(2310.0)


def test_results_follow_run_visibility(analyst, pge, release, worker, tmp_path, make_user):  # noqa: F811
    run = finished(analyst, pge, release, tmp_path)
    outsider = Client()
    sign_in(outsider, make_user("someone@example.com"))
    assert outsider.get(f"/calculations/runs/{run.pk}/results/").status_code == 404
    assert outsider.get(f"/calculations/runs/{run.pk}/results.csv").status_code == 404


def test_no_results_yet(analyst, pge, release, tmp_path, web):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    page = web.get(f"/calculations/runs/{run.pk}/results/")
    assert page.status_code == 200 and "once the run has finished" in page.content.decode()
    assert web.get(f"/calculations/runs/{run.pk}/results.csv").status_code == 404


def test_without_the_engine_the_page_says_so(analyst, pge, release, worker, tmp_path, web, monkeypatch):  # noqa: F811
    run = finished(analyst, pge, release, tmp_path)
    monkeypatch.setattr(results, "engine", lambda: None)
    page = web.get(f"/calculations/runs/{run.pk}/results/")
    assert page.status_code == 200 and "need the ReCET engine package" in page.content.decode()
