"""WCAG 2.2 AA on every page, in both colour schemes, at full width and at 320 px (reflow, 1.4.10).

Run with `pytest -m a11y` (CI does). axe finds what can be found automatically; keyboard and screen-reader checks are by hand
(docs/ACCESSIBILITY.md). Each page is checked with realistic content: SCENE builds organisations, members, an access request, a pending
PII grant and an open recertification, so the tables and forms are not empty."""

import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone

from cedars.core import storage
from cedars.modules.calculations import drafts
from cedars.modules.calculations import service as calculations
from cedars.modules.calculations.models import InputDraft
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service, tokens
from cedars.services.dictionary import service as dictionary
from cedars.services.notifications import service as notifications
from cedars.services.refdata import service as refdata
from cedars.services.registry import service as registry
from cedars.services.registry.models import ProgramChangePolicy
from cedars.services.rules.models import Rule, RuleSet
from cedars.services.submissions import service as submissions

from ..fake_recet_worker import write_results
from .pages import PAGES

pytestmark = [pytest.mark.a11y, pytest.mark.django_db(transaction=True)]

TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


@pytest.fixture
def impact_profiles(cpuc_admin, pge, pge_admin, make_user, settings, tmp_path):
    """The Impact profiles pages' content: a synced package, an accepted and a rejected upload, generated and custom profiles, a mapping
    and a library write."""
    import numpy as np
    from django.core.files.base import ContentFile

    from cedars.modules.impact_profiles import custom, etrm, generation, jobs, library, loadshapes
    from cedars.modules.impact_profiles.models import ImpactProfile, LoadShape, MeasurePackage

    from ..test_impact_profiles import FIXTURE, leds_series, load_shape_csv, profile_file

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.IMPACT_PROFILES_INLINE_JOBS = False
    dev = make_user("developer@pge.com", "Dee Veloper")
    service.grant(pge_admin, dev, pge, "impact_profiles.submit")
    etrm.sync(cpuc_admin, etrm.FileSource(FIXTURE))
    leds = MeasurePackage.objects.get(measure_version_id="SWLG999-01-draft")
    good = loadshapes.create_upload(dev, pge, leds, ContentFile(load_shape_csv(leds_series()), name="shapes.csv"))
    loadshapes.create_upload(dev, pge, leds, ContentFile(load_shape_csv([({"TechID": "Msr-X"}, np.full(8760, 1 / 8000))]), name="bad.csv"))
    jobs.run_pending()
    generation.generate(dev, leds, good)
    target = ImpactProfile.objects.get(profile_key="OfL*Ex*rDXGF*Msr-LED*Std-Fluor")
    custom.create_mapping(
        dev, pge, leds, alias_key="OfL*New*rDXGF*Msr-LED*Std-Fluor", target_sector="Com", target=target, reason="NC reuses Ex"
    )
    mine = custom.create_custom(
        dev, pge, profile_key="PGE Office NMEC", target_sector="Commercial", territory="PGE", data=profile_file(), file_name="p.csv"
    )
    library.export(cpuc_admin)
    return {
        "developer": dev,
        "package": leds.measure_version_id,
        "upload": good.pk,
        "rejected": good.pk + 1,
        "profile": target.pk,
        "custom_profile": mine.pk,
        "load_shape": LoadShape.objects.first().pk,
    }


