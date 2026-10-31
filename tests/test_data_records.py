import csv
import io
import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from openpyxl import load_workbook

from cedars.core import storage
from cedars.modules.data import query as dq
from cedars.modules.data import records
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.dictionary import service as dictionary
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .test_data_query import publish
from .test_snapshots import claims_deadline, confirm, filer  # noqa: F401 - filer is a fixture

pytestmark = pytest.mark.django_db


def sce_results() -> bytes:
    """SCE's claim: a MeasureID column PG&E's file lacks, and no expenditure columns."""
    t = pa.table(
        {
            "CEInputID": ["S1", "S2"],
            "PrgID": ["SCE_Res_Lt", "SCE_Com_HVAC"],
            "MeasureID": ["LT-01", "HV-02"],
            "Prg_ProgramSegment": ["resource_acquisition"] * 2,
            "Prg_PrimarySector": ["Residential", "Commercial"],
            "Prg_ProgramCategory": ["Downstream"] * 2,
            "UseCategory": ["Lighting", "HVAC"],
            "Used_Year": ["2028", "2028"],
            "Fuel_Sub_Flag": ["False", "False"],
            "TotalSystemBenefit": [11.0, 22.0],
            "FirstYearNetKWh": [100.0, 200.0],
            "TRCBen": [5.0, 6.0],
            "TRCCost": [2.0, 3.0],
            "CreditedPA": ["SCE", "SCE"],
        }
    )
    buf = io.BytesIO()
    pq.write_table(t, buf)
    return buf.getvalue()


@pytest.fixture
def sce_filer(make_user, cpuc_admin):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    admin = make_user("admin@sce.example.org")
    access.grant(cpuc_admin, admin, sce, perms.ORG_ADMIN, reason="first admin")
    u = make_user("analyst@sce.example.org")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(admin, u, sce, p)
    return u, sce


@pytest.fixture
def two_pas(filer, cpuc_admin, pge, sce_filer, django_capture_on_commit_callbacks):  # noqa: F811
    deadline = claims_deadline(cpuc_admin, 1)
    publish(filer, deadline, pge, django_capture_on_commit_callbacks, "c1")
    u, sce = sce_filer
    v = submissions.start(u, deadline, sce)
    submissions.record_validation(u, v, None, {"records": 2, "rejected": 0, "warnings": 0})
    key = "runs/s1/out/_results/measure_level_run.parquet"
    storage.store("results").put_bytes(key, sce_results())
    submissions.record_calculation(None, v, run="s1", input_hash="h", details={"results": {"measure_level": key}})
    confirm(u, SubmissionVersion.objects.get(pk=v.pk), django_capture_on_commit_callbacks)


def rq(**params):
    return records.from_params({k: v if isinstance(v, list) else [v] for k, v in params.items()})


def csv_rows(text):
    stamp = {line[2:].split(": ", 1)[0]: line[2:].split(": ", 1)[1] for line in text.splitlines() if line.startswith("# ")}
    rows = list(csv.reader(line for line in text.splitlines() if not line.startswith("# ")))
    return stamp, rows


def test_every_pa_in_one_file_with_their_columns_unioned(two_pas):
    p = records.page(rq(dataset="claims", columns="all"))
    assert p.count == 7 and len(p.rows) == 7  # 5 PG&E records and 2 SCE: nothing left out by default
    assert "MeasureID" in p.columns and "TotalExpenditures" in p.columns
    by_id = {r[p.columns.index("CEInputID")]: dict(zip(p.columns, r, strict=True)) for r in p.rows}
    assert by_id["S1"]["MeasureID"] == "LT-01" and by_id["S1"]["TotalExpenditures"] is None and by_id["S1"]["FiledBy"] == "SCE"
    assert by_id["M0"]["MeasureID"] is None and by_id["M0"]["TotalSystemBenefit"] == 100.0  # numbers stay numbers
    assert [r[p.columns.index("CEInputID")] for r in p.rows] == ["M0", "M1", "M2", "M3", "M4", "S1", "S2"]


