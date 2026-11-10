"""The walkthrough's charts (walkchart.py): how a measure's savings, their discounting and their value build up over its life.

Steps 3 and 5 are drawn from the measure's own outputs (tests/data/walkthrough_measures.json) and must add up to the engine's lifetime and
discounted savings. Steps 4 and 6 read the run's packs: here a load-shape library and an electric ACC written for the test (a flat-ish
shape, prices that rise each year), registered as file:// packs as `register_pack` does on a server. The engine's own per-measure
pricing is the oracle for the electric benefit those packs give; the chart is drawn only when its years add up to the row's ElecBen.
(On the engine's 1,270-measure regression job and its sliced reference data, every measure's years add up to its ElecBen or, for added
load, its ElecSupplyCost when it is a fuel substitution, else a negative ElecBen.)"""

import copy
import datetime
import json
from pathlib import Path

import pytest

from cedars.core import storage
from cedars.modules.calculations import walkchart
from cedars.modules.calculations import walkthrough as walk
from cedars.services.refdata import acc as packed_acc
from cedars.services.refdata import service as refdata

from .conftest import page_only
from .test_calculation_results import finished, web  # noqa: F401 - fixtures
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

pytest.importorskip("recet.pipeline.engine", reason="the charts price with the ReCET engine")
np = pytest.importorskip("numpy")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytestmark = pytest.mark.django_db

MEASURES = json.loads((Path(__file__).parent / "data" / "walkthrough_measures.json").read_text())
SETTINGS = {"inputs": {"first_year": 2028}, "acc": {"settings_acc_version": 2024}, "config": {"market_effects": 0}}
YEARS = range(2024, 2046)


def w_of(i, settings=SETTINGS):
    return walk.Walk(MEASURES[i], settings, None, 1, walk.RECET)


@pytest.mark.parametrize("i", range(len(MEASURES)))
def test_savings_and_discounting_add_up_to_the_engine_year_by_year(i):
    w = w_of(i)
    fuel = walkchart._fuel(w)
    life, disc = walkchart.life_years(w), walkchart.discount_years(w)
    assert life and disc and len(life) == len(disc) == int(float(MEASURES[i]["EUL_Yrs"]))
    assert sum(r["saved"] for r in life) == pytest.approx(w.g(fuel[3]), rel=1e-9)
    assert sum(r["counted"] for r in disc) == pytest.approx(w.g(fuel[4]), rel=1e-9)
    assert life[0]["starts"] == "Q" + MEASURES[i]["ClaimYearQuarter"][-1] + " " + MEASURES[i]["ClaimYearQuarter"][:4]


def test_an_early_replacement_switches_baseline_after_its_remaining_life():
    life = walkchart.life_years(w_of(1))  # W2: RUL 4 of EUL 12
    assert [r["first"] for r in life[:5]] == [1, 1, 1, 1, 0]
    assert life[0]["saved"] != life[4]["saved"]
    chart = walkchart.life_chart(w_of(1))
    assert ["Year of life", "Starts", "Baseline"] == chart.table["columns"][:3]
    assert [r[2] for r in chart.table["rows"][3:5]] == ["first", "second"]


def test_a_chart_is_left_out_when_its_years_do_not_add_up():
    row = dict(MEASURES[0], LifecycleNetKWhSite=1.0, DiscountedSavingsNetkWh=1.0)
    w = walk.Walk(row, SETTINGS, None, 1, walk.RECET)
    assert walkchart.life_years(w) is None and walkchart.discount_years(w) is None
    steps = walk.build(row, SETTINGS)
    assert all(not s.charts for s in steps)


