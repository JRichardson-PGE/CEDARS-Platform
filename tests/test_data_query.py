import csv
import io
import json
from datetime import timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.utils import timezone
from openpyxl import load_workbook

from cedars.core import storage
from cedars.modules.data import downloads, metrics
from cedars.modules.data import query as dq
from cedars.services.registry.models import FilingCycle
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .test_snapshots import claims_deadline, confirm, filer  # noqa: F401 - filer is a fixture

pytestmark = pytest.mark.django_db

RECORDS = [
    # PrgID, segment, sector, category, use category, year, fuel sub, TSB, kWh, therms, TRCBen, TRCCost
    ("PGE_Res_HVAC", "resource_acquisition", "Residential", "Downstream", "HVAC", "2028", "False", 100.0, 2000.0, 10.0, 150.0, 100.0),
    ("PGE_Res_HVAC", "resource_acquisition", "Residential", "Downstream", "Lighting", "2028", "True", 50.0, 1000.0, 0.0, 50.0, 100.0),
    ("PGE_Com_Lt", "market_support", "Commercial", "Midstream", "Lighting", "2028", "False", 30.0, 500.0, 0.0, 40.0, 20.0),
    ("PGE_CS", "codes_and_standards", "Cross-Cutting", "Codes", "Building Envelope", "2028", "False", 1000.0, 9000.0, 5.0, 0.0, 0.0),
    ("PGE_Pension", "resource_acquisition", "Cross-Cutting", "Pension and Benefits", "None", "2028", "False", 7.0, 0.0, 0.0, 0.0, 9.0),
]


def results_file(records=RECORDS, scale=1.0, drop=()) -> bytes:
    cols = {
        "CEInputID": [f"M{i}" for i in range(len(records))],
        "PrgID": [r[0] for r in records],
        "Prg_ProgramSegment": [r[1] for r in records],
        "Prg_PrimarySector": [r[2] for r in records],
        "Prg_ProgramCategory": [r[3] for r in records],
        "UseCategory": [r[4] for r in records],
        "Used_Year": [r[5] for r in records],
        "Fuel_Sub_Flag": [r[6] for r in records],
        "TotalSystemBenefit": [scale * r[7] for r in records],
        "FirstYearNetKWh": [scale * r[8] for r in records],
        "FirstYearNetThm": [scale * r[9] for r in records],
        "TRCBen": [scale * r[10] for r in records],
        "TRCCost": [scale * r[11] for r in records],
        "CreditedPA": ["PGE"] * len(records),
    }
    buf = io.BytesIO()
    pq.write_table(pa.table({k: v for k, v in cols.items() if k not in drop}), buf)
    return buf.getvalue()


def publish(filer, deadline, pge, capture, run, **kw):  # noqa: F811 - filer is the fixture's value
    v = submissions.start(filer, deadline, pge)
    submissions.record_validation(filer, v, None, {"records": 5, "rejected": 0, "warnings": 0})
    key = f"runs/{run}/out/_results/measure_level_run.parquet"
    storage.store("results").put_bytes(key, results_file(**kw))
    submissions.record_calculation(None, v, run=run, input_hash="h", details={"results": {"measure_level": key}})
    return confirm(filer, SubmissionVersion.objects.get(pk=v.pk), capture)