@pytest.fixture
def scene(cpuc_admin, cpuc, pge, pge_admin, make_user, impact_profiles):
    service.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    analyst = make_user("analyst@pge.com", "Ana Lyst")
    service.grant(pge_admin, analyst, pge, "claims.submit", expires_at=timezone.now() + timedelta(days=90))
    service.grant(pge_admin, analyst, pge, perms.PII, reason="files claims with contacts")
    community = make_user("community@example.com")
    service.request_access(community, pge, ["claims.read", "budgets.read"], "I work on PG&E's filings")
    service.open_recertification(pge_admin, pge, timezone.now() + timedelta(days=30))
    tokens.create_token(pge_admin, pge, name="Claims system", scopes=["calculations", "claims"], days=90)
    tokens.create_token(analyst, pge, name="Analyst laptop", scopes=["calculations"], days=30)
    factors = (
        (Path(__file__).parent.parent / "data" / "statewide_factors_20260918.csv")
        .read_bytes()
        .replace(b"0.4440,0.1550", b"0.4000,0.1550", 1)
    )
    dictionary.load_catalog(
        None, json.loads((Path(__file__).parent.parent.parent / "data" / "dictionary" / "recet-0.1.80.json").read_text())
    )
    allocation = registry.import_allocation_csv(cpuc_admin, factors, label="2028 TUAL draft", purpose="filing")  # with a problem to show
    rules = RuleSet.objects.create(
        code="claims", label="Quarterly claims", effective_from=date(2026, 3, 12), status="published", source="test"
    )
    Rule.objects.create(
        rule_set=rules,
        rule_id="S1a2b3c",
        severity="reject",
        table="Measure",
        field="NumUnits",
        message="NumUnits should be a number",
        fix="Enter a plain number in NumUnits.",
        kind="spec",
        definition={"check": "numeric"},
        modules=["claims"],
    )
    Rule.objects.create(
        rule_set=rules,
        rule_id="W4d5e6f",
        severity="warning",
        table="Measure",
        field="UnitMeaCost1stBaseline",
        kind="rule",
        message="When UnitIncentiveToOthers equals 0, UnitMeaCost1stBaseline should be different from 0",
        fix="Change it.",
        difference="Budget filings do not apply it.",
        modules=["claims"],
    )
    # a filing past its due date with a reopen request waiting for a CPUC claims approver
    service.grant(pge_admin, analyst, pge, "claims.confirm")
    approver = make_user("approver@cpuc.ca.gov", "Avery Prover")
    service.grant(cpuc_admin, approver, cpuc, "claims.approve")
    quarter = [p for p in registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]
    deadline = submissions.add_deadline(
        cpuc_admin, module="claims", kind="quarterly claim", period=quarter, due_at=timezone.now() + timedelta(days=10)
    )
    v = submissions.start(analyst, deadline, pge)
    submissions.attach_files(analyst, v, [{"name": "Measure.csv", "key": "s3://bucket/x", "sha256": "abc", "bytes": 10}])
    submissions.record_validation(analyst, v, None, {"records": 10, "rejected": 0, "warnings": 2})
    submissions.record_calculation(None, v, run="run-1", input_hash="h1")
    submissions.confirm(analyst, v, "h1")
    submissions.move_deadline(cpuc_admin, deadline, timezone.now() - timedelta(minutes=1), "close-out for the scene")
    submissions.request_reopen(analyst, v.submission, "Two measures were counted twice")
    manifest = {
        "hourly/electric_2026.parquet": {"sha256": "ab" * 32, "bytes": 912_000_000},
        "README.md": {"sha256": "cd" * 32, "bytes": 2048},
    }
    pack = refdata.register_pack(
        cpuc_admin,
        kind="acc_electric",
        label="2026 ACC",
        version="v2 draft",
        acc_year=2026,
        manifest=manifest,
        storage_uri="s3://refdata/acc/v2/",
    )
    refdata.record_tie_out(cpuc_admin, pack, check="source_workbook", passed=False, precision=1e-9, largest_difference=0.02)
    published = refdata.register_pack(
        cpuc_admin,
        kind="acc_electric",
        label="2026 ACC",
        version="v1b",
        acc_year=2026,
        manifest=manifest,
        storage_uri="s3://refdata/acc/v1b/",
    )
    for check in ("source_workbook", "reference_measures"):
        refdata.record_tie_out(cpuc_admin, published, check=check, passed=True, precision=1e-9, largest_difference=0)
    refdata.publish_pack(cpuc_admin, published)
    notifications.subscribe(pge_admin, "pack.published")
    notifications.notify(pge_admin, "Your run finished: 2028 TUAL", "Portfolio TRC 1.21, PAC 1.64", "/reference-data/")
    notifications.notify(pge_admin, "Your run failed: scratch")
    notifications.mark_read(pge_admin, list(pge_admin.notifications.filter(title__contains="failed").values_list("pk", flat=True)))
    # Edit inputs: a draft with a cell that would stop the run (the engine's checks; without the engine the pages say so)
    if drafts.engine() is not None:
        draft = drafts.create_blank(analyst, None, "Lighting retrofit, 2028")
        drafts.set_row(analyst, draft, "Measure", 0, {"NumUnits": "ten"})
    else:
        draft = InputDraft.objects.create(owner=analyst, name="Lighting retrofit, 2028")
    # calculations: an engine release, a queued run and a finished one with its files
    service.grant(pge_admin, analyst, pge, "calculations.submit")
    release = calculations.add_release(
        cpuc_admin,
        release="0.1.78",
        calculations=["recet", "cet-25.1"],
        accs={"recet": ["2024", "2026"], "cet-25.1": ["2024"]},
    )
    job = Path(settings.CEDARS_STORAGE["root"]).parent / "job.zip"
    with zipfile.ZipFile(job, "w") as z:
        z.writestr("Measure.csv", "CEInputID|ClaimYearQuarter\nM1|2028Q1\nM2|2028Q2\n")
    cet_outputs = job.with_name("job_outputs.csv")  # a parallel run: compared with CET's outputs for the same job
    cet_outputs.write_text("CET_ID,TRCCost\nM1,1000\n")
    req = calculations.RunRequest(
        name="2028 claims, all programs", release=release, calculation="recet", acc_version="2024", cet_outputs=cet_outputs
    )
    finished = calculations.create_run(analyst, pge, req, job)
    calculations.mark_started(finished, worker="scene")
    out = finished.storage_prefix + "out/"
    storage.store("results").put_bytes(out + "measure_level_run.csv.zip", b"PK")
    storage.store("results").put_bytes(out + "receipt.json", b"{}")
    storage.store("results").put_bytes(
        out + "cet_comparison_summary_run.csv", b"metric,POC,CET,POC/CET\nTRCCost,1200,1000,1.2\nGasBen,5,0,\n"
    )
    write_results(storage.store("results").path(out), 1.0)  # the Results tables (with the engine installed; a note without it)
    calculations.finish(
        finished,
        exit_code=0,
        receipt={
            "engine": {"release": "0.1.78", "commit": "91a88f5e43"},
            "packs": {"files": {"acc/2024/electric_hourly.parquet": {"sha256": "ab" * 32}}},
            "allocation": {"table": "statewide_factors_20260918", "sha256": "cd" * 32},
            "outputs": {"measure_level_run.csv.zip": {"sha256": "ef" * 32}},
            "totals": {"TRCRatio": 1.42, "PACRatio": 1.87},
            "seconds": 9,
        },
    )
    queued = calculations.create_run(
        analyst, pge, calculations.RunRequest(name="2029 budget", release=release, calculation="recet", acc_version="2026"), job
    )
    hvac = registry.create_program(
        pge_admin,
        prg_id="PGE_Res_HVAC",
        administrator=pge,
        valid_from=date(2024, 1, 1),
        name="Residential HVAC",
        status="active",
        segment="resource_acquisition",
        sectors={"Residential": 0.9, "Commercial": 0.1},
        start_year=2024,
        flags={"Resource_Flag": "True", "Deemed_Flag": "True"},
    )
    registry.change_program(pge_admin, hvac, effective=date(2026, 7, 1), reason="advice letter 4900-E", implementer="Third-Party Solicited")
    service.grant(cpuc_admin, approver, cpuc, "programs.approve")
    ProgramChangePolicy.objects.get_or_create(
        effective_from=date(2000, 1, 1), defaults={"locked_fields": ["segment"]}
    )  # flushed by earlier tests
    change_request = registry.request_change(
        pge_admin, hvac, effective=date.today() + timedelta(days=60), reason="moves to market support", segment="market_support"
    )
    sw_id = factors.split(b"\n")[1].split(b",")[1].decode()
    registry.create_program(
        cpuc_admin, prg_id=sw_id, administrator=None, statewide=True, valid_from=date(2024, 1, 1), name="A statewide program"
    )
    budgets = _budgets(cpuc_admin, pge, pge_admin, analyst, make_user)
    return {
        **budgets,
        "pack": pack,
        "published_pack": published,
        "analyst": analyst,
        "queued_run": queued,
        "finished_run": finished,
        "draft": draft,
        "rules": rules,
        "cpuc_admin": cpuc_admin,
        "approver": approver,
        "change_request": change_request.pk,
        "pge_admin": pge_admin,
        "community": community,
        "password": community,
        "pge": pge,
        "cpuc": cpuc,
        "allocation": allocation,
        **impact_profiles,
        "sw": sw_id,
    }


