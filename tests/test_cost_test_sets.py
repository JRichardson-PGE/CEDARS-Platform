"""Cost-test definition sets as data packs (kind cost_tests): registered from a ReCET set file, published once the engine check passes,
chosen by program year, pinned by every ReCET run (a published set by its id, a draft as the file itself, analysis runs only), checked
against the receipt when the run ends, and required by a filing's setup."""

import hashlib
from datetime import date

import pytest
import yaml
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from cedars.core import storage
from cedars.modules.budgets import service as budgets
from cedars.modules.calculations import service
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied
from cedars.services.refdata import cost_tests
from cedars.services.refdata import service as refdata

from .conftest import page_only, sign_in
from .test_budgets import filer, filing, group_sums, job, programs  # noqa: F401 - fixtures
from .test_calculations import analyst, job_zip, release, request  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


def set_file(sid="EE-2026", version="2026-09-15", years=(None, None), draft=False, tests=("TRC", "PAC")) -> bytes:
    """A cost-test set as ReCET writes one (the header and tests; the platform reads no more of it)."""
    d = {
        "schema_version": 1,
        "id": sid,
        "workstream": sid.split("-")[0],
        "version": version,
        "label": f"EE cost tests {sid[3:7]}",
        "draft": draft,
        "program_years": {"from": years[0], "to": years[1]},
        "tests": {t: {"label": f"{t} test", "pricing": "primary", "headline": t == "TRC"} for t in tests},
    }
    return yaml.safe_dump(d, sort_keys=False).encode()


def registered(admin, publish=True, **kw):
    p = cost_tests.register_set(admin, set_file(**kw), source="test")
    if publish:
        refdata.record_tie_out(admin, p, check="engine_check", passed=True, engine_release="0.1.83")
        p = refdata.publish_pack(admin, p)
    return p


def test_a_set_file_is_a_draft_pack_of_that_one_file(cpuc_admin):
    data = set_file(years=(2028, None), sid="EE-2028")
    p = cost_tests.register_set(cpuc_admin, data)
    assert (p.kind, p.label, p.version, p.status, p.valid_from) == ("cost_tests", "EE-2028", "2026-09-15", "draft", date(2028, 1, 1))
    assert cost_tests.sha256(p) == hashlib.sha256(data).hexdigest() and list(p.manifest) == ["definition.yaml"]
    assert cost_tests.header(p)["tests"] == {"TRC": "TRC test", "PAC": "PAC test"} and cost_tests.header(p)["headline"] == ["TRC"]
    assert cost_tests.file_bytes(p) == data and cost_tests.years_text(p) == "2028 on (until a later set starts)"
    with pytest.raises(AccessDenied, match="already registered"):
        cost_tests.register_set(cpuc_admin, data)
    with pytest.raises(cost_tests.SetError, match="id"):
        cost_tests.register_set(cpuc_admin, b"id: not a set\ntests: {}\n")


def test_publishing_needs_the_engine_check_and_a_final_file(cpuc_admin, pge_admin):
    p = cost_tests.register_set(cpuc_admin, set_file())
    with pytest.raises(AccessDenied, match="engine_check"):
        refdata.publish_pack(cpuc_admin, p)
    refdata.record_tie_out(cpuc_admin, p, check="engine_check", passed=True)
    assert refdata.publish_pack(cpuc_admin, p).status == "published"
    d = cost_tests.register_set(cpuc_admin, set_file(version="2027-draft", draft=True))
    refdata.record_tie_out(cpuc_admin, d, check="engine_check", passed=True)
    with pytest.raises(AccessDenied, match="draft: true"):
        refdata.publish_pack(cpuc_admin, d)
    with pytest.raises(AccessDenied):
        cost_tests.register_set(pge_admin, set_file(version="x"))  # a PA does not register the CPUC's sets