@pytest.fixture
def claims(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    return publish(filer, claims_deadline(cpuc_admin, 1), pge, django_capture_on_commit_callbacks, "c1")


def test_nothing_published_yet(db):
    r = dq.run(dq.Query("claims", ("tsb",)))
    assert r.rows == [] and r.release is None and r.totals["tsb"] is None and "Nothing has been published" in r.notes[0]


def test_sums_by_dimension_with_the_default_exclusions(claims):
    r = dq.run(dq.Query("claims", ("tsb", "kwh_first_year_net", "trc"), by=("sector",)))
    assert r.release.number == 1 and len(r.files) == 1
    by = {row["sector"]: row for row in r.rows}
    assert set(by) == {"Commercial", "Residential"}  # C&S and Pension and Benefits left out by default
    assert by["Residential"]["tsb"] == 150 and by["Residential"]["kwh_first_year_net"] == 3.0  # MWh
    assert by["Residential"]["trc"] == 1.0 and by["Commercial"]["trc"] == 2.0
    assert r.totals["tsb"] == 180 and r.totals["_rows"] == 3
    assert r.totals["trc"] == pytest.approx(240 / 220)  # from summed benefits and costs, never an average of ratios
    assert r.notes == ["Left out: Pension and Benefits programs.", "Left out: Codes and standards."]


def test_toggles_change_every_total_as_the_query_does(claims):
    base = dq.Query("claims", ("tsb",))
    assert dq.run(base).totals["tsb"] == 180
    assert dq.run(base.but(exclude=("pension",))).totals["tsb"] == 1180
    assert dq.run(base.but(exclude=())).totals["tsb"] == 1187
    assert dq.run(base.but(filters=(("measure_type", ("Fuel substitution",)),))).totals["tsb"] == 50
    assert dq.run(base.but(filters=(("use_category", ("lighting",)),))).totals["tsb"] == 80  # case-insensitive
    assert dq.run(base.but(year_from=2029)).totals["_rows"] == 0
    r = dq.run(base.but(by=("program",), filters=(("program", ("PGE_Res_HVAC", "PGE_Com_Lt")),)))
    assert [row["program"] for row in r.rows] == ["PGE_Com_Lt", "PGE_Res_HVAC"]


def test_a_ratio_over_no_cost_is_blank(claims):
    r = dq.run(dq.Query("claims", ("trc",), by=("segment",), exclude=("pension",)))
    assert {row["segment"]: row["trc"] for row in r.rows}["codes_and_standards"] is None


def test_query_state_round_trips_through_the_url(claims):
    q = dq.Query(
        "claims",
        ("tsb", "therms_first_year_net"),
        by=("pa", "sector"),
        year_from=2028,
        year_to=2028,
        filters=(("sector", ("Residential",)),),
        exclude=("pension", "esa"),
    )
    from django.http import QueryDict

    again = dq.from_params(QueryDict(q.query_string()))
    assert again == q
    assert dq.run(again).totals == dq.run(q).totals


def test_params_resolve_the_view_controls():
    q = dq.from_params({"metric": ["kwh", "co2"], "lifecycle": "1", "gross": "1", "cs": "include", "esa": "exclude"})
    assert q.metrics == ("kwh_lifecycle_gross", "co2_lifecycle_gross") and q.exclude == ("pension", "esa")
    assert dq.from_params({"metric": "kw", "lifecycle": "1"}).metrics == ("kw_first_year_net",)
    for bad in ({"metric": "nope"}, {"by": "colour"}, {"year_from": "20x8"}, {"year_from": "2030", "year_to": "2029"}):
        with pytest.raises(dq.QueryError):
            dq.from_params(bad)


def test_a_missing_column_reads_as_blank(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    publish(filer, claims_deadline(cpuc_admin, 1), pge, django_capture_on_commit_callbacks, "c1", drop=("UseCategory", "TRCCost"))
    r = dq.run(dq.Query("claims", ("tsb", "trc"), by=("use_category",)))
    assert [row["use_category"] for row in r.rows] == [None] and r.totals["tsb"] == 180 and r.totals["trc"] is None


def test_forecast_reads_each_pas_latest_filing_or_the_one_asked_for(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    cycle = FilingCycle.objects.create(first_year=2028, last_year=2031)
    due = timezone.now() - timedelta(days=5)
    tual = submissions.add_deadline(cpuc_admin, module="budgets", kind="TUAL", cycle=cycle, due_at=due)
    mcal = submissions.add_deadline(cpuc_admin, module="budgets", kind="MCAL", cycle=cycle, due_at=due + timedelta(days=2))
    submissions.set_policy(cpuc_admin, "budgets", late_confirmation=True)
    publish(filer, tual, pge, django_capture_on_commit_callbacks, "t1")
    publish(filer, mcal, pge, django_capture_on_commit_callbacks, "m1", scale=2.0)
    latest = dq.run(dq.Query("forecast", ("tsb",)))
    assert latest.totals["tsb"] == 360 and [f["kind"] for f in latest.files] == ["MCAL"]
    assert dq.run(dq.Query("forecast", ("tsb",), filing="TUAL")).totals["tsb"] == 180
    assert dq.run(dq.Query("forecast", ("tsb",), filing="2028-2031|tual")).totals["tsb"] == 180
    none = dq.run(dq.Query("forecast", ("tsb",), filing="ABAL"))
    assert none.rows == [] and "No published filing matches" in none.notes[-1]
    assert [row["filing"] for row in dq.run(dq.Query("forecast", ("tsb",), by=("filing",), filing="MCAL")).rows] == ["2028-2031 MCAL"]


def test_an_earlier_release_can_be_read(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    publish(filer, claims_deadline(cpuc_admin, 1), pge, django_capture_on_commit_callbacks, "c1")
    publish(filer, claims_deadline(cpuc_admin, 2), pge, django_capture_on_commit_callbacks, "c2", scale=3.0)
    assert dq.run(dq.Query("claims", ("tsb",))).totals["tsb"] == 540  # Q2 replaces Q1 (year to date)
    assert dq.run(dq.Query("claims", ("tsb",), release=1)).totals["tsb"] == 180


def test_snapshots_in_s3_are_read_from_a_local_copy(claims, settings, tmp_path, monkeypatch):
    from cedars.services.snapshots import service as snapshots

    real = snapshots.release_files

    def from_s3(release):
        return [f | {"uri": f"s3://bucket/{f['key']}"} for f in real(release)]

    monkeypatch.setattr(snapshots, "release_files", from_s3)
    r = dq.run(dq.Query("claims", ("tsb",)))
    assert r.totals["tsb"] == 180
    [copy] = list((tmp_path / "data_cache").iterdir())
    assert copy.name == f"{r.files[0]['sha256']}.parquet"


def test_downloads_carry_the_same_totals_and_a_stamp(claims):
    r = dq.run(dq.Query("claims", ("tsb", "kwh_first_year_net", "trc"), by=("sector",)))
    text = downloads.render(r, "csv").decode()
    stamp = {line[2:].split(": ", 1)[0]: line[2:].split(": ", 1)[1] for line in text.splitlines() if line.startswith("# ")}
    assert stamp["release"] == "1" and json.loads(stamp["snapshots"]) == [r.files[0]["snapshot"]]
    assert json.loads(stamp["excluded"]) == ["Pension and Benefits programs", "Codes and standards"]
    assert json.loads(stamp["metrics"])["trc"]["denominator"] == ["TRCCost"]
    rows = list(csv.reader(line for line in text.splitlines() if not line.startswith("# ")))
    assert rows[0][:4] == [
        "Primary sector",
        "Total system benefit ($)",
        "Electric energy savings, first year net (MWh)",
        "TRC ratio (ratio)",
    ]
    assert "TotalSystemBenefit" in rows[0] and rows[-1][0] == "Total" and float(rows[-1][1]) == r.totals["tsb"]

    wb = load_workbook(io.BytesIO(downloads.render(r, "xlsx")))
    data = list(wb["Data"].values)
    assert data[-1][0] == "Total" and data[-1][1] == 180
    assert dict(list(wb["About"].values)[1:])["release"] == "1"

    t = pq.read_table(io.BytesIO(downloads.render(r, "parquet")))
    assert sum(t.column("Total system benefit ($)").to_pylist()) == r.totals["tsb"]
    assert json.loads(t.schema.metadata[b"cedars"])["dataset"] == "claims"


def test_download_view_is_public_and_runs_the_page_query(claims, client):
    response = client.get("/data/download.csv?dataset=claims&metric=tsb&by=sector&cs=include")
    assert response.status_code == 200 and response["Content-Disposition"].endswith('release1_sector.csv"')
    assert "Cross-Cutting" in response.content.decode()
    assert client.get("/data/download.xlsx?metric=tsb").status_code == 200
    assert client.get("/data/download.parquet?metric=tsb&by=program").status_code == 200
    assert client.get("/data/download.csv?metric=nope").status_code == 400
    assert client.get("/data/download.exe?metric=tsb").status_code == 404
    assert client.post("/data/download.csv?metric=tsb").status_code == 405


def test_download_size_limit(claims, client, settings):
    settings.DATA_DOWNLOAD_MAX_ROWS = 1
    assert client.get("/data/download.csv?metric=tsb&by=sector").status_code == 413


def test_metric_catalog():
    assert metrics.get("tsb").adds_up and not metrics.get("trc").adds_up
    assert metrics.get("nox_first_year_net").numerator == ("FirstYearNetElecNOx", "FirstYearNetGasNOx")
    assert metrics.get("nox_first_year_net").value({"FirstYearNetElecNOx": 2204.62262}) == pytest.approx(1)  # pounds to metric tons
    assert metrics.get("trc").value({"TRCBen": 5, "TRCCost": 0}) is None
    rows = metrics.catalog_rows()
    assert {"tsb", "trc_no_admin", "kwh_lifecycle_gross", "percent_of_goal"} <= {r["code"] for r in rows}


def test_metric_fields_are_canonical_output_fields():
    """Every field a metric sums is in the field dictionary the platform ships (decision L)."""
    from pathlib import Path

    catalog = json.loads((Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.80.json").read_text())
    names = {f["name"] for f in catalog["fields"]}
    missing = {f for m in metrics.CATALOG.values() for f in m.fields} - names
    assert not missing
    assert {d.column for d in dq.DIMENSIONS.values() if d.column} <= names
    assert {e.column for e in dq.EXCLUSIONS.values()} <= names


def test_metric_catalog_download(client):
    response = client.get("/data/metrics.csv")
    assert response.status_code == 200 and b"TotalSystemBenefit" in response.content
