"""Walk through a measure: the steps built from one measure's outputs, and the page that picks a measure of a run and shows them.

tests/data/walkthrough_measures.json holds three measures as the engine writes them (every numeric output column; the C3 regression job of
the engine's own tests, with the names and descriptions replaced): W1 a single-baseline lighting measure, W2 an early replacement with
two baselines, W3 a gas measure. Every piece of arithmetic a step shows must agree with the engine's value for the same output."""

import json
from pathlib import Path

import pytest
from django.test import Client

from cedars.core import storage
from cedars.modules.calculations import walkthrough as walk

from .conftest import page_only, sign_in
from .test_calculation_results import finished, web  # noqa: F401 - fixtures
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

cost_test_sets = pytest.importorskip("recet.cost_test_sets", reason="the walkthrough reads the run's cost-test set with the ReCET engine")
pytestmark = pytest.mark.django_db

MEASURES = json.loads((Path(__file__).parent / "data" / "walkthrough_measures.json").read_text())
SETTINGS = {"inputs": {"first_year": 2028}, "acc": {"settings_acc_version": 2024}, "config": {"market_effects": 0}}


def steps_for(i, settings=SETTINGS):
    return walk.build(MEASURES[i], settings, cost_test_sets.load("EE-2026"))


@pytest.mark.parametrize("i", range(len(MEASURES)))
def test_every_piece_of_arithmetic_agrees_with_the_engine(i):
    steps = steps_for(i)
    assert [s.key for s in steps] == walk.STEP_KEYS
    checked = walk.checks(steps)
    assert len(checked) >= 15
    assert [(c.label, c.result, c.output_value) for c in checked if not c.ok] == []


def test_the_steps_explain_a_lighting_measure():
    s = {st.key: st for st in steps_for(0)}
    savings = s["savings"].calcs[0]
    assert savings.expr == "1 unit × 185,086 kWh a unit × 0.56 net-to-gross × 1 installed × 0.9 realized"
    assert savings.result == "93,283 kWh" and savings.output == "AC_Net_kWh_1st_Baseline" and savings.ok
    assert s["life"].calcs[0].expr == "93,283 kWh a year × 12 years" and s["life"].calcs[0].result == "1,119,400 kWh"
    # discounting at PG&E's 7.66%, checked from the rate: the measure is claimed in Q1 of the First Year
    disc = {f.label: f.value for f in s["discounting"].facts}
    assert disc["Discount rate for PGE, a year"] == "7.66%"
    assert s["discounting"].table["rows"][0] == ["1 year", "0.929", "$92.89"]
    check = next(c for c in s["discounting"].calcs if c.label == "Checked from the discount rate")
    assert check.ok and check.result == "770,356 kWh"
    value = next(c for c in s["value"].calcs if c.output == "LevBenElec")
    assert value.result == "$0.1346 per kWh" and value.ok
    tests = {t.id: t for t in s["tests"].tests}
    assert list(tests) == ["TRC", "PAC", "RIM", "SCB", "SCH", "PCT"]
    assert tests["TRC"].ratio_text == "0.06" and tests["TRC"].verdict.startswith("does not pass")
    assert tests["TRC"].program_part == "$1,834,601"
    assert "2.22" in tests["TRC"].note  # the ratio before the program's own costs
    assert tests["PCT"].verdict.startswith("passes") and tests["PCT"].program_part == ""
    assert "Tax credits, in present value" in tests["PCT"].benefits_zero  # an internal stream with nothing left for it
    summary = " ".join(s["summary"].plain)
    assert "pulls it below 1" in summary
    assert s["summary"].notes[-1].startswith(f"{len(walk.checks(steps_for(0)))} of the {len(walk.checks(steps_for(0)))} pieces")


def test_an_early_replacement_has_two_baselines():
    s = {st.key: st for st in steps_for(1)}
    labels = [c.label for c in s["savings"].calcs]
    assert labels == ["Electricity, net savings in a year (first baseline)", "Electricity, net savings in a year (second baseline)"]
    assert any("early replacement" in n for n in s["savings"].notes)
    assert "Remaining useful life of the old equipment" in [f.label for f in s["life"].facts]
    assert not any(c.label == "Checked from the discount rate" for c in s["discounting"].calcs)  # shown, not re-derived


def test_a_gas_measure_is_valued_per_therm():
    s = {st.key: st for st in steps_for(2)}
    assert any(c.label == "Gas, net savings in a year" and c.ok for c in s["savings"].calcs)
    assert any(c.output == "LevBenGas" and c.ok for c in s["value"].calcs)