def test_filters_by_pa_program_measure_and_year(two_pas):
    assert records.page(rq(pa="SCE")).count == 2
    assert records.page(rq(program="PGE_Res_HVAC, SCE_Res_Lt")).count == 3  # typed as a comma-separated list
    assert records.page(rq(measure="HV-02")).count == 1
    assert records.page(rq(year_from="2029")).count == 0
    assert records.page(rq(year_from="2028", year_to="2028", pa=["PGE"], cs="exclude", pension="exclude")).count == 3


def test_a_records_total_equals_the_dashboard_row(two_pas):
    q = dq.Query("claims", ("tsb",), filters=(("program", ("PGE_Res_HVAC",)),))
    p = records.page(records.RecordQuery(q, {}), limit=1)
    assert p.count == 2 and len(p.rows) == 1
    assert p.result.totals["tsb"] == 150 == dq.run(q.but(by=("program",))).rows[0]["tsb"]


def test_no_parameters_means_the_current_year():
    q = records.from_params({}, today=date(2028, 3, 1)).query
    assert (q.year_from, q.year_to, q.exclude) == (2028, 2028, ())
    assert records.from_params({"dataset": ["claims"]}).query.year_from is None


def test_columns_follow_the_dictionary_and_never_offer_a_restricted_field(two_pas):
    dictionary.load_catalog(
        None,
        {
            "source": "test",
            "fields": [
                {"name": "CEInputID", "kind": "input", "group": "Measure", "definition": "The measure's ID."},
                {"name": "TotalSystemBenefit", "kind": "output", "group": "Total system benefit", "definition": "TSB."},
                {"name": "TRCCost", "kind": "output", "group": "Cost-test costs", "audience": "restricted"},
            ],
        },
    )
    cat = records.catalog(dq.files_of(dq.Query("claims", ("tsb",))))
    groups = {g.label: g.names for g in cat.groups}
    assert list(groups)[0] == records.SOURCE and groups[records.SOURCE] == ["FiledBy", "Filing", "CountedPA"]
    assert groups["Measure inputs"] == ["CEInputID"] and groups["Total system benefit"] == ["TotalSystemBenefit"]
    assert "Prg_ProgramSegment" in groups["Program metadata"] and "TRCCost" not in cat.names
    assert cat.dictionary == "test"
    p = records.page(rq(col="TRCCost"))
    assert "TRCCost" not in p.columns and p.unknown == ["TRCCost"]


def test_a_column_choice_round_trips_through_a_short_url(two_pas):
    cat = records.catalog(dq.files_of(dq.Query("claims", ("tsb",))))
    every = cat.names
    choices = [
        cat.key,
        every,
        [],
        cat.key + ["TRCBen"],
        [c for c in cat.key if c != "Filing"],
        [c for c in every if c != "TRCBen"],
        ["CEInputID", "TRCBen"],
    ]
    for want in choices:
        want = [n for n in every if n in want]
        params = records.encode(cat, want)
        assert records.chosen(cat, params)[0] == want
    assert records.encode(cat, cat.key) == {}
    assert records.encode(cat, every) == {"columns": ["all"]}
    assert records.encode(cat, cat.key + ["TRCBen"]) == {"col": ["TRCBen"]}
    assert "colgroup" in records.encode(cat, [c for c in every if c != "TRCBen"])


def test_the_form_posts_and_lands_on_the_short_link(two_pas, client):
    form = {"dataset": "claims", "year_from": "2028", "year_to": "", "pa": ["SCE"], "program": "", "columns": "none"}
    form["col"] = ["CEInputID", "MeasureID", "TotalSystemBenefit"]
    form |= {code: "include" for code in dq.EXCLUSIONS}
    response = client.post("/data/records/", form)
    assert response.status_code == 302
    target = urlparse(response["Location"])
    params = parse_qs(target.query)
    assert params["pa"] == ["SCE"] and sorted(params["col"]) == ["CEInputID", "MeasureID", "TotalSystemBenefit"]
    html = client.get(f"{target.path}?{target.query}").content.decode()
    assert "Claimed records" in html and "2 records" in html and "LT-01" in html and "Columns: 3 of" in html
    assert 'href="/data/records/download.csv?' in html