def test_the_set_in_force_follows_the_program_year(cpuc_admin):
    assert cost_tests.for_year(2028) is None
    draft = registered(cpuc_admin, publish=False)
    assert cost_tests.for_year(2028) is None and cost_tests.for_year(2028, include_drafts=True) == draft
    ee26 = registered(cpuc_admin, version="v2")
    ee28 = registered(cpuc_admin, sid="EE-2028", years=(2028, None))
    assert cost_tests.for_year(2027) == ee26 and cost_tests.for_year(2028) == ee28 and cost_tests.for_year(2031) == ee28
    assert cost_tests.for_year(2028, workstream="IQP") is None


def test_a_run_is_priced_on_the_published_set_in_force_by_its_id(analyst, pge, release, cpuc_admin, tmp_path):  # noqa: F811
    plain = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert "cost_test_set" not in plain.settings and plain.cost_tests == {}
    ee = registered(cpuc_admin)
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert run.settings["cost_test_set"] == "EE-2026" and run.spec["settings"]["cost_test_set"] == "EE-2026"
    assert "cost_tests" not in run.inputs and "cost_tests" not in run.spec["inputs"]
    assert run.cost_tests == {
        "id": "EE-2026",
        "version": "2026-09-15",
        "status": "published",
        "sha256": cost_tests.sha256(ee),
        "pack": ee.pk,
    }
    assert run.spec["context"]["packs"]["cost_tests"]["fingerprint"] == ee.fingerprint and run.input_hash != plain.input_hash
    try:
        from recet import jobspec
    except ImportError:
        pass
    else:
        assert jobspec.validate(run.spec) == []
    c = Client()
    sign_in(c, analyst)
    assert "EE-2026 2026-09-15" in c.get(f"/calculations/runs/{run.pk}/").content.decode()


def test_a_draft_set_goes_as_its_file_and_only_for_analysis(analyst, pge, release, cpuc_admin, tmp_path):  # noqa: F811
    draft = registered(cpuc_admin, publish=False, version="2027-draft")
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    f = run.inputs["cost_tests"]
    assert f["sha256"] == cost_tests.sha256(draft) and storage.store("uploads").get_bytes(f["key"]) == cost_tests.file_bytes(draft)
    assert run.spec["inputs"]["cost_tests"]["sha256"] == f["sha256"] and "cost_test_set" not in run.spec["settings"]
    assert run.cost_tests["status"] == "draft"
    for settings in ({"acc_purpose": "claims"}, {"filing_validation": "claims"}):
        with pytest.raises(AccessDenied, match="published cost-test set"):
            service.create_run(analyst, pge, request(release, settings=settings, cost_test_set=draft), job_zip(tmp_path))
    # named by id: its published version wins over the draft
    ee = registered(cpuc_admin)
    named = service.create_run(analyst, pge, request(release, settings={"cost_test_set": "EE-2026"}), job_zip(tmp_path))
    assert named.cost_tests["pack"] == ee.pk
    with pytest.raises(AccessDenied, match="No cost-test set EE-2040"):
        service.create_run(analyst, pge, request(release, settings={"cost_test_set": "EE-2040"}), job_zip(tmp_path))


def test_a_replica_takes_no_cost_test_set(analyst, pge, release, cpuc_admin, tmp_path):  # noqa: F811
    ee = registered(cpuc_admin)
    run = service.create_run(analyst, pge, request(release, calculation="cet-25.1"), job_zip(tmp_path))
    assert run.cost_tests == {} and "cost_test_set" not in run.settings
    with pytest.raises(AccessDenied, match="cost-test set"):
        service.create_run(analyst, pge, request(release, calculation="cet-25.1", cost_test_set=ee), job_zip(tmp_path))


