"""New run's engine options (ReCET UI parity PR 2): an ACC comparison, the First Year, market effects, ACC components, the combined
8,760 and its years, Exclude_From_CE in the summary, named filings under "Priced for", the "Which ACC applies" table, and the ACC
tabs a comparison run gets on Results."""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils.html import escape

from cedars.modules.calculations import dispatch, service, views
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied
from cedars.services.refdata import service as refdata

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures
from .test_calculations_api import bearer, key  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


@pytest.fixture
def web(analyst):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    return c


def post_new(web, tmp_path, **fields):
    data = {
        "name": "options",
        "run_for": "PGE",
        "job": SimpleUploadedFile("job.zip", job_zip(tmp_path).read_bytes(), content_type="application/zip"),
        "engine": "0.1.78|recet",
        "acc_version": "2024",
        "market_effects": "0",
        "output_format": "csv.zip",
        **fields,
    }
    return web.post("/calculations/new/", data)


def test_new_run_sends_the_engine_options(web, tmp_path, release):  # noqa: F811
    page = web.get("/calculations/new/").content.decode()
    for label in (
        "Compare with another ACC",
        "First Year (optional)",
        "Market effects",
        "Price each ACC component separately",
        "Combined 8,760 electric load profile",
        "Leave Exclude_From_CE programs out",
    ):
        assert label in page
    r = post_new(
        web,
        tmp_path,
        compare_with="2026",
        base_year="2028",
        market_effects="0.05",
        components="on",
        apply_exclude_from_ce="on",
        portfolio_8760="on",
        profile_years="2029, 2028",
    )
    assert r.status_code == 302, r.content.decode()[:2000]
    run = Run.objects.get()
    want = {
        "compare_with": "2026",
        "base_year": 2028,
        "market_effects": 0.05,
        "components": True,
        "apply_exclude_from_ce": True,
        "portfolio_8760": True,
        "profile_years": [2028, 2029],
    }
    assert {k: run.settings.get(k) for k in want} == want
    assert {k: run.spec["settings"].get(k) for k in want} == want
    shown = dict(views.settings_view(run.settings))
    assert "5%" in shown.values() and "2026 ACC" in shown.values() and "2028, 2029" in shown.values()


def test_the_engines_defaults_are_not_sent(web, tmp_path, release):  # noqa: F811
    assert post_new(web, tmp_path).status_code == 302
    st = Run.objects.get().settings
    for k in ("compare_with", "base_year", "market_effects", "components", "apply_exclude_from_ce", "portfolio_8760", "profile_years"):
        assert k not in st


def test_new_run_refuses_options_that_do_not_fit(web, tmp_path, release):  # noqa: F811
    html = post_new(web, tmp_path, compare_with="2024").content.decode()
    assert "Choose an ACC other than the run&#x27;s own" in html
    html = post_new(web, tmp_path, profile_years="2028").content.decode()
    assert "Tick Combined 8,760 electric load profile" in html
    html = post_new(web, tmp_path, portfolio_8760="on", profile_years="next year").content.decode()
    assert "four-digit numbers" in html
    assert not Run.objects.exists()


def test_the_service_checks_the_options(tmp_path, analyst, pge, release):  # noqa: F811
    with pytest.raises(AccessDenied, match="names the run's own ACC"):
        service.create_run(analyst, pge, request(release, settings={"compare_with": "2024"}), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="no 2030 ACC to compare with"):
        service.create_run(analyst, pge, request(release, settings={"compare_with": "2030"}), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="0% or 5%"):
        service.create_run(analyst, pge, request(release, settings={"market_effects": 0.1}), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="only when the combined profile"):
        service.create_run(analyst, pge, request(release, settings={"profile_years": [2028]}), job_zip(tmp_path))
    for refused in ({"compare_with": "2026"}, {"components": True}, {"portfolio_8760": True}):
        with pytest.raises(AccessDenied, match="A CET replica does not take"):
            service.create_run(analyst, pge, request(release, calculation="cet-25.1", settings=refused), job_zip(tmp_path))