def test_the_page_shows_the_records_and_says_what_is_missing(two_pas, client):
    html = client.get("/data/records/?dataset=claims&metric=tsb&program=PGE_Res_HVAC").content.decode()
    assert "M0" in html and "M1" in html and "2 records" in html and "Total system benefit ($): 150" in html
    assert "could not be shown" in client.get("/data/records/?metric=nope").content.decode()
    assert "No records match." in client.get("/data/records/?metric=tsb&program=NOPE").content.decode()
    assert "Not in these records" in client.get("/data/records/?dataset=claims&col=Nope").content.decode()
    assert "Choose at least one column" in client.get("/data/records/?dataset=claims&columns=none").content.decode()
    assert client.put("/data/records/").status_code == 405


def test_downloads_hold_only_the_chosen_records_and_columns_with_a_stamp(two_pas, client):
    qs = "dataset=claims&pa=SCE&columns=none&col=CEInputID&col=MeasureID&col=TotalSystemBenefit"
    response = client.get(f"/data/records/download.csv?{qs}")
    assert response.status_code == 200 and response["Content-Disposition"].endswith('release2_records.csv"')
    stamp, rows = csv_rows(b"".join(response.streaming_content).decode())
    assert rows == [["CEInputID", "TotalSystemBenefit", "MeasureID"], ["S1", "11", "LT-01"], ["S2", "22", "HV-02"]]  # the files' order
    assert json.loads(stamp["pas"]) == {"SCE": 2}  # the PAs the records hold (BR-08.3)
    assert stamp["records"] == "2" and json.loads(stamp["columns"]) == ["CEInputID", "TotalSystemBenefit", "MeasureID"]
    assert json.loads(stamp["filters"]) == {"pa": ["SCE"]} and stamp["release"] == "2" and stamp["kind"] == "records"
    assert all(f.startswith("SCE ") for f in json.loads(stamp["filings"]))  # only the filings that contribute rows (F26)

    t = pq.read_table(io.BytesIO(b"".join(client.get(f"/data/records/download.parquet?{qs}").streaming_content)))
    assert t.column_names == ["CEInputID", "TotalSystemBenefit", "MeasureID"] and t.column("TotalSystemBenefit").to_pylist() == [11, 22]
    assert pa.types.is_floating(t.schema.field("TotalSystemBenefit").type)
    assert json.loads(t.schema.metadata[b"cedars"])["records"] == 2

    wb = load_workbook(io.BytesIO(b"".join(client.get(f"/data/records/download.xlsx?{qs}").streaming_content)))
    assert list(wb["Data"].values) == [("CEInputID", "TotalSystemBenefit", "MeasureID"), ("S1", 11, "LT-01"), ("S2", 22, "HV-02")]
    assert dict(list(wb["About"].values)[1:])["records"] == "2"

    empty = b"".join(client.get("/data/records/download.csv?dataset=claims&pa=NOPE").streaming_content).decode()
    assert csv_rows(empty)[1][0][:2] == ["FiledBy", "Filing"] and len(csv_rows(empty)[1]) == 1
    assert client.get("/data/records/download.exe?dataset=claims").status_code == 404
    assert client.get("/data/records/download.csv?metric=nope").status_code == 400


def test_download_limits(two_pas, client, settings, monkeypatch):
    settings.DATA_DOWNLOAD_MAX_ROWS = 6
    response = client.get("/data/records/download.csv?dataset=claims")
    assert response.status_code == 413 and "7 records" in response.content.decode()
    assert "more records than the 6" in client.get("/data/records/?dataset=claims").content.decode()
    assert client.get("/data/records/download.csv?dataset=claims&pa=SCE").status_code == 200
    monkeypatch.setattr(records, "XLSX_MAX_ROWS", 1)
    assert client.get("/data/records/download.xlsx?dataset=claims&pa=SCE").status_code == 413
    assert client.get("/data/records/download.parquet?dataset=claims&pa=SCE").status_code == 200


def test_the_filing_choice_is_not_a_filter_on_the_filing_label():
    q = dq.from_params({"filing": ["2028-2031|TUAL"]})
    assert q.filing == "2028-2031|TUAL" and q.filters == ()
