"""The open data API (/api/v1/data, BR-11.5): public, the dashboards' and record downloads' own queries, public fields only, paged,
stamped, throttled like every API call."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from rest_framework.throttling import AnonRateThrottle

from cedars.core import storage
from cedars.modules.data import api as data_api
from cedars.modules.data import query as dq
from cedars.modules.data import records
from cedars.services.dictionary import service as dictionary
from cedars.services.releases import limits
from cedars.services.snapshots.models import Snapshot

from .test_data_records import rq, sce_filer, two_pas  # noqa: F401 - fixtures
from .test_snapshots import filer  # noqa: F401 - a fixture

pytestmark = pytest.mark.django_db

DICTIONARY = sorted((Path(settings.BASE_DIR) / "data" / "dictionary").glob("*.json"))[-1]


def load_dictionary(restricted=(), drop=(), label="test dictionary"):
    payload = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    payload["fields"] = [f for f in payload["fields"] if f["name"] not in drop]
    for f in payload["fields"]:
        if f["name"] in restricted:
            f["audience"] = "restricted"
    return dictionary.load_catalog(None, payload, label=label)


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def published(two_pas):  # noqa: F811
    load_dictionary()


def get(path, **params):
    return Client().get(path, params)


def test_every_endpoint_is_public_and_in_the_api_description(published):
    for path in ("", "metrics", "dimensions", "query", "columns", "records"):
        assert get(f"/api/v1/data/{path}").status_code == 200, path
    schema = Client().get("/api/v1/schema", HTTP_ACCEPT="application/json").content.decode()
    for path in ("/api/v1/data/query", "/api/v1/data/records", "/api/v1/data/metrics", "/api/v1/data/dimensions"):
        assert path in schema


def test_the_index_names_each_datasets_release(published):
    body = get("/api/v1/data/").json()
    claims = next(d for d in body["datasets"] if d["code"] == "claims")
    assert claims["release"] == 2 and claims["snapshots"] == 2 and claims["as_of"]
    assert next(d for d in body["datasets"] if d["code"] == "forecast")["release"] is None
    assert body["links"]["records"].endswith("/api/v1/data/records")


def test_an_aggregated_query_equals_the_dashboards_query(published):
    params = {"dataset": "claims", "metric": ["tsb", "trc", "kwh_first_year_net"], "by": "pa"}
    body = get("/api/v1/data/query", **params).json()
    expected = dq.run(dq.from_params({k: v if isinstance(v, list) else [v] for k, v in params.items()}))
    assert body["by"] == ["pa"] and body["count"] == 2
    rows = {r["pa"]: r for r in body["rows"]}
    for r in expected.rows:
        assert rows[r["pa"]]["tsb"] == r["tsb"] and rows[r["pa"]]["trc"] == r["trc"]
        assert rows[r["pa"]]["records"] == r["_rows"] and rows[r["pa"]]["sums"]["TotalSystemBenefit"] == r["_sums"]["TotalSystemBenefit"]
    assert body["totals"]["tsb"] == expected.totals["tsb"] == 180 + 33  # C&S and pension left out by default, as on the dashboards
    assert body["totals"]["records"] == 5 and "pa" not in body["totals"]
    stamp = body["stamp"]
    assert stamp["release"] == 2 and stamp["dataset"] == "claims" and stamp["dictionary"] == "test dictionary"
    assert stamp["excluded"] == ["Pension and Benefits programs", "Codes and standards"] and len(stamp["snapshots"]) == 2
    assert stamp["query"] == expected.query.query_string() and stamp["notes"]
    included = get("/api/v1/data/query", dataset="claims", cs="include", pension="include").json()
    assert included["totals"]["tsb"] == 1187 + 33 and included["stamp"]["excluded"] == []


def test_query_rows_are_paged_with_links(published):
    body = get("/api/v1/data/query", dataset="claims", by="program", limit=2).json()
    assert body["count"] == 4 and len(body["rows"]) == 2 and body["previous"] is None
    second = Client().get(body["next"]).json()
    assert [r["program"] for r in body["rows"] + second["rows"]] == ["PGE_Com_Lt", "PGE_Res_HVAC", "SCE_Com_HVAC", "SCE_Res_Lt"]
    assert parse_qs(urlparse(body["next"]).query)["offset"] == ["2"] and second["next"] is None and second["previous"]
    assert body["totals"] == second["totals"]  # over every row, on every page


def test_records_are_paged_with_public_columns_and_a_stamp(published):
    body = get("/api/v1/data/records", dataset="claims", columns="none", col=["CEInputID", "MeasureID", "TotalSystemBenefit"], limit=3)
    body = body.json()
    page = records.page(rq(dataset="claims", columns="none", col=["CEInputID", "MeasureID", "TotalSystemBenefit"]))
    assert body["columns"] == page.columns and set(page.columns) == {"CEInputID", "TotalSystemBenefit", "MeasureID"}  # as the downloads
    assert body["count"] == 7 and [r["CEInputID"] for r in body["rows"]] == ["M0", "M1", "M2"]
    assert body["rows"][0] == {"CEInputID": "M0", "TotalSystemBenefit": 100.0, "MeasureID": None}
    assert list(body["rows"][0]) == page.columns
    assert body["totals"] == {"tsb": 1220.0, "records": 7}  # records leave nothing out unless asked
    rest = Client().get(body["next"]).json()
    assert [r["CEInputID"] for r in rest["rows"]] == ["M3", "M4", "S1"]
    stamp = body["stamp"]
    assert stamp["pas"] == {"PGE": 5, "SCE": 2} and stamp["records"] == 7 and stamp["release"] == 2
    assert stamp["dictionary"] == "test dictionary" and stamp["excluded"] == []
    assert "col=" in stamp["query"]  # the record page's own link to the same records and columns

    # the same rows as the record download's query
    assert [dict(zip(page.columns, r, strict=True)) for r in page.rows][:3] == body["rows"]


def test_record_filters_and_defaults(published):
    assert get("/api/v1/data/records", dataset="claims", pa="SCE").json()["count"] == 2
    assert get("/api/v1/data/records", dataset="claims", program="PGE_Res_HVAC, SCE_Res_Lt").json()["count"] == 3
    assert get("/api/v1/data/records", dataset="claims", cs="exclude", pension="exclude").json()["count"] == 5
    key = get("/api/v1/data/records", dataset="claims").json()["columns"]
    assert key[:2] == ["FiledBy", "Filing"] and "TotalSystemBenefit" in key and "Prg_ProgramSegment" not in key


def test_a_field_the_dictionary_does_not_mark_public_is_never_served(published):
    load_dictionary(restricted=("TRCCost", "Prg_PrimarySector"), drop=("MeasureID",), label="restricted")
    cols = get("/api/v1/data/columns", dataset="claims").json()
    names = [c["name"] for g in cols["groups"] for c in g["columns"]]
    assert "TRCCost" not in names and "MeasureID" not in names and "Prg_PrimarySector" not in names  # restricted, or not in it
    assert "TRCBen" in names and names[:3] == ["FiledBy", "Filing", "CountedPA"]
    every = get("/api/v1/data/records", dataset="claims", columns="all").json()
    assert "TRCCost" not in every["columns"] and "MeasureID" not in every["columns"]
    assert all("TRCCost" not in r and "MeasureID" not in r for r in every["rows"])
    for asked in ({"col": "TRCCost"}, {"col": "MeasureID"}, {"col": "Nope"}):
        r = get("/api/v1/data/records", dataset="claims", **asked)
        assert r.status_code == 400 and "not a public column" in r.json()["detail"]

    metrics = {m["code"] for m in get("/api/v1/data/metrics").json()["results"]}
    assert "trc_benefit" in metrics and "trc" not in metrics and "trc_cost" not in metrics  # a ratio needs both fields public
    dims = {d["code"] for d in get("/api/v1/data/dimensions").json()["dimensions"]}
    assert "sector" not in dims and "measure" not in dims and {"pa", "pa_type", "segment", "organisation", "filing"} <= dims
    for params in ({"metric": "trc"}, {"by": "sector"}, {"sector": "Residential"}, {"by": "measure"}):
        r = get("/api/v1/data/query", dataset="claims", **params)
        assert r.status_code == 400 and "not public data" in r.json()["detail"], params
    assert get("/api/v1/data/records", dataset="claims", sector="Residential").status_code == 400


def test_nothing_is_served_without_a_dictionary(two_pas):  # noqa: F811
    cols = get("/api/v1/data/columns", dataset="claims").json()
    assert [c["name"] for g in cols["groups"] for c in g["columns"]] == ["FiledBy", "Filing", "CountedPA"]
    assert get("/api/v1/data/metrics").json()["results"] == []
    assert get("/api/v1/data/query", dataset="claims").status_code == 400


def test_the_catalog_and_dimensions(published):
    body = get("/api/v1/data/metrics").json()
    tsb = next(m for m in body["results"] if m["code"] == "tsb")
    assert tsb["numerator"] == ["TotalSystemBenefit"] and tsb["adds_up"] and tsb["unit"] == "$"
    assert body["bases"]["kwh"]["net_lifecycle"] == "kwh_lifecycle_net" and body["dictionary"] == "test dictionary"
    dims = get("/api/v1/data/dimensions").json()
    by_code = {d["code"]: d for d in dims["dimensions"]}
    assert by_code["pa"]["view_by"] and by_code["pa"]["values"] == ["PGE", "SCE"] and not by_code["program"]["view_by"]
    assert by_code["measure_type"]["values"] == ["Energy efficiency", "Fuel substitution"]
    exclusions = {e["code"]: e for e in dims["exclusions"]}
    assert exclusions["cs"]["excluded_by_default_in_query"] and not exclusions["esa"]["excluded_by_default_in_query"]
    assert not exclusions["cs"]["excluded_by_default_in_records"]
    assert [g["type"] for g in dims["pas"]] == ["IOU"] and {p["code"] for p in dims["pas"][0]["pas"]} == {"PGE", "SCE"}


def test_unreadable_calls_are_refused_with_a_reason(published):
    cases = [
        ({"metric": "nope"}, "unknown metric"),
        ({"by": "nope"}, "unknown dimension"),
        ({"metrics": "tsb"}, "unknown parameter: metrics"),
        ({"limit": "0"}, "limit must be from 1"),
        ({"limit": str(settings.DATA_API_MAX_ROWS + 1)}, "limit must be from 1"),
        ({"offset": "-1"}, "offset must be a whole number"),
        ({"release": "99"}, "no release 99"),
        ({"year_from": "2030", "year_to": "2028"}, "first year is after the last"),
    ]
    for params, words in cases:
        r = get("/api/v1/data/query", dataset="claims", **params)
        assert r.status_code == 400 and words in r.json()["detail"], params
    assert "unknown parameter: by" in get("/api/v1/data/records", by="pa").json()["detail"]
    assert "no public column group" in get("/api/v1/data/records", dataset="claims", colgroup="nope").json()["detail"]


def test_an_earlier_release_is_read_when_named(published):
    first = get("/api/v1/data/query", dataset="claims", release=1).json()
    assert first["stamp"]["release"] == 1 and first["totals"]["records"] == 3  # PG&E's claim alone
    assert get("/api/v1/data/records", dataset="claims", release=1).json()["stamp"]["pas"] == {"PGE": 5}


def test_a_value_that_is_not_a_number_reads_as_null(published, filer):  # noqa: F811
    snap = Snapshot.objects.get(dataset="claims", organisation__code="SCE")
    t = pq.read_table(dq.local_path({"uri": storage.store("snapshots").uri(snap.key), "key": snap.key, "sha256": snap.sha256}))
    t = t.set_column(t.schema.get_field_index("TotalSystemBenefit"), "TotalSystemBenefit", pa.array([float("nan"), 22.0]))
    storage.store("snapshots").put_bytes(snap.key, _parquet(t))
    rows = get("/api/v1/data/records", dataset="claims", pa="SCE", columns="none", col=["CEInputID", "TotalSystemBenefit"]).json()
    assert rows["rows"][0] == {"CEInputID": "S1", "TotalSystemBenefit": None}


def _parquet(t) -> bytes:
    import io

    buf = io.BytesIO()
    pq.write_table(t, buf)
    return buf.getvalue()


def test_calls_are_throttled_like_every_api_call(published, monkeypatch):
    monkeypatch.setattr(AnonRateThrottle, "THROTTLE_RATES", {"user": "3/minute", "anon": "2/minute"})
    c = Client(REMOTE_ADDR="10.0.0.21")
    assert [c.get("/api/v1/data/metrics").status_code for _ in range(2)] == [200, 200]
    over = c.get("/api/v1/data/query")
    assert over.status_code == 429 and int(over["Retry-After"]) > 0


def test_the_page_limit_is_listed():
    assert limits.BY_KEY["api_data_rows"].current() == f"{settings.DATA_API_MAX_ROWS:,} rows"
    assert data_api.SOURCE_COLUMNS == ("FiledBy", "Filing", "CountedPA")


def test_before_anything_is_published(db):
    load_dictionary()
    body = get("/api/v1/data/query", dataset="forecast").json()
    assert body["stamp"]["release"] is None and body["rows"] == [] and body["count"] == 0 and body["totals"]["records"] == 0
    assert "Nothing has been published" in body["stamp"]["notes"][0]
    records_body = get("/api/v1/data/records", dataset="forecast").json()
    assert records_body["rows"] == [] and records_body["count"] == 0 and records_body["stamp"]["release"] is None