def _budgets(cpuc_admin, pge, pge_admin, analyst, make_user):
    """A TUAL priced, confirmed and published, and an MCAL whose upload has rejected records. The scene's impact-profile pack is a
    draft, which a filing's run refuses, so the TUAL's run is recorded as the Calculations module records a finished one."""
    from cedars.modules.budgets import service as budgets

    from ..test_budgets import group_sums, job

    for p in ("budgets.submit", "budgets.confirm"):
        service.grant(pge_admin, analyst, pge, p)
    registry.create_program(pge_admin, prg_id="PGE_Com_Ltg", administrator=pge, valid_from=date(2024, 1, 1), name="Commercial lighting")
    tual = budgets.add_filing(
        cpuc_admin, first_year=2028, kind="True-up advice letter (TUAL)", due_at=timezone.now() + timedelta(days=30), acc_version="2026"
    )
    mcal = budgets.add_filing(
        cpuc_admin, first_year=2028, kind="Mid-cycle advice letter (MCAL)", due_at=timezone.now() + timedelta(days=400), acc_version="2026"
    )
    v = budgets.upload(analyst, tual, pge, job(), "tual.zip")
    key = "runs/scene/out/_results/group_sums2_measure_level_run.parquet"
    storage.store("results").put_bytes(key, group_sums())
    details = {"job_sha256": budgets.job_file(v)["sha256"], "acc": "2026", "release": "0.1.80", "results": {"group_sums": key}}
    v = submissions.record_calculation(None, v, run="scene", input_hash="h", details=details)
    v = budgets.confirm(analyst, v)
    bad = job(costs=["PGE_Res_HVAC|2027|2027Q1|PGE|1|0|0|0|0|0|0|0|0|0|0"], measures=["M1|NOPE|2028Q1|1|0|0|0|0"])
    draft = budgets.upload(analyst, mcal, pge, bad, "mcal.zip")
    return {
        "tual": tual,
        "mcal": mcal,
        "budget_version": v,
        "budget_draft": draft,
        **_fund_shifts(cpuc_admin, pge, analyst, tual.cycle, make_user),
    }


