"""The budget filing and monthly report templates (navigation review D2, Jake 2026-11-04): one workbook each, as the claims template
is, with a Fields sheet defining every column; linked from each index page, each upload form and the data dictionary."""

import io
import json

import pytest
from openpyxl import load_workbook

from cedars.modules.budgets import jobfile
from cedars.modules.monthly import upload as up
from cedars.modules.monthly.models import COMMITMENT_FIELDS
from cedars.services.dictionary import service as dictionary

from .conftest import page_only
from .test_search import CATALOG

pytestmark = pytest.mark.django_db


def _book(client, url):
    r = client.get(url)  # public, signed out
    assert r.status_code == 200 and r["Content-Type"].startswith("application/vnd.openxmlformats")
    data = b"".join(r.streaming_content) if r.streaming else r.content
    return data, load_workbook(io.BytesIO(data), read_only=True)


def _header(wb, sheet):
    return [c for c in next(wb[sheet].iter_rows(values_only=True))]


@pytest.fixture
def catalog():
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))


def test_the_budget_filing_template(client, catalog):
    data, wb = _book(client, "/budgets/template.xlsx")
    assert wb.sheetnames == ["Fields", "Measure", "ProgramCost"]
    for t in jobfile.TABLES:
        cols = _header(wb, t)
        assert cols == [f.name for f, _ in dictionary.table_fields("calculations", t)]
        assert set(jobfile.REQUIRED[t]) <= set(cols)
    fields = list(wb["Fields"].iter_rows(values_only=True))
    assert fields[0][:3] == ("Table", "Field", "Required") and all(r[5] for r in fields[1:] if r[1] == "NumUnits")
    # the template as it stands is a budget filing's two tables (the Fields sheet is not read)
    assert set(jobfile.read_tables(data, "budget_filing_template.xlsx")) == set(jobfile.TABLES)


def test_the_monthly_report_template(client, catalog):
    data, wb = _book(client, "/monthly/template.xlsx")
    assert wb.sheetnames == ["Fields", "Commitments", "Measure", "ProgramCost"]
    assert _header(wb, "Commitments") == ["PA", "PrgID", "YearMonth"] + [f for f, _ in COMMITMENT_FIELDS] + ["Comments"]
    assert set(up.REQUIRED["Commitments"]) <= set(_header(wb, "Commitments"))
    for t in up.BCA_TABLES:
        assert set(up.REQUIRED[t]) <= set(_header(wb, t))


def test_without_a_catalog_each_table_has_its_required_columns(client):
    _, wb = _book(client, "/budgets/template.xlsx")
    assert _header(wb, "Measure") == jobfile.REQUIRED["Measure"]


def test_the_templates_are_linked_where_they_are_needed(client, signed_in, pge_admin, catalog):
    c = signed_in(pge_admin)
    assert 'href="/budgets/template.xlsx"' in page_only(c.get("/budgets/").content.decode())
    assert 'href="/monthly/template.xlsx"' in page_only(c.get("/monthly/").content.decode())
    page = page_only(client.get("/dictionary/").content.decode())
    for url in ("/budgets/template.xlsx", "/claims/template.xlsx", "/monthly/template.xlsx"):
        assert f'href="{url}"' in page
    for guide, url in (("budgets", "/budgets/template.xlsx"), ("monthly-reports", "/monthly/template.xlsx")):
        assert f'href="{url}"' in client.get(f"/help/{guide}/").content.decode()