def test_without_the_run_settings_or_a_set_the_steps_still_show():
    steps = walk.build(MEASURES[0], {}, None)
    s = {st.key: st for st in steps}
    assert s["discounting"].table is None  # no rate to show
    assert [t.id for t in s["tests"].tests] == ["TRC", "PAC", "RIM"]
    assert not [c for c in walk.checks(steps) if not c.ok]


def test_a_supplied_run_parameters_file_is_named_not_guessed():
    s = {st.key: st for st in steps_for(0, {**SETTINGS, "supplied_files": {"run_parameters": {"path": "rp.csv"}}})}
    assert s["discounting"].table is None and any("supplied its own run parameters" in n for n in s["discounting"].notes)


def _with_measures(run, rows):
    """Replace the run's kept measure-level parquet with these rows (the fake worker's has only a few columns)."""
    import pandas as pd  # with the engine only (the accessibility job runs without it)

    key = walk._measure_key(run, "")
    storage.store("results").put_bytes(key, b"")
    path = storage.store("results").path(key)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return run


def test_pick_a_measure_then_walk_through_it(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    run = finished(analyst, pge, release, tmp_path)
    page = web.get(f"/calculations/runs/{run.pk}/walkthrough/")
    html = page_only(page.content.decode())
    assert page.status_code == 200 and "Find a measure" in html and "measure 5" in html and "6 measures" not in html
    assert "Measures of this run: 6" in html

    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"q": "com_ltg"}).content.decode())
    assert 'Measures matching "com_ltg": 3' in html and "measure 0" not in html

    page = web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "M2"})
    html = page_only(page.content.decode())
    assert page.status_code == 200 and "Walking through M2" in html and "Step 1 of 10: The measure" in html
    assert 'aria-current="step"' in html and "Next: Energy saved in a year" in html

    page = web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "M2", "step": "tests"})
    html = page_only(page.content.decode())
    assert "Step 9 of 10: The cost tests" in html and "Previous: Its share" in html

    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "M2", "all": "1"}).content.decode())
    assert all(f"Step {i} of 10" in html for i in range(1, 11)) and "One step at a time" in html

    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "nope"}).content.decode())
    assert "This run has no measure with the ID nope" in html and "Find a measure" in html

    # linked from the run page and from Results
    assert f"/calculations/runs/{run.pk}/walkthrough/" in web.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert f"/calculations/runs/{run.pk}/walkthrough/" in web.get(f"/calculations/runs/{run.pk}/results/").content.decode()


def test_a_full_measure_shows_its_arithmetic_beside_the_outputs(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    run = _with_measures(finished(analyst, pge, release, tmp_path), MEASURES)
    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "W1", "step": "savings"}).content.decode())
    assert "1 unit × 185,086 kWh a unit × 0.56 net-to-gross × 1 installed × 0.9 realized" in html
    assert "Agrees</span> with the engine's <code>AC_Net_kWh_1st_Baseline</code>: 93,283 kWh" in html
    assert "Differs" not in html
    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "W1", "step": "tests"}).content.decode())
    assert "Total Resource Cost (TRC): 0.06, does not pass" in html and "Participant Cost Test (PCT): 5.04, passes" in html


def test_the_walkthrough_follows_run_visibility_and_state(analyst, pge, release, worker, tmp_path, make_user, web):  # noqa: F811
    run = finished(analyst, pge, release, tmp_path)
    outsider = Client()
    sign_in(outsider, make_user("someone@example.com"))
    assert outsider.get(f"/calculations/runs/{run.pk}/walkthrough/").status_code == 404
    from cedars.modules.calculations import service

    queued = service.create_run(analyst, pge, request(release), job_zip(tmp_path, name="queued.zip"))
    html = web.get(f"/calculations/runs/{queued.pk}/walkthrough/").content.decode()
    assert "once the run has finished" in html


def test_without_the_engine_the_page_says_so(analyst, pge, release, worker, tmp_path, web, monkeypatch):  # noqa: F811
    run = finished(analyst, pge, release, tmp_path)
    from cedars.modules.calculations import results

    monkeypatch.setattr(results, "engine", lambda: None)
    html = web.get(f"/calculations/runs/{run.pk}/walkthrough/").content.decode()
    assert "ReCET engine package, which is not installed" in html