def test_the_steps_carry_their_charts_and_the_charts_their_numbers():
    steps = {s.key: s for s in walk.build(MEASURES[0], SETTINGS)}
    (life,) = steps["life"].charts
    assert life.legend == [("s1", "Saved in the years before"), ("s2", "Saved that year")]
    assert len(life.table["rows"]) == 12 and life.table["rows"][-1][-1] == walk.quantity(MEASURES[0]["LifecycleNetKWhSite"])
    assert "the last column is the lifetime total" in life.summary
    assert all(m["d"].startswith("M") for m in life.marks) and life.yticks[0][1] == "0"
    (disc,) = steps["discounting"].charts
    assert [d["label"] for d in sorted(disc.dots, key=lambda d: d["cls"])] == [
        walk.quantity(MEASURES[0]["LifecycleNetKWhSite"], "kWh"),
        walk.quantity(MEASURES[0]["DiscountedSavingsNetkWh"], "kWh"),
    ]
    assert steps["timing"].charts == [] and steps["value"].charts == []  # no packs given


def test_every_year_of_a_chart_has_its_tooltip():
    steps = {s.key: s for s in walk.build(MEASURES[0], SETTINGS)}
    (life,) = steps["life"].charts
    (disc,) = steps["discounting"].charts
    for chart in (life, disc):
        assert len(chart.slots) == len(chart.table["rows"]) == 12
        xs = [s["x"] for s in chart.slots]
        assert xs[0] == chart.plot[0] and xs == sorted(xs)
        assert xs[-1] + chart.slots[-1]["w"] == pytest.approx(chart.plot[2], abs=0.02)  # the bands cover the plot, end to end
    last = life.slots[-1]
    assert last["title"].startswith("Year 12 (starts ")
    assert last["rows"][-1] == {"cls": "", "text": "Running total: " + walk.quantity(MEASURES[0]["LifecycleNetKWhSite"], "kWh")}
    assert [r["cls"] for r in last["rows"]] == ["s2", "s1", ""] and last["text"].endswith(".")
    # A line chart marks each series' point in the year, where the line is drawn.
    assert [p["cls"] for p in disc.slots[-1]["points"]] == ["s1", "s2"]
    ends = {d["cls"]: (d["x"], d["y"]) for d in disc.dots}
    assert all((p["x"], p["y"]) == ends[p["cls"]] for p in disc.slots[-1]["points"])
    assert disc.slots[-1]["rows"][1]["text"].endswith(walk.quantity(MEASURES[0]["DiscountedSavingsNetkWh"], "kWh"))
    assert " × " in disc.slots[1]["rows"][2]["text"]


def test_a_replica_run_draws_savings_and_discounting_only():
    e = json.loads((Path(__file__).parent / "data" / "walkthrough_replica_measures.json").read_text())[0]
    steps = {s.key: s for s in walk.build(e["row"], e["settings"], None, engine=walk.Engine.of(None, e["settings"]))}
    assert steps["life"].charts
    assert steps["timing"].charts == [] and steps["value"].charts == []
    assert walkchart.pack_series(None, "", e["row"], e["settings"], walk.Engine.of(None, e["settings"]))["why"] == "replica"


# --- steps 4 and 6, from the run's packs


def _shape():
    hours = np.arange(8760)
    shape = 1.0 + 0.5 * np.sin(2 * np.pi * (hours % 24) / 24) + 0.25 * np.cos(2 * np.pi * hours / 8760)
    return shape / shape.sum()


def _price(year):
    hours = np.arange(8760)
    return 40.0 + 2.0 * (year - 2024) + 30.0 * ((hours % 24) >= 16) * ((hours % 24) < 21)  # $/MWh, an evening peak


