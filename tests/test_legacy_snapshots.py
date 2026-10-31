import pyarrow as pa
import pytest
from django.core.management import call_command

from cedars.modules.budgets import legacy as legacy_filings
from cedars.modules.budgets.models import BudgetResult
from cedars.modules.claims import legacy as legacy_claims
from cedars.modules.claims import service as claims
from cedars.modules.claims.models import ClaimResult
from cedars.modules.data import query as dq
from cedars.services.legacy import snapshot
from cedars.services.snapshots import service as snapshots
from cedars.services.snapshots.models import Release, Snapshot

from .test_legacy_claims import _version, _year
from .test_legacy_filings import _version as _filing_version
from .test_legacy_filings import downloads  # noqa: F401 - a fixture

pytestmark = pytest.mark.django_db
SUMS = ("tsb", "expenditure", "incentives", "kwh_first_year_net", "kwh_lifecycle_net", "trc_benefit", "pac_benefit", "pac_cost")


def _by_pa(dataset, metrics=SUMS, **kw):
    q = dq.Query(dataset=dataset, metrics=metrics, by=("pa",), exclude=(), **kw)
    return {r["pa"]: r for r in dq.run(q).rows}


def _results(version, model=ClaimResult):
    out = {}
    for r in model.objects.filter(version=version):
        for k, v in r.values.items():
            out[k] = out.get(k, 0) + (v or 0)
    return out


@pytest.mark.parametrize("year,layout", [(2023, "claims_b"), (2019, "claims_a")])
def test_a_claim_year_publishes_one_snapshot_per_pa_and_the_dashboard_adds_up_to_the_loads_totals(tmp_path, year, layout):
    _year(tmp_path, year, layout=layout, sw=year >= 2023)
    out = legacy_claims.load(year)
    assert out.snapshots == 2 and out.notes == []
    assert sorted(Snapshot.objects.values_list("organisation__code", flat=True)) == ["PGE", "SCE"]  # SW stays outside the totals
    (release,) = Release.objects.filter(dataset="claims")  # one release for the whole year
    assert release.snapshots.count() == 2

    rows = _by_pa("claims")
    for code in ("PGE", "SCE"):
        v = _version(code, year)
        loaded = _results(v)
        expenditure = claims.table(v).total.expenditures
        r = rows[code]
        assert r["tsb"] == pytest.approx(loaded["TotalSystemBenefit"])
        assert r["kwh_first_year_net"] == pytest.approx(loaded["FirstYearNetKWh"] / 1000)  # shown in MWh
        assert r["trc_benefit"] == pytest.approx(loaded["TRCBen"]) and r["pac_benefit"] == pytest.approx(loaded["PACBen"])
        assert r["expenditure"] == pytest.approx(float(expenditure))  # derived before 2023, as filed from 2023
    assert rows["PGE"]["expenditure"] == pytest.approx(100.0)
    if layout == "claims_a":  # derived: the four incentive outputs (from 2023 the file's own column, left blank in this example)
        assert rows["PGE"]["incentives"] == pytest.approx(40.0)

    # the program columns come with each record: segment and sector read from the stored program table
    segments = dq.run(dq.Query(dataset="claims", metrics=("tsb",), by=("segment", "sector"), exclude=())).rows
    assert {(r["segment"], r["sector"]) for r in segments} == {("Resource Acquisition", "Commercial")}

    again = legacy_claims.load(year)
    assert again.snapshots == 0 and Release.objects.filter(dataset="claims").count() == 1


def test_imports_loaded_without_a_snapshot_get_one_from_the_backfill(tmp_path, monkeypatch, capsys):
    _year(tmp_path)

    def broken(imp, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(snapshot, "publish_snapshot", broken)
    out = legacy_claims.load(2023)
    assert out.published == ["PGE", "SCE", "SW"] and out.snapshots == 0
    assert any("PGE: no dashboard snapshot (disk full)" in n for n in out.notes)
    assert not Snapshot.objects.exists() and snapshots.current_release("claims") is None
    monkeypatch.undo()

    call_command("snapshot_legacy")
    assert "2 snapshots made, 0 failed" in capsys.readouterr().out
    assert snapshots.current_release("claims").snapshots.count() == 2
    assert _by_pa("claims")["PGE"]["tsb"] == pytest.approx(_results(_version("PGE"))["TotalSystemBenefit"])
    call_command("snapshot_legacy")
    assert "0 snapshots made" in capsys.readouterr().out and Release.objects.filter(dataset="claims").count() == 1


def test_a_filing_publishes_forecast_snapshots(downloads):  # noqa: F811 - the fixture
    downloads()
    out = legacy_filings.load(legacy_filings.BY_KEY["2028-2031-application"])
    assert out.snapshots == 2
    rows = _by_pa("forecast", metrics=("trc_benefit", "pac_benefit", "expenditure"))
    for code in ("PGE", "BAY"):
        loaded = _results(_filing_version(code), BudgetResult)
        assert rows[code]["trc_benefit"] == pytest.approx(loaded["TRCBen"]) and rows[code]["pac_benefit"] == pytest.approx(loaded["PACBen"])
    by_year = dq.run(dq.Query(dataset="forecast", metrics=("expenditure",), by=("year",), exclude=())).rows
    assert [r["year"] for r in by_year] == [2028, 2029, 2030, 2031]


def test_measure_and_outputs_batches_line_up_whatever_their_row_groups():
    a = pa.table({"x": list(range(7))}).to_batches(max_chunksize=3)
    b = pa.table({"y": list(range(7))}).to_batches(max_chunksize=2)
    pairs = list(snapshot._aligned(iter(a), iter(b)))
    assert [m["x"].to_pylist() for m, _ in pairs] == [o["y"].to_pylist() for _, o in pairs]
    assert sum(m.num_rows for m, _ in pairs) == 7
    with pytest.raises(ValueError, match="different numbers of rows"):
        list(snapshot._aligned(iter(a), iter(b[:2])))