def test_the_compared_accs_pack_is_pinned(tmp_path, analyst, pge, release, cpuc_admin):  # noqa: F811
    folder = tmp_path / "acc2026"
    folder.mkdir()
    (folder / "acc_2026.parquet").write_bytes(b"2026")
    refdata.register_pack(  # a draft: an analysis run takes it
        cpuc_admin,
        kind="acc_electric",
        label="2026 ACC",
        version="v1b",
        acc_year=2026,
        valid_from=date(2027, 1, 1),
        manifest=refdata.manifest_for(folder),
        storage_uri="s3://refdata/acc/2026-v1b/",
    )
    run = service.create_run(analyst, pge, request(release, settings={"compare_with": "2026"}), job_zip(tmp_path))
    assert "acc_electric:2026" in run.spec["context"]["packs"]
    plain = service.create_run(analyst, pge, request(release, name="plain"), job_zip(tmp_path, name="b.zip"))
    assert "acc_electric:2026" not in (plain.spec["context"].get("packs") or {})


def test_priced_for_names_each_budget_filing_and_the_acc_table_shows(web, release):  # noqa: F811
    pytest.importorskip("recet.versions")
    choices = dict(service.acc_purposes())
    assert choices[""] == "Any filing" and choices["claims"] == "Claims" and len(choices) > 2  # and the named filings
    rows = service.acc_table([release])
    assert {r["acc"] for r in rows if r["available"]} == {2021, 2022, 2024, 2026}
    page = web.get("/calculations/new/").content.decode()
    assert 'id="acc-help"' in page and "Which ACC applies to my filing?" in page
    for value, label in choices.items():
        if value:
            assert f'value="{value}"' in page and escape(label) in page


def test_the_api_takes_the_options(key, release, tmp_path):  # noqa: F811
    with open(job_zip(tmp_path), "rb") as f:
        r = Client().post(
            "/api/v1/calculations/runs",
            {
                "job": f,
                "name": "api options",
                "acc_version": "2024",
                "compare_with": "2026",
                "base_year": 2028,
                "market_effects": 0.05,
                "components": True,
                "portfolio_8760": True,
                "profile_years": [2029, 2028],
            },
            **bearer(key),
        )
    assert r.status_code == 201, r.content
    st = Run.objects.get().settings
    assert st["compare_with"] == "2026" and st["base_year"] == 2028 and float(st["market_effects"]) == 0.05
    assert st["components"] is True and st["portfolio_8760"] is True and st["profile_years"] == [2028, 2029]


# --- Results of a run compared with another ACC


def test_a_comparison_run_has_a_results_tab_for_each_acc_and_the_comparison(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    pytest.importorskip("recet.results_view")
    service.create_run(analyst, pge, request(release, settings={"compare_with": "2026"}), job_zip(tmp_path))
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    assert run.status == Run.Status.SUCCEEDED
    names = [f["name"] for f in service.output_files(run)]
    assert "comparison_summary_acc2024_vs_acc2026.csv" in names and not any("/_results/" in n for n in names)

    own = web.get(f"/calculations/runs/{run.pk}/results/").content.decode()
    assert "2024 ACC (the run&#x27;s own)" in own and "?acc=2026" in own and "?acc=comparison" in own
    assert "$23,100" in own  # the run's own ACC: 1,100 x (1 + ... + 6)
    other = web.get(f"/calculations/runs/{run.pk}/results/", {"acc": "2026"}).content.decode()
    assert "$25,410" in other and 'name="acc" value="2026"' in other  # the other ACC's tables (x 1.1), kept on every form
    assert "acc=2026&amp;level=" in other  # the column picker's Default columns link keeps it too
    comparison = web.get(f"/calculations/runs/{run.pk}/results/", {"acc": "comparison"}).content.decode()
    assert 'id="comparison-h"' in comparison and "TotalSystemBenefit" in comparison and "2,310" in comparison
    r = web.get(f"/calculations/runs/{run.pk}/results.csv", {"acc": "2026", "level": "program"})
    assert r.status_code == 200 and "_acc2026" in r["Content-Disposition"]
    # an ACC the run does not have falls back to the run's own
    assert "$23,100" in web.get(f"/calculations/runs/{run.pk}/results/", {"acc": "2030"}).content.decode()


def test_a_plain_run_has_no_acc_tabs(analyst, pge, release, worker, tmp_path, web):  # noqa: F811
    pytest.importorskip("recet.results_view")
    service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    html = web.get(f"/calculations/runs/{run.pk}/results/").content.decode()
    assert "?acc=comparison" not in html and 'name="acc"' not in html