def test_a_receipt_on_another_set_fails_the_run(analyst, pge, release, cpuc_admin, tmp_path):  # noqa: F811
    ee = registered(cpuc_admin)
    ok = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    ok = service.finish(ok, exit_code=0, receipt={"cost_tests": {"id": "EE-2026", "sha256": cost_tests.sha256(ee)}})
    assert ok.status == Run.Status.SUCCEEDED and not ok.error
    bad = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    bad = service.finish(bad, exit_code=0, receipt={"cost_tests": {"id": "EE-2026", "sha256": "0" * 64}})
    assert bad.status == Run.Status.FAILED and "not the pinned EE-2026" in bad.error


def test_a_filing_is_priced_on_the_set_its_setup_requires(filer, filing, pge, release, programs, cpuc_admin):  # noqa: F811
    ee26 = registered(cpuc_admin)
    ee28 = registered(cpuc_admin, sid="EE-2028", years=(2030, None))  # not yet in force for the filing's 2028 jobs
    draft = registered(cpuc_admin, publish=False, sid="EE-2028", version="next")
    setup = filing.budget_setup
    with pytest.raises(AccessDenied, match="not a published cost-test set"):
        budgets.change_setup(cpuc_admin, setup, acc_version="2026", reason="try a draft", cost_test_set=draft)
    v = budgets.upload(filer, filing, pge, job(), "tual.zip")
    run = Run.objects.get(pk=budgets.price(filer, v))
    assert run.cost_tests["id"] == "EE-2026" and run.settings["cost_test_set"] == "EE-2026"  # blank setup: the set in force
    budgets.change_setup(cpuc_admin, setup, acc_version="2026", reason="the CPUC's 2028 tests", cost_test_set=ee28)
    v = budgets.upload(filer, filing, pge, job(), "tual-2.zip")
    run = Run.objects.get(pk=budgets.price(filer, v))
    assert run.cost_tests["pack"] == ee28.pk and run.settings["cost_test_set"] == "EE-2028"
    calc = finish_run_on(run, ee28)
    v.refresh_from_db()
    assert calc.status == Run.Status.SUCCEEDED and v.calculation["cost_tests"]["id"] == "EE-2028"
    assert ee26.status == "published"


def finish_run_on(run, pack):
    service.mark_started(run, worker="test")
    out = run.storage_prefix + "out/"
    storage.store("results").put_bytes(out + "_results/group_sums2_measure_level_run.parquet", group_sums())
    return service.finish(run, exit_code=0, receipt={"packs": {"files": {}}, "cost_tests": {"sha256": cost_tests.sha256(pack)}})


def test_the_cost_test_sets_page_lists_and_registers_sets(cpuc_admin, pge_admin):
    registered(cpuc_admin)
    c = Client()
    sign_in(c, pge_admin)
    page = c.get("/reference-data/cost-tests/").content.decode()
    assert "EE-2026 2026-09-15" in page and "Register a set" not in page_only(page)
    sign_in(c, cpuc_admin)
    r = c.post("/reference-data/cost-tests/", {"file": SimpleUploadedFile("definition.yaml", set_file(sid="EE-2028", years=(2028, None)))})
    p = refdata.PackVersion.objects.get(kind="cost_tests", label="EE-2028")
    assert r.status_code == 302 and r["Location"] == f"/reference-data/{p.pk}/"
    detail = c.get(f"/reference-data/{p.pk}/").content.decode()
    assert "TRC: TRC test (headline)" in detail and "2028 on" in detail
    r = c.post("/reference-data/cost-tests/", {"file": SimpleUploadedFile("x.yaml", b"- not a set\n")})
    assert "not a readable" in r.content.decode().lower() or "mapping" in r.content.decode()


def test_the_register_command(cpuc_admin, tmp_path):
    from django.core.management import call_command

    f = tmp_path / "definition.yaml"
    f.write_bytes(set_file(sid="EE-2030", years=(2030, 2033)))
    call_command("register_cost_tests", str(f))
    p = refdata.PackVersion.objects.get(kind="cost_tests", label="EE-2030")
    assert p.status == "draft" and cost_tests.years_text(p) == "2030 to 2033"