def _fund_shifts(cpuc_admin, pge, analyst, cycle, make_user):
    """A shift in effect, a statewide one waiting for its co-funder (SCE, by a confirmed allocation version) and an unbalanced draft."""
    from cedars.modules.budgets import shifts

    from ..test_fund_shifts import FACTORS, L

    registry.confirm_allocation(cpuc_admin, registry.import_allocation_csv(cpuc_admin, FACTORS.encode(), label="SW X", purpose="filing"))
    registry.create_program(cpuc_admin, prg_id="SW_X", administrator=None, statewide=True, valid_from=date(2024, 1, 1), name="SW X")
    from cedars.services.accounts.models import Organisation

    sce = Organisation.objects.get(code="SCE")
    registry.create_program(cpuc_admin, prg_id="SCE_Ind", administrator=sce, valid_from=date(2024, 1, 1), name="Industrial")
    sce_admin, sce_user = make_user("admin@sce.com", "Addy Min"), make_user("budgets@sce.com", "Sky Edison")
    service.grant(cpuc_admin, sce_admin, sce, perms.ORG_ADMIN)
    service.grant(sce_admin, sce_user, sce, "budgets.confirm")
    made = {}
    for key, title, lines, submit in [
        ("shift_effective", "Into residential HVAC", L(("PGE_Res_HVAC", 2029, 100), ("PGE_Com_Ltg", 2028, -100)), True),
        ("shift_waiting", "Into SW X", L(("SW_X", 2028, 50), ("PGE_Com_Ltg", 2028, -30)), True),
        ("shift_draft", "Not balanced yet", L(("PGE_Res_HVAC", 2028, 10), ("PGE_Com_Ltg", 2028, -5000)), False),
    ]:
        s = shifts.create(analyst, pge, cycle, title=title, reason="the scene's shift")
        shifts.set_lines(analyst, s, lines)
        made[key] = (shifts.submit(analyst, s) if submit else s).pk
    return {**made, "sce_user": sce_user}