def _write_packs(tmp_path):
    lib = tmp_path / "impact_profiles"
    lib.mkdir()
    pq.write_table(
        pa.table(
            {
                "end_use_name": ["8 = Industrial Indoor Lighting", "8 = Industrial Indoor Lighting"],
                "iou_ac_territory": ["PGE", "SCE"],
                "target_sector": ["INDUSTRIAL", "INDUSTRIAL"],
                "profile_version": [1, 1],
                "year_represented": [None, None],
                "shape_id": [7, 8],
            }
        ),
        lib / "catalog.parquet",
    )
    with pq.ParquetWriter(
        lib / "hourly.parquet",
        pa.schema(
            [("impact_profile_id", pa.int32()), ("climate_zone", pa.string()), ("hour_of_year", pa.int16()), ("impact", pa.float64())]
        ),
    ) as wr:
        for pid in (7, 8):
            wr.write_table(
                pa.table(
                    {
                        "impact_profile_id": np.full(8760, pid, dtype=np.int32),
                        "climate_zone": ["13"] * 8760,
                        "hour_of_year": np.arange(1, 8761, dtype=np.int16),
                        "impact": _shape(),
                    }
                )
            )
    accdir = tmp_path / "acc"
    accdir.mkdir()
    start = datetime.datetime(2018, 1, 1)
    stamps = [start + datetime.timedelta(hours=h) for h in range(8760)]
    schema = pa.schema(
        [
            ("iou", pa.string()),
            ("climate_zone", pa.string()),
            ("cost_test", pa.string()),
            ("year", pa.int16()),
            ("hour_of_year", pa.int32()),
            ("datetime", pa.timestamp("ms")),
            ("total_avoided_cost", pa.float64()),
        ]
    )
    with pq.ParquetWriter(accdir / "electric_hourly.parquet", schema) as wr:
        for iou, cz in (
            ("PG&E", "CZ12"),
            ("PG&E", "CZ13"),
            ("SCE", "CZ9"),
        ):  # one row group per (utility, zone, test), as the extraction writes
            for test, bump in (("TRC", 0.0), ("SCT Base", 100.0)):
                n = len(YEARS) * 8760
                wr.write_table(
                    pa.table(
                        {
                            "iou": [iou] * n,
                            "climate_zone": [cz] * n,
                            "cost_test": [test] * n,
                            "year": np.repeat(np.array(list(YEARS), dtype=np.int16), 8760),
                            "hour_of_year": np.tile(np.arange(1, 8761, dtype=np.int32), len(YEARS)),
                            "datetime": stamps * len(YEARS),
                            "total_avoided_cost": np.concatenate([_price(y) + bump + (5.0 if cz != "CZ13" else 0.0) for y in YEARS]),
                        },
                        schema=schema,
                    )
                )
    manifest = lambda d: {p.name: {"sha256": "0" * 64, "bytes": p.stat().st_size} for p in d.iterdir()}  # noqa: E731
    refdata.register_pack(None, kind="impact_profiles", label="Impact profiles", manifest=manifest(lib), storage_uri=lib.as_uri())
    return refdata.register_pack(
        None, kind="acc_electric", label="2024 ACC", acc_year=2024, manifest=manifest(accdir), storage_uri=accdir.as_uri()
    )


def _elec_ben(row):
    """The electric benefit the engine's own per-measure pricing gives on the test's packs."""
    import pandas as pd
    from recet.pipeline import engine
    from recet.pipeline import real_impact_profiles as rip

    rates = walk.discount_rates(row, SETTINGS)
    m = pd.DataFrame(
        [
            {
                "CEInputID": row["CEInputID"],
                "Used_Year": int(row["Used_Year"]),
                "Used_Quarter": int(row["Used_Quarter"]),
                "EUL_Yrs": float(row["EUL_Yrs"]),
                "RUL_Yrs": float(row["RUL_Yrs"]),
                "Priced_Quantity_1st_Baseline": float(row["AC_Net_kWh_1st_Baseline"]),
                "Priced_Quantity_2nd_Baseline": float(row["AC_Net_kWh_2nd_Baseline"]),
            }
        ]
    )
    res = engine.price_real_hourly_per_measure(
        m,
        lambda _: (_shape(), 2009),
        lambda _: {y: _price(y) / 1000 for y in YEARS},
        lambda s, y, r: rip.shift_to_acc(s, r, 2018),
        lambda _: rates["DiscountRateQtr"],
        2028,
    )
    return float(res.values["QuarterValuePV"].sum())


def test_one_series_is_read_from_its_own_row_groups(tmp_path):
    pack = _write_packs(tmp_path)
    s = packed_acc.hourly(pack, "PG&E", "CZ13", "TRC")
    assert s.calendar_year == 2018 and list(s.years) == list(YEARS)
    assert np.allclose(s.years[2030], _price(2030) / 1000)
    assert packed_acc.hourly(pack, "SDG&E", "CZ7") is None and packed_acc.hourly(pack, "PG&E", "CZ13", column="nope") is None