# A run on a CET replica: tests/data/walkthrough_replica_measures.json holds five measures as the replicas of CET 25.1, 24.1, 21.0 and 25.0
# write them (the engine's own replica reference jobs, names replaced), each with the run settings that priced it.
REPLICA = json.loads((Path(__file__).parent / "data" / "walkthrough_replica_measures.json").read_text())


def replica_steps(i):
    e = REPLICA[i]
    return walk.build(e["row"], e["settings"], None, engine=walk.Engine.of(None, e["settings"]))


@pytest.mark.parametrize("i", range(len(REPLICA)))
def test_on_a_replica_the_arithmetic_agrees_or_names_the_cet_error(i):
    steps = replica_steps(i)
    assert [s.key for s in steps] == walk.STEP_KEYS
    checked = walk.checks(steps)
    assert len(checked) >= 10
    assert [(c.label, c.result, c.output_value) for c in checked if not c.ok and not c.known] == []


def test_a_replica_run_is_explained_as_that_cet_release():
    s = {st.key: st for st in replica_steps(0)}  # CET 25.1, an early replacement
    assert any(f.value == "CET 25.1 replica, CET Version 2026" for f in s["measure"].facts)
    assert any("CET 25.1's own calculation reproduced exactly" in p for p in s["measure"].plain)
    assert "quarterly avoided-cost tables" in s["timing"].plain[0]
    # the first-year savings are checked against the release's own column name
    first = s["savings"].calcs[0]
    assert first.output == "FirstYearNetkWhSite" and first.ok and first.result == "93,283 kWh"
    # the discount rate is the release's own, for the measure's PA at the run's CET Version
    assert {f.label: f.value for f in s["discounting"].facts}["Discount rate for PGE, a year"] == "7.66%"
    # CET 25.x writes LevBenElec from the wrong position: labeled, not marked as a disagreement
    lev = next(c for c in s["value"].calcs if c.output == "LevBenElec")
    assert lev.ok is False and "wrong place" in lev.known
    tests = {t.id: t for t in s["tests"].tests}
    assert list(tests) == ["TRC", "PAC", "RIM", "SCB", "SCH"]  # CET's own tests: no PCT
    assert all(c.ok for t in tests.values() for c in t.calcs if c.label == "Ratio")
    assert "no column for its benefits" in s["tests"].notes[-1]
    assert "because of an error CET 25.1 makes" in s["summary"].notes[-1]


def test_older_replicas_use_their_own_names_and_rules():
    s = {st.key: st for st in replica_steps(2)}  # CET 24.1, an early replacement: lifetime kWh times the life again
    life = next(c for c in s["life"].calcs if c.output == "LifecycleNetkWhSite")
    assert life.ok is False and "a second time" in life.known
    s = {st.key: st for st in replica_steps(3)}  # CET 21.0 with 5% market effects, claimed in Q3
    savings = s["savings"].calcs[0]
    assert "net-to-gross with market effects" in savings.expr and savings.output == "FirstYearNetKWh" and savings.ok
    check = next(c for c in s["discounting"].calcs if c.label == "Checked from the discount rate")
    assert check.ok and "as CET 21.0 does" in check.expr
    assert [t.id for t in s["tests"].tests] == ["TRC", "PAC", "RIM"]
    assert s["tests"].tests[2].verdict == "not priced"  # CET 21.0 writes a RIM cost but no ratio
    s = {st.key: st for st in replica_steps(4)}  # CET 25.0, a fuel substitution: no societal gas column
    scb = next(t for t in s["tests"].tests if t.id == "SCB")
    assert not any(c.label == "Ratio" for c in scb.calcs) and "has no column of its own" in scb.calcs[0].expr


def test_a_replica_run_page(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    from cedars.modules.calculations.models import Run

    run = _with_measures(finished(analyst, pge, release, tmp_path), [e["row"] for e in REPLICA[:2]])
    Run.objects.filter(pk=run.pk).update(calculation="cet-25.1")
    key = run.storage_prefix + "out/run_settings.json"
    storage.store("results").put_bytes(key, json.dumps(REPLICA[0]["settings"]).encode())
    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "R1", "step": "value"}).content.decode())
    assert "Differs, a known CET error</span> from the replica's <code>LevBenElec</code>" in html
    html = page_only(web.get(f"/calculations/runs/{run.pk}/walkthrough/", {"measure": "R1"}).content.decode())
    assert "CET 25.1 replica" in html
