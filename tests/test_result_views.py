"""Saved Results views, Find and Clear in the column picker, and Compare's own picker, row filter and definitions (ReCET UI parity
Q1-Q3)."""

import csv
import io

import pytest
from django.test import Client

from cedars.modules.calculations import result_views
from cedars.modules.calculations.models import ResultView
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

from .conftest import sign_in
from .test_calculation_results import finished, web  # noqa: F401 - fixtures
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

pytest.importorskip("recet.results_view", reason="the Results pages use the ReCET engine's results code")
pytestmark = pytest.mark.django_db


def test_saving_and_deleting_views(analyst, make_user):  # noqa: F811
    v = result_views.save(analyst, "  Ratios  only ", ["TRC_ratio", "PAC_ratio", "TRC_ratio"])
    assert v.name == "Ratios only" and v.columns == ["TRC_ratio", "PAC_ratio"]
    again = result_views.save(analyst, "ratios ONLY", ["ElecBen"])  # the same name, case ignored, replaces the columns
    assert again.pk == v.pk and again.columns == ["ElecBen"] and ResultView.objects.count() == 1
    actions = list(AuditEvent.objects.filter(object_type="calculations.resultview").values_list("action", flat=True).order_by("pk"))
    assert actions == ["result_view.added", "result_view.changed"]
    for name, cols in [("", ["ElecBen"]), ("x" * 61, ["ElecBen"]), ("Empty", [])]:
        with pytest.raises(AccessDenied):
            result_views.save(analyst, name, cols)
    other = make_user("other@example.org")
    assert result_views.get(other, v.pk) is None and result_views.get(analyst, "nope") is None
    with pytest.raises(AccessDenied):
        result_views.delete(other, v)
    result_views.delete(analyst, v)
    assert not ResultView.objects.exists() and AuditEvent.objects.filter(action="result_view.deleted").exists()


def test_a_view_on_results_and_the_picker(analyst, pge, release, worker, tmp_path, web, make_user):  # noqa: F811
    run = finished(analyst, pge, release, tmp_path)
    url = f"/calculations/runs/{run.pk}/results/"
    page = web.get(url, {"level": "program", "col": ["TRC_ratio", "ElecBen"]})
    html = page.content.decode()
    assert "Choose columns (2 shown)" in html and 'name="columns" value="TRC_ratio,ElecBen"' in html

    # save the columns shown, from the page's own form, and go back to the page
    back = f"{url}?level=program&col=TRC_ratio&col=ElecBen"
    r = web.post("/calculations/result-views/", {"name": "Two", "columns": "TRC_ratio,ElecBen", "next": back})
    assert r.status_code == 302 and r["Location"] == back
    view = ResultView.objects.get(owner=analyst)
    assert web.post("/calculations/result-views/", {"name": "Evil", "columns": "x", "next": "https://example.org/"})["Location"] == (
        "/calculations/runs/"
    )

    # choosing the view shows its columns, on any run, and names it
    page = web.get(url, {"level": "year", "view": view.pk}).content.decode()
    assert 'Choose columns (2 shown, view "Two")' in page and "Electric benefits" in page
    r = web.get(f"/calculations/runs/{run.pk}/results.csv", {"level": "year", "view": view.pk})
    assert set(next(csv.DictReader(io.StringIO(r.content.decode())))) == {"Program year", "Measures", "TRC", "Electric benefits ($)"}

    # Find narrows the picker over names and definitions and keeps the ticked columns it hides; Clear unticks everything
    page = web.get(url, {"columns": "TRC_ratio,ElecBen", "find": "electric"}).content.decode()
    assert 'value="ElecBen" aria-describedby' in page and 'value="TRC_ratio" aria-describedby' not in page
    assert '<input type="hidden" name="col" value="TRC_ratio">' in page and " open>" in page
    page = web.get(url, {"columns": "TRC_ratio,ElecBen", "clear": "1"}).content.decode()
    assert "checked" not in page.split('id="picker"')[1].split("</details>")[0]

    # someone else's view is not theirs to show or delete
    other = Client()
    sign_in(other, make_user("x@example.org"))
    assert other.post(f"/calculations/result-views/{view.pk}/delete", {"next": url}).status_code == 404
    r = web.post(f"/calculations/result-views/{view.pk}/delete", {"next": url})
    assert r.status_code == 302 and not ResultView.objects.filter(pk=view.pk).exists()


def test_compare_has_its_own_picker_row_filter_and_definitions(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    a = finished(analyst, pge, release, tmp_path, acc="2024", name="on 2024")
    b = finished(analyst, pge, release, tmp_path, acc="2026", name="on 2026")
    view = result_views.save(analyst, "Benefits", ["ElecBen"])
    url = f"/calculations/runs/{a.pk}/compare/"
    html = web.get(url, {"with": str(b.pk), "level": "program", "view": view.pk}).content.decode()
    assert 'Choose columns (1 shown, view "Benefits")' in html
    assert "Column definitions" in html
    html = web.get(url, {"with": str(b.pk), "level": "program", "q": "sce_ind"}).content.decode()
    assert "SCE_Ind" in html and "PGE_Res_HVAC" not in html and "of " in html
    r = web.get(f"/calculations/runs/{a.pk}/compare.csv", {"with": str(b.pk), "level": "program", "q": "sce_ind"})
    rows = list(csv.DictReader(io.StringIO(r.content.decode())))
    assert len(rows) == 1 and rows[0]["PrgID"] == "SCE_Ind"


def test_the_two_list_column_picker(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    """Choose columns as the output template page has it (Jake, 2026-11-04): every column on the left, the columns shown on the right in
    the order chosen, sent as one comma list that sets the table's column order. Shown only with scripts; the tick boxes stay without."""
    import json
    import re

    run = finished(analyst, pge, release, tmp_path)
    url = f"/calculations/runs/{run.pk}/results/"
    html = web.get(url, {"level": "program", "columns": "ElecBen,TRC_ratio"}).content.decode()
    picker = html.split('id="picker"')[1].split("</details>")[0]
    assert '<form method="get" data-with-picker hidden>' in picker and "<div data-without-picker>" in picker
    assert 'id="field-picker" data-target="pick-columns" data-sep="comma" data-noun="the table" hidden' in picker
    assert '<input type="hidden" name="columns" id="pick-columns" value="ElecBen,TRC_ratio">' in picker
    assert 'name="level" value="program"' in picker and "field_picker.js" in html
    catalog = json.loads(re.search(r'<script id="field-catalog" type="application/json">(.*?)</script>', html, re.S).group(1))
    keys = [f["c"] for f in catalog]
    assert {"ElecBen", "TRC_ratio", "TotalSystemBenefit"} <= set(keys) and len(keys) == len(set(keys))
    elec = next(f for f in catalog if f["c"] == "ElecBen")
    assert elec["t"] == "Electric benefits ($)" and elec["g"] and elec["d"]
    # the order chosen is the table's order, and the CSV's
    r = web.get(f"/calculations/runs/{run.pk}/results.csv", {"level": "program", "columns": "ElecBen,TRC_ratio"})
    assert list(next(csv.DictReader(io.StringIO(r.content.decode()))))[-2:] == ["Electric benefits ($)", "TRC"]
    # Compare gets the same picker
    html = web.get(f"/calculations/runs/{run.pk}/compare/", {"with": str(run.pk), "columns": "TRC_ratio"}).content.decode()
    assert 'data-target="pick-columns"' in html and 'id="pick-columns" value="TRC_ratio"' in html