def test_the_load_shape_and_the_value_charts_from_the_runs_packs(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    _write_packs(tmp_path)
    run = finished(analyst, pge, release, tmp_path)
    assert run.spec["context"]["packs"]["acc_electric"]["label"] == "2024 ACC"  # pinned when the run was queued
    rows = [dict(r) for r in MEASURES]
    for r in rows[:2]:
        r["ElecBen"] = _elec_ben(r)
    rows[1]["ElecBen"] *= 1.01  # this one does not add up: its value chart is left out
    from .test_calculation_walkthrough import _with_measures

    _with_measures(run, rows)
    settings = walk.run_settings(run, None)
    settings.update(SETTINGS)
    data = walkchart.pack_series(run, "", rows[0], settings, walk.RECET)
    assert data["shape"]["zone"] == "13" and data["shape"]["calendar"] == 2009
    assert sum(data["shape"]["months"]) == pytest.approx(1) and sum(data["shape"]["hours"]) == pytest.approx(1)
    assert sum(q["pv"] for q in data["priced"]["quarters"]) == pytest.approx(rows[0]["ElecBen"], rel=1e-9)
    shifted = _shape()[(np.arange(8760) - 72) % 8760]  # 2009 starts on a Thursday, 2018 on a Monday: moved three days
    assert data["priced"]["per_kwh"][0]["value"] == pytest.approx(float((shifted * _price(2028)).sum() / 1000), rel=1e-9)
    assert storage.store("results").exists(walkchart._cache_key(run, "", "W1"))  # kept beside the run's outputs
    assert walkchart.pack_series(run, "", rows[1], settings, walk.RECET)["why"] == "does not add up"

    steps = {s.key: s for s in walk.build(rows[0], settings, None, 1, walk.RECET, data)}
    assert [c.key for c in steps["timing"].charts] == ["shape-months", "shape-hours"]
    value, worth = steps["value"].charts
    assert value.table["rows"][-1][-1] == walk.dollars(rows[0]["ElecBen"])
    assert worth.table["rows"][0][2] == "2028" and len(worth.table["rows"]) == 12  # 2028 to 2039: a 12-year life claimed in Q1

    copy.deepcopy(steps)  # plain data, kept as JSON
    page = web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "W1", "step": "value"})
    html = page_only(page.content.decode())
    assert page.status_code == 200 and "The electric benefit building up, year by year" in html
    assert 'role="img"' in html and "The numbers in this chart" in html and "What a kWh saved in each year is worth" in html
    # Each year's tooltip: a band to point at (its values as the <title> without scripts), its words for the script, and the script.
    assert html.count('class="hit"') == len(value.slots) + len(worth.slots) and html.count('class="band"') == html.count('class="hit"')
    assert 'class="walk-tips" hidden' in html and "to see each year's values" in html
    assert "calculations/walkthrough.js" in page.content.decode()
    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "W1", "step": "timing"}).content.decode())
    assert "Share of the year&#x27;s savings in each month" in html and "Share of the year&#x27;s savings in each hour of the day" in html
    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "W2", "step": "value"}).content.decode())
    assert "building up, year by year" not in html and "Step 6 of 10" in html


def test_added_load_is_charted_by_its_size_as_a_supply_cost_or_a_negative_benefit():
    w = w_of(0)
    quarters = [{"year": 2028, "quarter": q, "pv": -10.0, "value": -10.0} for q in range(1, 5)]
    cost = walkchart.value_charts(w, {"priced": {"quarters": quarters, "per_kwh": [], "total": -40.0, "cost": True}})
    negative = walkchart.value_charts(w, {"priced": {"quarters": quarters, "per_kwh": [], "total": -40.0, "cost": False}})
    assert cost[0].title.startswith("The cost of supplying the added electricity") and cost[0].table["rows"][-1][-1] == "$40.00"
    assert negative[0].title.startswith("The value of the added electricity use (a negative electric benefit)")
