import pyarrow as pa
import pytest
from django.core.management import call_command

from cedars.modules.budgets import legacy as legacy_filings
from cedars.modules.budgets.models import BudgetResult
from cedars.modules.claims import legacy as legacy_claims
from cedars.modules.claims import service as claims
from cedars.modules.claims.models import ClaimResult
from cedars.modules.data import query as dq
from cedars.services.audit.models import AuditEvent
from cedars.services.legacy import snapshot, tables
from cedars.services.legacy.models import LegacyImport
from cedars.services.snapshots import service as snapshots
from cedars.services.snapshots.models import Release, Snapshot

from .legacy_fixtures import legacy_claim
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


def test_a_filing_publishes_forecast_snapshots(downloads, capsys):  # noqa: F811 - the fixture
    downloads()
    out = legacy_filings.load(legacy_filings.BY_KEY["2028-2031-application"])
    assert out.snapshots == 2
    rows = _by_pa("forecast", metrics=("trc_benefit", "pac_benefit", "expenditure"))
    for code in ("PGE", "BAY"):
        loaded = _results(_filing_version(code), BudgetResult)
        assert rows[code]["trc_benefit"] == pytest.approx(loaded["TRCBen"]) and rows[code]["pac_benefit"] == pytest.approx(loaded["PACBen"])
    by_year = dq.run(dq.Query(dataset="forecast", metrics=("expenditure",), by=("year",), exclude=())).rows
    assert [r["year"] for r in by_year] == [2028, 2029, 2030, 2031]

    call_command("load_legacy_filings", "--rebuild-benefits")  # filings file TRC and PAC benefits and no RIM: nothing to rebuild
    assert "2 imports checked, 0 corrected, 2 with every benefit filed" in capsys.readouterr().out


def test_measure_and_outputs_batches_line_up_whatever_their_row_groups():
    a = pa.table({"x": list(range(7))}).to_batches(max_chunksize=3)
    b = pa.table({"y": list(range(7))}).to_batches(max_chunksize=2)
    pairs = list(snapshot._aligned(iter(a), iter(b)))
    assert [m["x"].to_pylist() for m, _ in pairs] == [o["y"].to_pylist() for _, o in pairs]
    assert sum(m.num_rows for m, _ in pairs) == 7
    with pytest.raises(ValueError, match="different numbers of rows"):
        list(snapshot._aligned(iter(a), iter(b[:2])))


BENEFITS = {"Electric Benefits": "30", "Gas Benefits": "10", "Other Benefits": "5", "TRC Cost": "40", "PAC Cost": "20"}


def test_benefits_come_from_the_benefit_columns_and_fall_back_to_ratio_times_cost_per_record():
    cols = {
        "ElecBen": pa.array([30.0, None, 8.0]),
        "GasBen": pa.array([10.0, None, None]),
        "OtherBen": pa.array([5.0, None, None]),
        "TRCRatio": pa.array([1.25, 2.0, 1.0]),
        "TRCCost": pa.array([40.0, 10.0, None]),
        "PACRatio": pa.array([2.5, 2.0, 1.0]),
        "PACCost": pa.array([20.0, 10.0, 1.0]),
        "RIMRatio": pa.array([1.0, 1.0, 1.0]),
        "RIMCost": pa.array([3.0, 4.0, 5.0]),
        "RIMBen": pa.array([7.0, 7.0, 7.0]),
    }
    b = tables.benefits(cols)
    assert b["TRCBen"].values.to_pylist() == [45.0, 20.0, 8.0] and b["TRCBen"].source == "benefit columns"  # record 2: ratio x cost
    assert b["TRCBen"].from_ratio.to_pylist() == [50.0, 20.0, 0.0]
    assert b["PACBen"].values.to_pylist() == [40.0, 20.0, 8.0]
    assert b["RIMBen"].values.to_pylist() == [7.0, 7.0, 7.0] and b["RIMBen"].source == "filed"
    rim = tables.benefits({k: cols[k] for k in ("ElecBen", "GasBen", "RIMRatio", "RIMCost")})["RIMBen"]  # PAC's parts
    assert rim.values.to_pylist() == [40.0, 4.0, 8.0]
    only_ratios = tables.benefits({k: v for k, v in cols.items() if k not in ("ElecBen", "GasBen", "OtherBen", "RIMBen")})
    assert only_ratios["TRCBen"].source == "ratio x cost" and only_ratios["RIMBen"].values.to_pylist() == [3.0, 4.0, 5.0]


def _benefit_year(tmp_path):
    pge = [
        legacy_claim("PGE-2019-0001", "PGE", "PGE_Com_001", 2019, **BENEFITS),
        legacy_claim("PGE-2019-0002", "PGE", "PGE_Com_001", 2019, **BENEFITS),
    ]
    _year(tmp_path, 2019, layout="claims_a", pge=pge, sw=False)


def test_a_claim_year_prices_the_tests_from_its_benefit_columns(tmp_path):
    _benefit_year(tmp_path)
    legacy_claims.load(2019)
    v = _version("PGE", 2019)
    loaded = _results(v)
    assert loaded["TRCBen"] == pytest.approx(90.0) and loaded["PACBen"] == pytest.approx(80.0)  # not 1.25 x 40 and 2.5 x 20 per record
    checks = {c["name"]: c for c in LegacyImport.objects.get(version=v).reconciliation["checks"]}
    assert not checks["TRCBen = ratio x cost"]["ok"] and checks["TRCBen = ratio x cost"]["rebuilt"] == pytest.approx(100.0)
    rows = _by_pa("claims", metrics=("trc", "pac"))
    assert rows["PGE"]["trc"] == pytest.approx(90.0 / 80.0) and rows["PGE"]["pac"] == pytest.approx(80.0 / 40.0)


def test_years_loaded_by_the_old_rule_are_corrected_and_their_snapshots_replaced(tmp_path, monkeypatch, capsys):
    _benefit_year(tmp_path)
    monkeypatch.setattr(tables, "BENEFIT_PARTS", {"TRCBen": (), "PACBen": (), "RIMBen": ()})  # the old rule: ratio x cost
    legacy_claims.load(2019)
    assert _results(_version("PGE", 2019))["TRCBen"] == pytest.approx(100.0)
    old = Snapshot.objects.get(organisation__code="PGE")
    assert _by_pa("claims", metrics=("trc",))["PGE"]["trc"] == pytest.approx(100.0 / 80.0)
    monkeypatch.undo()

    call_command("load_legacy_claims", "--rebuild-benefits")
    out = capsys.readouterr().out
    assert "2019 PGE: 1 program results corrected" in out and "imports checked, 1 corrected" in out
    assert _results(_version("PGE", 2019))["TRCBen"] == pytest.approx(90.0)
    assert AuditEvent.objects.filter(action="legacy.benefits_rebuilt").count() == 1

    releases = Release.objects.filter(dataset="claims").count()
    call_command("snapshot_legacy", "--replace")
    assert "2 snapshots made, 0 failed" in capsys.readouterr().out
    assert not Snapshot.objects.filter(pk=old.pk).exists() and Release.objects.filter(dataset="claims").count() == releases + 1
    assert _by_pa("claims", metrics=("trc",))["PGE"]["trc"] == pytest.approx(90.0 / 80.0)