def _session_cookie(user, method, live_server):
    s = SessionStore()
    s[SESSION_KEY] = str(user.pk)
    s[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    s[HASH_SESSION_KEY] = user.get_session_auth_hash()
    records = [{"method": "password", "at": 0}] + ([{"method": method, "at": 0}] if method != "password" else [])
    s["account_authentication_methods"] = records
    s["account_authentication_methods_user"] = str(user.pk)
    s.create()
    return {"name": settings.SESSION_COOKIE_NAME, "value": s.session_key, "url": live_server.url}


def _open(browser, live_server, scene, path, who, **context_args):
    context = browser.new_context(**context_args)
    if who is not None:
        context.add_cookies([_session_cookie(scene[who], "password" if who == "password" else "mfa", live_server)])
    page = context.new_page()
    from cedars.services.accounts.models import Organisation

    ids = {k: (v.code if isinstance(v, Organisation) else getattr(v, "pk", v)) for k, v in scene.items()}
    response = page.goto(live_server.url + path.format(**ids))
    assert response.status == 200, f"{path} answered {response.status} at {page.url}"
    return context, page


def _violations(page, axe_source):
    page.add_script_tag(content=axe_source)
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # check what is folded away too
    result = page.evaluate("async (tags) => await axe.run(document, {runOnly: {type: 'tag', values: tags}})", TAGS)
    return [
        {"id": v["id"], "impact": v["impact"], "help": v["help"], "targets": [n["target"] for n in v["nodes"]][:5]}
        for v in result["violations"]
    ]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("name,path,who", PAGES, ids=[p[0] for p in PAGES])
def test_axe_clean(browser, axe_source, live_server, scene, name, path, who, scheme):
    # axe is injected inline, so this context bypasses the page's CSP; nothing else does
    context, page = _open(browser, live_server, scene, path, who, color_scheme=scheme, bypass_csp=True)
    found = _violations(page, axe_source)
    context.close()
    assert not found, f"{name} ({scheme}): " + json.dumps(found, indent=1)


@pytest.mark.parametrize("name,path,who", PAGES, ids=[p[0] for p in PAGES])
def test_reflows_at_320px(browser, live_server, scene, name, path, who):
    context, page = _open(browser, live_server, scene, path, who, viewport={"width": 320, "height": 640})
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    context.close()
    assert overflow <= 0, f"{name} scrolls sideways by {overflow} px at 320 px wide"
