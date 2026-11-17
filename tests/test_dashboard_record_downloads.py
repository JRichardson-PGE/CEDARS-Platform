"""The record-level download beside each dashboard series' "download these rows": the same query, one record per row, made while
the person waits up to DATA_DOWNLOAD_MAX_ROWS records and in the background beyond that."""

import csv
import html
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from cedars.modules.data.models import DownloadJob

from .test_dashboard import both, goal_set  # noqa: F401 - fixtures
from .test_snapshots import filer  # noqa: F401 - filer is a fixture

pytestmark = pytest.mark.django_db
with_records = pytest.mark.usefixtures("both")


def section(page: str) -> str:
    start = page.index('id="record-downloads"')
    return page[start : page.index("</ul>", start)]


def links(page: str, fmt: str = "csv") -> list[str]:
    return [html.unescape(h) for h in re.findall(rf'href="(/data/records/download\.{fmt}\?[^"]+)"', section(page))]


def records_csv(client, href: str) -> list[dict]:
    text = b"".join(client.get(href).streaming_content).decode()
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("# ")))


@with_records
def test_each_series_has_its_records_beside_its_rows(client):
    page = client.get("/data/overview/?by=sector").content.decode()
    part = section(page)
    assert "Download the records behind these rows" in page and "Forecast, 3 records" in part and "Claimed, 3 records" in part
    assert "Excel (XLSX)" in part and "Parquet" in part and "Choose columns" in part
    forecast, claims = links(page)
    p = parse_qs(urlsplit(claims).query)
    assert p["dataset"] == ["claims"] and "by" not in p
    assert p["cs"] == ["exclude"] and p["pension"] == ["exclude"]  # exclusions stated: records leave nothing out unless asked
    rows = records_csv(client, claims)
    assert len(rows) == 3 and sum(float(r["TotalSystemBenefit"]) for r in rows) == 180  # the dashboard's claimed total
    assert sum(float(r["TotalSystemBenefit"]) for r in records_csv(client, forecast)) == 360
    assert f'href="{html.escape("/data/records/?" + urlsplit(claims).query)}"' in part  # Choose columns: the same records


@with_records
def test_the_records_follow_the_filters_and_the_exclusions(client):
    page = client.get("/data/overview/?by=program&sector=Residential&series=claims&cs=include").content.decode()
    [claims] = links(page)
    p = parse_qs(urlsplit(claims).query)
    assert p["sector"] == ["Residential"] and "cs" not in p and p["pension"] == ["exclude"]  # C&S included: the records default
    rows = records_csv(client, claims)
    assert rows and all(r["PrgID"].startswith("PGE_Res") for r in rows)
    assert "Forecast," not in section(page)  # only the series shown


@with_records
def test_a_large_records_download_is_made_in_the_background(client, settings, signed_in, make_user):
    settings.DATA_DOWNLOAD_MAX_ROWS = 2
    page = client.get("/data/overview/?series=claims").content.decode()
    part = section(page)
    assert "made in the background" in part and "Sign in to have it made" in part and "next=" in part
    assert not links(page) and "Make this download" not in part

    signed_in(make_user("reader@example.org"))
    part = section(client.get("/data/overview/?series=claims").content.decode())
    assert "Make this download" in part and 'name="format"' in part
    query = html.unescape(re.search(r'name="query" value="([^"]+)"', part).group(1))
    response = client.post("/data/downloads/request", {"query": query, "format": "parquet"})
    job = DownloadJob.objects.get()
    assert response.status_code == 302 and (job.records, job.fmt, job.dataset) == (3, "parquet", "claims")


def test_nothing_published_says_no_records(client, db):
    assert "Claimed: no records." in section(client.get("/data/overview/").content.decode())


@pytest.mark.usefixtures("both", "goal_set")
def test_the_goals_and_budget_pages_offer_the_records_too(client):
    goals = client.get("/data/goals/").content.decode()
    [_, claims] = links(goals)
    assert sum(float(r["TotalSystemBenefit"]) for r in records_csv(client, claims)) == 180  # the goal page's claimed TSB
    budget = client.get("/data/budget/?year_from=2028&year_to=2031").content.decode()
    assert "Download the records behind these rows" in budget and links(budget)
