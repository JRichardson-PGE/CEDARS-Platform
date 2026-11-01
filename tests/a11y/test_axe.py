"""WCAG 2.2 AA on every page, in both color schemes, at full width and at 320 px (reflow, 1.4.10).

Run with `pytest -m a11y` (CI does). axe finds what can be found automatically; keyboard and screen-reader checks are by hand
(docs/ACCESSIBILITY.md). Each page is checked with realistic content: SCENE builds organizations, members, an access request, a pending
PII grant and an open recertification, so the tables and forms are not empty."""

import io
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from pytest_django.live_server_helper import LiveServer

from cedars.core import storage
from cedars.modules.calculations import drafts
from cedars.modules.calculations import service as calculations
from cedars.modules.calculations.models import InputDraft
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service, tokens
from cedars.services.dictionary import service as dictionary
from cedars.services.notifications import service as notifications
from cedars.services.refdata import cost_tests
from cedars.services.refdata import service as refdata
from cedars.services.registry import service as registry
from cedars.services.registry.models import ProgramChangePolicy
from cedars.services.rules.models import Rule, RuleSet
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from ..conftest import create_cpuc_admin, create_pge, create_pge_admin, create_user
from ..fake_recet_worker import write_results
from ..test_cost_test_sets import set_file
from .pages import PAGES

pytestmark = pytest.mark.a11y

TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


def _impact_profiles(cpuc_admin, pge, pge_admin, make_user):
    """The Impact profiles pages' content: a synced package, an accepted and a rejected upload, generated and custom profiles, a mapping
    and a library write."""
    import numpy as np
    from django.core.files.base import ContentFile

    from cedars.modules.impact_profiles import custom, etrm, generation, jobs, library, loadshapes
    from cedars.modules.impact_profiles.models import ImpactProfile, LoadShape, MeasurePackage

    from ..test_impact_profiles import FIXTURE, leds_series, load_shape_csv, profile_file

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
    custom.create_custom(  # identical to it: the page lists it as another name of the profile (BR-06.18)
        dev, pge, profile_key="SCE Office NMEC", target_sector="Commercial", territory="SCE", data=profile_file(), file_name="s.csv"
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


@pytest.fixture(scope="module")
def scene(django_db_setup, django_db_blocker, tmp_path_factory):
    """Built once for every check: the pages only read it, and rebuilding it per check (with a database flush after each) was nine
    tenths of the job's time. The live server thread and the session cookies need the database, so every check unblocks it too."""
    root = tmp_path_factory.mktemp("a11y")
    files = override_settings(
        CEDARS_STORAGE={"backend": "local", "root": str(root / "storage"), "buckets": {}},
        CALCULATIONS_RESULTS_CACHE=str(root / "results_cache"),
        MEDIA_ROOT=str(root / "media"),
        IMPACT_PROFILES_INLINE_JOBS=False,
        DATA_DOWNLOAD_MAX_ROWS=5,  # the forecast's 6 records are a background download, so its request form is checked
    )
    with django_db_blocker.unblock(), files:
        storage.reset()
        try:
            cpuc_admin = create_cpuc_admin()
            pge = create_pge(cpuc_admin)
            pge_admin = create_pge_admin(cpuc_admin, pge)
            yield _scene(cpuc_admin, service.cpuc(), pge, pge_admin, create_user)
        finally:
            for alias in ("default", "vault"):
                call_command("flush", database=alias, interactive=False, verbosity=0)
            storage.reset()


@pytest.fixture(scope="module")
def site(scene):
    """The site, served from a thread for the whole module. pytest-django's live_server fixture would flush the database after every
    check (it asks for transactional_db), taking the scene with it."""
    server = LiveServer("localhost")
    server._live_server_modified_settings.enable()
    yield server
    server._live_server_modified_settings.disable()
    server.stop()


@pytest.fixture(autouse=True)
def local_storage(django_db_blocker):
    """Replaces the per-test storage folder of tests/conftest.py: the scene's files stay where it wrote them."""
    with django_db_blocker.unblock():
        yield


def _scene(cpuc_admin, cpuc, pge, pge_admin, make_user):
    impact_profiles = _impact_profiles(cpuc_admin, pge, pge_admin, make_user)
    service.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    service.create_organisation(cpuc_admin, code="NEWPA", name="A PA with no type yet", kind="pa")  # PA type not set
    ava = service.create_organisation(cpuc_admin, code="EBCE", name="East Bay Community Energy", kind="pa")
    service.rename_organisation(cpuc_admin, ava, code="AVA", name="Ava Community Energy", effective=date(2024, 1, 1), reason="renamed")
    twice = service.create_organisation(cpuc_admin, code="AVA2", name="Ava, loaded twice", kind="pa")
    service.merge_organisation(cpuc_admin, twice, into=ava, effective=date(2025, 1, 1), reason="the same party")
    analyst = make_user("analyst@pge.com", "Ana Lyst")
    service.grant(pge_admin, analyst, pge, "claims.submit", expires_at=timezone.now() + timedelta(days=90))
    service.grant(pge_admin, analyst, pge, perms.PII, reason="files claims with contacts")
    community = make_user("community@example.com")
    service.request_access(community, pge, ["claims.read", "budgets.read"], "I work on PG&E's filings")
    service.open_recertification(pge_admin, pge, timezone.now() + timedelta(days=30))
    tokens.create_token(pge_admin, pge, name="Claims system", scopes=["calculations", "claims"], days=90)
    tokens.create_token(analyst, pge, name="Analyst laptop", scopes=["calculations"], days=30)
    small_items = _role_templates_and_feedback(cpuc_admin, pge, pge_admin, analyst, make_user)
    factors = (
        (Path(__file__).parent.parent / "data" / "statewide_factors_20260918.csv")
        .read_bytes()
        .replace(b"0.4440,0.1550", b"0.4000,0.1550", 1)
    )
    dictionary.load_catalog(
        None, json.loads((Path(__file__).parent.parent.parent / "data" / "dictionary" / "recet-0.1.87.json").read_text())
    )
    allocation = registry.import_allocation_csv(cpuc_admin, factors, label="2028 TUAL draft", purpose="filing")  # with a problem to show
    from cedars.services.registry import goals

    goal_set = goals.import_goals(  # a draft with one problem to show
        cpuc_admin,
        b"Utility,Kind,FirstYear,LastYear,ProgramTSB,ProgramGWh,ProgramMW,ProgramMMTherms,CSGWh,CSMW,CSMMTherms\n"
        b"PGE,cycle,2028,2031,1000,500,90,10,300,50,5\nPGE,annual,2028,2028,250,120,22,-,80,12,1\nXYZ,annual,2028,2028,1,1,1,1,1,1,1\n",
        decision="D.00-00-000",
        adopted=date(2026, 1, 1),
        rules=b"PA,Territory,Share,AppliesTo,Source\nMCE,PGE,1,programs,example\n",
        source_name="goals.csv",
    )
    rules = RuleSet.objects.create(code="claims", label="Quarterly claims", effective_from=date(2026, 3, 12), source="test")
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
    RuleSet.objects.filter(pk=rules.pk).update(status="published")  # rules are added while a set is a draft (migration 0002)
    rules_draft, value_list_draft = _admin_drafts(rules)
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
    cost_set = cost_tests.register_set(cpuc_admin, cost_tests.engine_copy("EE-2026", "2026-10-31") or set_file(), source="ReCET 0.1.85")
    refdata.record_tie_out(cpuc_admin, cost_set, check="engine_check", passed=True, engine_release="0.1.85")
    refdata.publish_pack(cpuc_admin, cost_set)
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
    pii_filer = make_user("contacts@pge.com", "Con Tacts")
    for p in ("claims.submit", "claims.confirm"):
        service.grant(pge_admin, pii_filer, pge, p)
    service.approve_pii(cpuc_admin, service.grant(pge_admin, pii_filer, pge, perms.PII, reason="keeps the claims' contacts"))
    claims = _claims(cpuc_admin, pge, pge_admin, analyst, pii_filer)
    budgets = _budgets(cpuc_admin, pge, pge_admin, analyst, make_user)
    monthly = _monthly(cpuc_admin, pge, pge_admin, analyst)
    corrections = _corrections(cpuc_admin, pge, analyst, budgets, monthly)
    _follows(pge_admin, community, pge, hvac)
    documents = _documents(cpuc_admin, cpuc, pge, hvac, community, pge_admin, analyst)
    legacy_years = _legacy()
    downloads = _downloads(community)
    return {
        **downloads,
        **_saved_views(community, cpuc_admin, pge_admin, pge),
        **legacy_years,
        **claims,
        **budgets,
        **monthly,
        **corrections,
        **_releases(cpuc_admin),
        **_mandates(cpuc_admin, rules),
        **documents,
        **_publication_hold(pge),
        **_accessibility_checks(cpuc_admin, documents["document"]),
        "pack": pack,
        "published_pack": published,
        "etrm_pack": _etrm(cpuc_admin),
        "cost_set": cost_set,
        "analyst": analyst,
        "pii_filer": pii_filer,
        "queued_run": queued,
        "finished_run": finished,
        "draft": draft,
        "rules": rules,
        "rules_draft": rules_draft,
        "list_code": value_list_draft,
        "cpuc_admin": cpuc_admin,
        "approver": approver,
        "change_request": change_request.pk,
        "pge_admin": pge_admin,
        "community": community,
        "password": community,
        "pge": pge,
        "cpuc": cpuc,
        "allocation": allocation,
        "goal_set": goal_set,
        **small_items,
        **impact_profiles,
        "sw": sw_id,
    }


def _role_templates_and_feedback(cpuc_admin, pge, pge_admin, analyst, make_user):
    """BR-01.3 and BR-12.4: the starter role templates (seeded by a migration, which the transactional tests flush, so seeded again),
    a member granted one, a retired template; a new feedback message from a member and a closed one sent signed out."""
    import importlib

    from django.apps import apps

    from cedars.services.accounts.models import RoleTemplate
    from cedars.services.feedback import service as feedback

    importlib.import_module("cedars.services.accounts.migrations.0007_role_templates").add_starters(apps, None)
    consultant = RoleTemplate.objects.get(name="Consultant")
    service.apply_role_template(pge_admin, make_user("consultant@example.org", "Con Sultant"), pge, consultant, reason="2028 filing")
    old = service.create_role_template(cpuc_admin, name="Contractor (old)", kinds=["pa"], permissions=["programs.read"])
    service.change_role_template(cpuc_admin, old, reason="replaced by Implementer", active=False)
    new = feedback.submit(page="/budgets/?year=2028", message="The year filter is hard to find on a phone.", user=analyst, organisation=pge)
    closed = feedback.submit(page="/data/overview/", message="Could the chart show 2027 too?\nThanks!", address="203.0.113.5")
    feedback.close(cpuc_admin, closed, "Added in the next release.")
    return {"role_template": consultant.pk, "feedback": new.pk, "feedback_closed": closed.pk}


def _admin_drafts(rules):
    """Gap 10 (BR-12.6): a draft copy of the published rules with a changed rule, and the sector value list with a published
    version and a draft that adds a code. Made directly (the scene has no engine to copy the shipped list from)."""
    from cedars.services.dictionary.models import Code, ValueList, ValueListVersion

    draft = RuleSet.objects.create(
        code="claims", label="Quarterly claims", effective_from=date(2027, 1, 1), based_on=rules, reason="D.26-01-001", source="Copied"
    )
    for r in rules.rules.all():
        Rule.objects.create(
            rule_set=draft,
            **{f: getattr(r, f) for f in ("rule_id", "severity", "table", "field", "message", "fix", "kind", "definition", "modules")},
        )
    Rule.objects.filter(rule_set=draft, rule_id="W4d5e6f").update(rule_id="V7a8b9c", severity="reject", replaces="W4d5e6f")
    sector = ValueList.objects.create(name="sector", label="Sector")
    v1 = ValueListVersion.objects.create(value_list=sector, number=1, based_on="the list shipped with ReCET")
    rows = [("Residential", 2019, None), ("Commercial", 2019, None), ("Res", 2013, 2018)]
    Code.objects.bulk_create(Code(version=v1, code=c, start_year=a, end_year=b) for c, a, b in rows)
    ValueListVersion.objects.filter(pk=v1.pk).update(status="published", effective_from=date(2026, 1, 1), reason="First version")
    v2 = ValueListVersion.objects.create(value_list=sector, number=2, based_on="version 1")
    Code.objects.bulk_create(Code(version=v2, code=c, start_year=a, end_year=b) for c, a, b in rows)
    new = Code.objects.create(version=v2, code="Tribal", description="Tribal communities", start_year=2028, added_in_draft=True)
    return draft, new.pk


def _documents(cpuc_admin, cpuc, pge, hvac, community, pge_admin, analyst):
    """The library: an EM&V report published with a newer version waiting, a PA's joint memo not yet published, a download of each
    kind. The value lists are seeded by a migration, which the transactional tests flush, so they are seeded again here."""
    import importlib

    from django.apps import apps
    from django.core.files.uploadedfile import SimpleUploadedFile

    from cedars.modules.documents import comments, deliverables, studies, subscriptions
    from cedars.modules.documents import service as documents
    from cedars.modules.documents.models import AttributeOption, Document

    importlib.import_module("cedars.modules.documents.migrations.0002_value_lists").seed(apps, None)
    importlib.import_module("cedars.modules.documents.migrations.0005_study_lists").seed(apps, None)

    def opt(code, label):
        return AttributeOption.objects.get(attribute_list__code=code, label=label)

    def files(*names):
        return [documents.Upload(n, SimpleUploadedFile(n, b"%PDF-1.7 scene")) for n in names]

    documents.retire_option(cpuc_admin, opt("portfolio_cycle", "2006-2008"), reason="closed cycle")
    work_order = studies.add_work_order(cpuc_admin, number="ED_D_Res_1", name="Residential impact evaluations", firm_name="An evaluator")
    task = studies.add_task(cpuc_admin, work_order, "Task 3: HVAC")
    study = studies.create_study(
        cpuc_admin,
        cpuc,
        studies.StudyData(
            title="Residential HVAC impact evaluation",
            manager=cpuc,
            lead=opt("study_lead", "CPUC"),
            study_type=opt("study_type", "Impact Evaluation"),
            sectors=[opt("sector", "Residential")],
            funding_cycles=[opt("portfolio_cycle", "2022"), opt("portfolio_cycle", "2023")],
            budget=850000,
            status=opt("study_status", "Data/Analysis"),
            work_order=work_order,
            description="Gross and net savings of residential HVAC measures.",
            next_activity="Draft report",
            next_activity_due=date.today() + timedelta(days=20),
            contacts="Evaluation contractor's project manager",
        ),
    )
    study.psr = {
        "project": study.number,
        "study_id": study.study_id,
        "fields": {"Study manager": "DNV GL", "Group": "Cross-Cutting (Group B)"},
    }
    study.save(update_fields=["psr"])
    studies.change_study(
        cpuc_admin,
        study,
        studies.StudyData(
            title=study.title,
            manager=cpuc,
            lead=study.lead,
            study_type=study.study_type,
            sectors=list(study.sectors.all()),
            funding_cycles=list(study.funding_cycles.all()),
            budget=900000,
            status=opt("study_status", "Draft Report"),
            work_order=work_order,
            description=study.description,
        ),
        reason="Draft report delivered",
    )
    studies.set_coverage(cpuc_admin, study, hvac, 2022, 2024, whole=False, note="Heat pumps and furnaces")
    report = documents.create_document(
        cpuc_admin,
        cpuc,
        "emv",
        documents.Details(
            opt("deliverable_type", "Final Evaluation Report"),
            "PY2022-2024 Residential HVAC impact evaluation",
            summary="Gross and net savings of residential HVAC measures, program years 2022 to 2024.",
            pas=[pge],
            programs=[hvac],
            first_year=2022,
            last_year=2024,
            attributes=[opt("study_type", "Impact Evaluation"), opt("sector", "Residential"), opt("study_lead", "CPUC")],
            study=study,
            work_order=work_order,
            task=task,
        ),
        files("hvac_impact_final.pdf", "hvac_impact_appendices.pdf"),
        publish=True,
    )
    documents.add_version(cpuc_admin, cpuc, report, files("hvac_impact_final_rev1.pdf"), reason="Table 4-2 corrected")
    Document.objects.filter(pk=report.pk).update(pda_id=4210, calmac_id="CPU0213.01")  # as the PDA migration leaves it
    memo = documents.create_document(
        None,  # as an import would file it; PG&E's members file their own
        pge,
        "pa",
        documents.Details(opt("pa_report_type", "Joint Cooperation Memo"), "PG&E and BayREN Joint Cooperation Memo 2026", first_year=2026),
        files("jcm_2026.pdf"),
    )
    documents.create_document(  # a placeholder implementation plan (BR-13.6), shown on the program's page
        None,
        pge,
        "program",
        documents.Details(
            opt("program_file_type", "Implementation plan"), "Residential HVAC implementation plan", programs=[hvac], first_year=2028
        ),
        files("hvac_ip_placeholder.pdf"),
        publish=True,
        placeholder=True,
    )
    f = report.versions.get(number=1).files.first()
    documents.record_download(None, f)
    documents.record_download(cpuc_admin, f, viewed=True)
    # A deliverable: a draft workplan commented on, the final results, PG&E asked to respond; a second one with its comments open now.
    today = date.today()
    plan = documents.create_document(
        cpuc_admin,
        cpuc,
        "emv",
        documents.Details(opt("deliverable_type", "Evaluation Plan"), "Residential HVAC impact evaluation deliverable", study=study),
        files("workplan_draft.pdf"),
        publish=True,
        deliverable=True,
        stage="draft_workplan",
        comment_period=(today - timedelta(days=3), today + timedelta(days=11)),
    )
    question = comments.post(
        community,
        None,
        plan.versions.get(),
        comments.NewComment("question", "Baseline year", "Which baseline year does the workplan use?", section="Page 12"),
    )
    comments.respond(cpuc_admin, cpuc, question, "2019, as section 3 says.")
    comments.post(
        community, None, plan.versions.get(), comments.NewComment("comment", "Sample frame", "Please include heat pump water heaters.")
    )
    comments.set_period(cpuc_admin, plan.versions.get(), today - timedelta(days=30), today - timedelta(days=1), reason="closed")
    documents.add_version(cpuc_admin, cpuc, plan, files("results_final.pdf"), reason="Final results", publish=True, stage="final_results")
    documents.add_version(cpuc_admin, cpuc, plan, files("results_final_rev1.pdf"), reason="Table 5 corrected", stage="final_results")
    deliverables.request_responses(cpuc_admin, plan, [pge], today + timedelta(days=30))
    service.grant(pge_admin, analyst, pge, "documents.submit")  # files PG&E's response
    open_plan = documents.create_document(
        cpuc_admin,
        cpuc,
        "emv",
        documents.Details(opt("deliverable_type", "Draft Report"), "Commercial lighting market study", study=study),
        files("draft_results.pdf"),
        publish=True,
        deliverable=True,
        stage="draft_results",
        comment_period=(today - timedelta(days=1), today + timedelta(days=13)),
    )
    # Search by a file's text, key dates on the home page, and subscriptions (BR-15.2, 15.3, 15.5).
    documents.create_document(
        cpuc_admin,
        cpuc,
        "guidance",
        documents.Details(opt("guidance_type", "Guidance document"), "Field verification guidance"),
        [documents.Upload("field_checks.txt", SimpleUploadedFile("field_checks.txt", b"Refrigerant charge checks for heat pumps & AC."))],
        publish=True,
    )
    subscriptions.add_key_date(
        cpuc_admin,
        today + timedelta(days=6),
        "EM&V stakeholder workshop",
        detail="Draft results of the HVAC study.",
        link="https://www.cpuc.ca.gov/",
    )
    subscriptions.add_key_date(cpuc_admin, today - timedelta(days=4), "Workplan comments closed")
    subscriptions.subscribe(community, collection="emv")
    subscriptions.subscribe(community, option=opt("sector", "Residential"), channel="in_app")
    subscriptions.subscribe(community, study=study)
    subscriptions.subscribe(community, work_order=work_order)
    subscriptions.subscribe(cpuc_admin, collection="guidance")
    return {
        "document": report,
        "document_draft": memo,
        "study": study.number,
        "work_order": work_order.pk,
        "deliverable": plan.pk,
        "deliverable_open": open_plan.pk,
    }


def _downloads(community):
    """Background record downloads: one ready (the file made from the forecast records) and one queued."""
    from cedars.modules.data import jobs, records

    page = records.page(records.from_params({"dataset": ["forecast"]}), limit=0)
    ready = jobs.request(community, page, "csv")
    jobs.run(jobs._claim())
    queued = jobs.request(community, page, "parquet")
    return {"ready_download": ready.pk, "queued_download": queued.pk}


def _saved_views(community, cpuc_admin, pge_admin, pge):
    """Saved views: one of the community account's, shared by name and asked to be published, with a history; one published; its own
    default record columns."""
    from cedars.modules.data import saved
    from cedars.services.notifications import service as notifications

    view = saved.save(community, name="Residential lighting", page="overview", query="by=program&sector=Residential", description="Mine")
    saved.share_with(community, view, pge_admin.email)
    saved.request_publication(community, view, "The working group asked for it")
    # The administrators read the request's notification: an unread count on the bell would widen the header that
    # test_the_full_header_stays_on_one_row_at_1280 measures for cpuc_admin.
    notifications.mark_read(cpuc_admin)
    shown = saved.save(pge_admin, name="PG&E by sector", page="overview", query="by=sector&pa=PGE")
    saved.set_organisation(pge_admin, shown, pge)
    saved.publish(cpuc_admin, shown, "For the quarterly report")
    saved.set_default_columns(community, ["CEInputID", "PrgID", "TotalSystemBenefit"])
    return {"saved_view": view.pk, "published_view": shown.pk}


def _publication_hold(pge):
    """A published filing whose snapshot the scan for personal information held (BR-08.5), for the Publication holds pages. Not
    released, so the dashboards' releases are unchanged."""
    from cedars.services.snapshots.models import PIIScan, Snapshot
    from cedars.services.submissions.models import SubmissionVersion

    version = (
        SubmissionVersion.objects.filter(status="published", snapshot__isnull=True, submission__organisation=pge)
        .exclude(origin="imported")
        .order_by("pk")
        .first()
    )
    snap = Snapshot(dataset="claims", version=version, organisation=pge, rows=6, bytes=1, sha256="ab" * 32)
    snap.key = f"claims/{snap.id}/results.parquet"
    snap.save()
    finding = {"column": "MeasDescription", "pattern": "phone", "records": 1, "examples": ["4**-***-****"], "record_ids": ["M6"]}
    PIIScan.objects.create(
        snapshot=snap,
        outcome="held",
        findings=[finding],
        columns=["MeasDescription"],
        skipped=["CEInputID", "PrgID"],
        patterns=["email", "phone"],
        scanned_at=timezone.now(),
    )
    return {"held_snapshot": snap.pk}


def _dashboard_data(version, pge):
    """The portfolio dashboard's forecast: a snapshot of the confirmed TUAL with measure-level results, released."""
    from cedars.services.snapshots import service as snapshots
    from cedars.services.snapshots.models import Snapshot

    from ..test_data_query import results_file

    data = results_file()
    snap = Snapshot(dataset="forecast", version=version, organisation=pge, rows=5, bytes=len(data), sha256="ab" * 32)
    snap.key = f"forecast/{snap.id}/results.parquet"
    storage.store("snapshots").put_bytes(snap.key, data)
    snap.save()
    snapshots.rebuild("forecast", "scene")


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
    _dashboard_data(v, pge)
    bad = job(costs=["PGE_Res_HVAC|2027|2027Q1|PGE|1|0|0|0|0|0|0|0|0|0|0"], measures=["M1|NOPE|2028Q1|1|0|0|0|0"])
    draft = budgets.upload(analyst, mcal, pge, bad, "mcal.zip")
    return {
        "tual": tual,
        "mcal": mcal,
        "budget_version": v,
        "budget_draft": draft,
        **_fund_shifts(cpuc_admin, pge, analyst, tual.cycle, make_user),
    }


def _monthly(cpuc_admin, pge, pge_admin, analyst):
    """2028's monthly reports: January priced (estimates recorded as the Calculations module records a finished run), confirmed and
    published against the TUAL's operating budget; February uploaded with rejected records. Every month is open now."""
    from cedars.modules.monthly import service as monthly
    from cedars.services.submissions.models import Deadline

    from ..test_budgets import group_sums
    from ..test_monthly import report

    for p in ("monthly.submit", "monthly.confirm"):
        service.grant(pge_admin, analyst, pge, p)
    monthly.set_up_year(cpuc_admin, year=2028, acc_version="2026", due_day=20)
    Deadline.objects.filter(module="monthly").update(opens_at=None, due_at=timezone.now() + timedelta(days=30))
    jan, feb, mar = monthly.months(2028)[:3]
    v = monthly.upload(analyst, jan, pge, report(), "january.zip")
    key = "runs/monthly/out/_results/group_sums2_measure_level_run.parquet"
    storage.store("results").put_bytes(key, group_sums())
    details = {"job_sha256": monthly.job_file(v)["sha256"], "acc": "2026", "release": "0.1.80", "results": {"group_sums": key}}
    v = submissions.record_calculation(None, v, run="monthly", input_hash="h", details=details)
    v = monthly.confirm(analyst, v)
    bad = report("202802", commitments=["PGE|NOPE|202802|0|0|1|0|0|0|0|0|0|", "PGE|PGE_Res_HVAC|202801|0|0|1|0|0|0|0|0|0|"])
    draft = monthly.upload(analyst, feb, pge, bad, "february.zip")
    return {"monthly_jan": jan, "monthly_feb": feb, "monthly_mar": mar, "monthly_version": v, "monthly_draft": draft}


def _corrections(cpuc_admin, pge, analyst, budgets_scene, monthly_scene):
    """A business plan corrected after its due date and April's monthly report corrected after its due date: each correction's
    publication writes its change letter (BR-09.3). Neither touches the filings the other pages show."""
    from cedars.modules.budgets import service as budgets
    from cedars.modules.monthly import service as monthly

    from ..test_budgets import job
    from ..test_monthly import report

    plan = budgets.add_filing(
        cpuc_admin, first_year=2028, kind="Business plan application", due_at=timezone.now() - timedelta(days=60), acc_version="2026"
    )
    key = "runs/scene/out/_results/group_sums2_measure_level_run.parquet"
    april = monthly.months(2028)[3]
    reopen = {m: submissions.policy(m).reopen_after_due for m in ("budgets", "monthly")}
    for m in reopen:
        submissions.set_policy(cpuc_admin, m, reopen_after_due="open")

    def priced(module, v, run):
        details = {"job_sha256": module.job_file(v)["sha256"], "acc": "2026", "release": "0.1.80", "results": {"group_sums": key}}
        return submissions.record_calculation(None, v, run=run, input_hash=run, details=details)

    v = budgets.confirm(analyst, priced(budgets, budgets.upload(analyst, plan, pge, job(), "plan.zip"), "plan1"))
    submissions.request_reopen(analyst, v.submission, "The lighting program's marketing was left out")
    more = job(
        costs=[
            "PGE_Res_HVAC|2028|2028Q1|PGE|1000|500|200|0|0|0|0|100|0|5000|0",
            "PGE_Res_HVAC|2029|2029Q1|PGE|1100|500|200|0|0|0|0|100|0|0|0",
            "PGE_Com_Ltg|2028|2028Q1|PGE|300|0|900|0|0|0|0|0|0|0|0",
        ]
    )
    v2 = budgets.confirm(analyst, priced(budgets, budgets.upload(analyst, plan, pge, more, "plan_correction.zip"), "plan2"))
    m = monthly.upload(analyst, april, pge, report("202804", measures=[]), "april.zip")
    monthly.price(analyst, m)
    m = monthly.confirm(analyst, SubmissionVersion.objects.get(pk=m.pk))
    submissions.move_deadline(cpuc_admin, april, timezone.now() - timedelta(days=1), "the scene's correction")
    submissions.request_reopen(analyst, m.submission, "A late lighting invoice")
    lighting = "PGE_Com_Ltg|2028|2028Q2|PGE|450|0|0|0|0|0|0|0|0|50|0"
    m2 = monthly.upload(analyst, april, pge, report("202804", measures=[], costs=[lighting]), "april_correction.zip")
    monthly.price(analyst, m2)
    m2 = monthly.confirm(analyst, SubmissionVersion.objects.get(pk=m2.pk))
    for module, policy in reopen.items():
        submissions.set_policy(cpuc_admin, module, reopen_after_due=policy)
    return {"budget_letter": v2.budget_change_letter.pk, "monthly_letter": m2.monthly_change_letter.pk}


def _follows(pge_admin, community, pge, hvac):
    """What the notification settings page shows filled in: chosen PAs, a followed program, reminders; a community member who follows
    a program from its page."""
    notifications.set_follows(pge_admin, pa_scope="chosen", pas=[pge], programs=[hvac])
    notifications.set_reminders(pge_admin, [(14, "budgets", "email"), (2, "", "in_app")])
    notifications.subscribe(pge_admin, "submission.published", module="budgets")
    notifications.follow_program(community, hvac)


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


def _claims(cpuc_admin, pge, pge_admin, analyst, pii_filer):
    """2028's claims: Q1 priced (recorded as the Calculations module records a finished run), confirmed and published, then corrected
    after its due date (a claim withdrawn, with its change letter) with its PII kept in the vault; Q2 uploaded with rejected records (an
    unknown program, another PA's claim) and a PII file that failed its checks; Q3 not started. The analyst and pii_filer already hold
    the claims grants, pii_filer the approved PII grant too."""
    from unittest import mock

    from cedars.modules.claims import service as claims

    from ..test_budgets import group_sums
    from ..test_claims import claim, claims_zip, row, tables

    due = {kind: timezone.now() + timedelta(days=30 * (i + 1)) for i, (kind, _) in enumerate(claims.PERIODS)}
    claims.add_year(cpuc_admin, year=2028, acc_version="2026", due=due)
    q1, q2, q3 = claims.periods(2028)[:3]
    first = tables(
        claim("PGE-2028-001", prg="PGE_Res_HVAC"),
        claim("PGE-2028-002", prg="PGE_Res_HVAC", NumUnits="4"),
        ProgramCost=[row("ProgramCost", PrgID="PGE_Res_HVAC")],
        Site=[row("Site", SiteID="PGE-S1")],
        ContactClaim=[row("ContactClaim", ClaimID="PGE-2028-001", ContactID="PGE-C1")],
    )
    # the accessibility job runs without the engine, so the claims rule set's findings are left out here (tests/test_claims.py runs them)
    for c in first["Claim"]:
        c["ProjectID"] = "PGE-P1"  # two meters of one project (the project key)
    no_rules = mock.patch.object(claims, "_rule_findings", return_value=([], [], ""))
    with no_rules:
        v = claims.upload_claims(analyst, q1, pge, claims_zip(first, full=True), "q1.zip")
    key = "runs/claims/out/_results/group_sums2_measure_level_run.parquet"
    storage.store("results").put_bytes(key, group_sums())
    details = {"job_sha256": v.findings["job_sha256"], "acc": "2026", "release": "0.1.80", "results": {"group_sums": key}}
    v = submissions.record_calculation(None, v, run="claims", input_hash="h", details=details)
    v = claims.confirm(analyst, v)
    # Q1 corrected after its due date: PGE-2028-002 withdrawn, PGE-2028-001's units changed; its publication writes a change letter
    submissions.move_deadline(cpuc_admin, q1, timezone.now() - timedelta(days=1), "the scene's correction")
    reopen = submissions.policy("claims").reopen_after_due
    submissions.set_policy(cpuc_admin, "claims", reopen_after_due="open")
    v2 = claims.request_reopen(analyst, v.submission, "PGE-2028-002 was canceled; PGE-2028-001 had 12 units")
    submissions.set_policy(cpuc_admin, "claims", reopen_after_due=reopen)
    kept = claim("PGE-2028-001", prg="PGE_Res_HVAC", NumUnits="12")
    kept["Claim"][0]["ProjectID"] = "PGE-P1"
    fixed = tables(
        kept,
        ProgramCost=first["ProgramCost"],
        Site=first["Site"],
        ContactClaim=first["ContactClaim"],
        Withdrawal=[row("Withdrawal", ClaimID="PGE-2028-002", WithdrawalReason="The customer canceled the project")],
    )
    with no_rules:
        v2 = claims.upload_claims(analyst, q1, pge, claims_zip(fixed, full=True), "q1_correction.zip")
    contacts = {"PII_Contact.csv": "ContactID,ContactName,ContactEmail\nPGE-C1,A. Customer,a@example.com\n",
                "PII_Site.csv": "SiteID,SiteAddress,ElectricServiceAccountID\nPGE-S1,1 Market St,1234567890\n"}  # fmt: skip
    claims.upload_pii(pii_filer, v2, _zip(contacts), "pii.zip", "mfa")
    details = {**details, "job_sha256": v2.findings["job_sha256"]}
    v2 = claims.confirm(analyst, submissions.record_calculation(None, v2, run="claims2", input_hash="h2", details=details))
    bad = tables(claim("PGE-2028-003", prg="NOPE", quarter="2028Q2"), claim("SCE-2028-1", prg="PGE_Res_HVAC", quarter="2028Q2"))
    with no_rules:
        q2v = claims.upload_claims(analyst, q2, pge, claims_zip(bad, full=True), "q2.zip")
    wrong = {"PII_Contact.csv": "ContactID,ContactName,ContactPhoneNumber\nPGE-C9,A. Customer,415-555-0100\n",
             "PII_Site.csv": "SiteID,SiteAddress\nPGE-S1,1 Market St\n"}  # fmt: skip
    claims.upload_pii(pii_filer, q2v, _zip(wrong), "pii.zip", "mfa")
    # the filing's discussion (BR-09.5): CPUC staff ask, with a file; PG&E answers, so the reply waits on the claims approver's queue
    from cedars.services.submissions import comments

    note = ("review_notes.txt", io.BytesIO(b"PGE-2028-002: 4 units filed, 3 installed."))
    comments.post(cpuc_admin, v, "Please check PGE-2028-002's unit count before the next quarter.", [note])
    comments.post(analyst, v, "It was canceled.\nThe correction withdraws it.")
    restatement = _restatement(analyst, pge, v2, group_sums)
    # correcting the Q2 draft's records in place (BR-02.4): a partial correction waiting in its preview
    from cedars.services.submissions import corrections

    fix = _zip({"Measure.csv": "ClaimID,PrgID,NumUnits\nPGE-2028-003,PGE_Res_HVAC,4\n",
                "Delete.csv": "Table,ClaimID\nClaim,SCE-2028-1\nMeasure,SCE-2028-1\n"})  # fmt: skip
    with no_rules:
        preview = corrections.preview(analyst, q2v, fix, "q2_fix.zip")
    return {
        "restatement": restatement,
        "claims_draft": q2v,
        "claims_preview": preview.token,
        "claims_q1": q1,
        "claims_q2": q2,
        "claims_q3": q3,
        "claims_version": v,
        "claims_corrected": v2,
        "claims_letter": v2.change_letter,
    }


def _restatement(analyst, pge, v, group_sums):
    """The corrected Q1 restated on the 2024 ACC (BR-06.17) for PG&E, both runs finished, so the page shows the split."""
    from cedars.modules.calculations import service as calculations
    from cedars.modules.calculations.models import Run
    from cedars.modules.claims import restatements

    rs = restatements.start(analyst, v, organisation=pge, release="0.1.78", acc_version="2024", reason="The 2024 ACC's effect")
    for role, scale in (("bridge", 1.1), ("restated", 1.4)):
        run = Run.objects.get(pk=rs.runs[role])
        calculations.mark_started(run, worker="scene")
        storage.store("results").put_bytes(run.storage_prefix + "out/_results/group_sums2_measure_level_run.parquet", group_sums(scale))
        calculations.finish(run, exit_code=0, receipt={"packs": {"files": {}}})
    restatements.refresh(rs)
    return rs


def _legacy():
    """Legacy CEDARS years loaded from made-up downloads as the load commands load them: 2023's claims (PG&E's, with a PAC benefit that
    differs from ratio x cost so the reconciliation shows a difference, and the statewide unallocated claims) and the 2021 budget filing."""
    import tempfile

    from cedars.modules.budgets import legacy as budget_legacy
    from cedars.modules.claims import legacy as claims_legacy
    from cedars.services.legacy import service as staging

    from ..legacy_fixtures import csv_text, filing_row, legacy_claim, manifest_row, manifest_text, write_zip

    folder = Path(tempfile.mkdtemp())
    pge_claims = [
        legacy_claim("PGE-2023-0001", "PGE", "PGE_Com_Legacy", 2023),
        legacy_claim("PGE-2023-0002", "PGE", "PGE_Com_Legacy", 2023, **{"PAC Benefits": "49.0"}),
    ]
    statewide = [legacy_claim("SW-2023-0001", "SW", "SW_HVAC_Legacy", 2023)]
    files = [
        ("claims", "2023", "all", write_zip(folder / "claims/all_2023.zip", {"all.csv": csv_text("claims_b", pge_claims)})),
        ("claims", "2023", "SW", write_zip(folder / "claims/sw_2023.zip", {"sw.csv": csv_text("claims_b", statewide)})),
        (
            "filings",
            "2021",
            "all",
            write_zip(
                folder / "filings/all_2021.zip", {"f.csv": csv_text("filings", [filing_row("PGE-2021-1", "PGE", "PGE_Com_Legacy", 2021)])}
            ),
        ),
    ]
    assert staging.stage(None, manifest_text([manifest_row(*f) for f in files]), folder).ok
    assert claims_legacy.load(2023).published == ["PGE", "SW"]
    assert budget_legacy.load(budget_legacy.BY_KEY["2021"]).published == ["PGE"]
    versions = SubmissionVersion.objects.filter(origin=SubmissionVersion.Origin.IMPORTED).select_related("submission__organisation")
    claims_pge = versions.get(submission__deadline__module="claims", submission__organisation__code="PGE")
    return {
        "legacy_claims": claims_pge,
        "legacy_claims_sw": versions.get(submission__deadline__module="claims", submission__organisation__code="SW"),
        "legacy_claims_period": claims_pge.submission.deadline,
        "legacy_budget": (budget := versions.get(submission__deadline__module="budgets")),
        "legacy_filing": budget.submission.deadline,
    }


def _etrm(cpuc_admin):
    """The eTRM sample download as a published permutations pack (the deemed checks' source)."""
    from cedars.services.refdata import etrm

    sample = Path(__file__).parent.parent / "data" / "etrm_permutations_export.csv"
    return refdata.publish_pack(cpuc_admin, etrm.load(cpuc_admin, [sample], label="eTRM published permutations", version="2026-10-30"))


def _releases(cpuc_admin):
    """Release notes: one coming (a limit change, at short notice, so every page shows the banner), one in effect, one withdrawn and a
    draft with a change."""
    from unittest import mock

    from cedars.services.releases import service as releases

    def note(title, days, version="", **change):
        n = releases.create_draft(
            cpuc_admin, title=title, summary=f"{title}: what changes and why.", effective_on=today + timedelta(days=days), version=version
        )
        releases.add_change(cpuc_admin, n, **{"kind": "system", "area": "claims", "text": "Claims uploads are compiled faster.", **change})
        return n

    today = timezone.localdate()
    coming = note("Larger uploads", 7, "0.3.0", kind="limit", limit="upload_mb", limit_value="200 MB", text="Each file may be larger.")
    releases.publish(cpuc_admin, coming, short_notice_reason="The Q4 claims need it.")
    with mock.patch.object(releases, "today", return_value=today - timedelta(days=40)):
        done = note("Release notes and limits", 20, "0.2.0", area="everyone", action="Nothing.")
        releases.publish(cpuc_admin, done)
        gone = note("A rule change", 30, kind="rule", area="budgets")
        releases.publish(cpuc_admin, gone)
    releases.withdraw(cpuc_admin, gone, "Replaced by the 2028 rule set.")
    draft = note("Next release", 45, "0.4.0")
    return {"release_note": coming.pk, "release_done": done.pk, "release_draft": draft.pk}


def _mandates(cpuc_admin, rules):
    """Gaps 33 and 30: a field retiring in 30 days (a new catalog version), and the register of CPUC-mandated changes with a published
    entry (linked to the rules, the field and the version, changed once so it has a history) and a draft."""
    from cedars.services.dictionary import changes
    from cedars.services.releases import mandates

    today = timezone.localdate()
    version, _ = changes.retire(
        cpuc_admin,
        "LevNetBenRIMGas",
        retired_from=today + timedelta(days=30),
        reason="No report or dashboard reads it.",
        usage="Never filed; read by no download in 2025 or 2026.",
        audience="No published dataset shows it.",
        regulatory="The Standard Practice Manual does not require it.",
    )
    entry = mandates.create_draft(
        cpuc_admin,
        reference="Res. E-0000",
        authority="resolution",
        title="Tax credits in the TRC",
        requirement="Count tax credits as a TRC benefit from the 2026 claims.",
        area="both",
        effective_on=today - timedelta(days=200),
        document_url="https://docs.cpuc.ca.gov/example.pdf",
    )
    mandates.add_link(cpuc_admin, entry, "rule_set", str(rules.pk))
    mandates.add_link(cpuc_admin, entry, "field", "TaxCredits")
    mandates.add_link(cpuc_admin, entry, "catalog", str(version.pk))
    mandates.publish(cpuc_admin, entry)
    mandates.update(
        cpuc_admin, entry, status="implemented", implemented_on=today - timedelta(days=10), status_note="In the 2026 rules.", reason="Done"
    )
    draft = mandates.create_draft(
        cpuc_admin,
        reference="D.00-00-001",
        title="A new output",
        requirement="Report a new output.",
        effective_on=today + timedelta(days=90),
    )
    return {"mandate": entry.pk, "mandate_draft": draft.pk, "catalog_retired": version.pk}


def _zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return out.getvalue()


def _session_cookie(user, method, site):
    s = SessionStore()
    s[SESSION_KEY] = str(user.pk)
    s[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    s[HASH_SESSION_KEY] = user.get_session_auth_hash()
    records = [{"method": "password", "at": 0}] + ([{"method": method, "at": 0}] if method != "password" else [])
    s["account_authentication_methods"] = records
    s["account_authentication_methods_user"] = str(user.pk)
    s.create()
    return {"name": settings.SESSION_COOKIE_NAME, "value": s.session_key, "url": site.url}


def _open(browser, site, scene, path, who, **context_args):
    context = browser.new_context(**context_args)
    if who is not None:
        context.add_cookies([_session_cookie(scene[who], "password" if who == "password" else "mfa", site)])
    page = context.new_page()
    from cedars.services.accounts.models import Organisation

    ids = {k: (v.code if isinstance(v, Organisation) else getattr(v, "pk", v)) for k, v in scene.items()}
    response = page.goto(site.url + path.format(**ids))
    assert response.status == 200, f"{path} answered {response.status} at {page.url}"
    if who is not None:  # a check that reached the sign-in page instead would pass without having seen the page
        assert "/accounts/login/" not in page.url, f"{path} sent {who} to {page.url}"
    return context, page


def _violations(page, axe_source):
    page.add_script_tag(content=axe_source)
    # Check what is folded away too: every disclosure on the page is opened, except the header's menus, which are dropdowns. An open
    # dropdown covers whatever is under it, as it should, so each menu is opened on its own and only the header is checked in that state.
    page.evaluate("document.querySelectorAll('details:not(.top nav details)').forEach(d => d.open = true)")
    found = _run_axe(page)
    for i in range(page.evaluate("document.querySelectorAll('.top nav details').length")):
        page.evaluate("(i) => document.querySelectorAll('.top nav details').forEach((d, j) => { d.open = j === i })", i)
        found += [v for v in _run_axe(page, within=".top") if v not in found]
    return found


def _run_axe(page, within="html"):
    result = page.evaluate("async ([tags, within]) => await axe.run(within, {runOnly: {type: 'tag', values: tags}})", [TAGS, within])
    return [
        {"id": v["id"], "impact": v["impact"], "help": v["help"], "targets": [n["target"] for n in v["nodes"]][:5]}
        for v in result["violations"]
    ]


# The header sticks to the top of a window at least 861 px wide and 480 px tall (app.css). A long page with links and fields all the way
# down: going back up through it with Shift+Tab is where a sticky header would hide the focus (2.4.11).
STICKY_PAGE = ("/data/overview/?by=program", None)


def _header_box(page):
    return page.evaluate(
        "(() => { const r = document.querySelector('body > header').getBoundingClientRect(); return [r.top, r.bottom]; })()"
    )


# Seen if any of five points down the middle of an element shows the element itself rather than something over it.
SEEN = """(e) => { const r = e.getBoundingClientRect(), x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
  return [0.1, 0.3, 0.5, 0.7, 0.9].some(f => { const y = r.top + r.height * f; if (y < 0 || y >= innerHeight) return false;
    const hit = document.elementFromPoint(x, y); return hit && (e === hit || e.contains(hit)); }) }"""
FOCUS_SEEN = f"""(() => {{ const e = document.activeElement;
  return e === document.body || !!e.closest('body > header') || ({SEEN})(e); }})()"""
# Chromium centers what it scrolls to on focus; other browsers scroll it just into view, which is where a sticky header covers it. This
# does that from the bottom of the page for every visible link and field in the content, and lists the ones left out of sight.
NEAREST_HIDDEN = f"""(() => {{ const seen = {SEEN}, out = [];
  for (const e of document.querySelectorAll('main a[href], main button, main input, main select, main summary, main [tabindex]')) {{
    if (!e.checkVisibility()) continue;  // folded away in a closed disclosure: not reachable
    window.scrollTo(0, document.documentElement.scrollHeight);
    e.scrollIntoView({{block: 'nearest'}});
    if (!seen(e)) out.push(e.outerHTML.slice(0, 80));
  }}
  return out; }})()"""


def test_header_stays_in_view_and_never_hides_the_focus(browser, site, scene):
    context, page = _open(browser, site, scene, *STICKY_PAGE, viewport={"width": 1280, "height": 720})
    assert page.evaluate("document.documentElement.scrollHeight") > 720 + 400, "the page is too short to show anything"
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    top, bottom = _header_box(page)
    assert top == 0 and bottom > 0, "the header scrolled away"
    hidden = []
    page.evaluate("[...document.querySelectorAll('footer a')].pop().focus()")
    for _ in range(60):  # back up the page with Shift+Tab, as a keyboard user does
        page.keyboard.press("Shift+Tab")
        if not page.evaluate(FOCUS_SEEN):
            hidden.append(page.evaluate("document.activeElement.outerHTML.slice(0, 80)"))
    hidden += page.evaluate(NEAREST_HIDDEN)
    context.close()
    assert not hidden, f"focus hidden under the header: {hidden}"


def test_the_full_header_stays_on_one_row_at_1280(browser, site, scene):
    """A CPUC system administrator has every menu and the account cluster; at 1280 px they fit one row (UAT F16), so the sticky header
    does not double in height."""
    context, page = _open(browser, site, scene, "/data/overview/?by=program", "cpuc_admin", viewport={"width": 1280, "height": 720})
    top_h = page.evaluate("document.querySelector('.top').getBoundingClientRect().height")
    first = page.evaluate("document.querySelector('.top .brand').getBoundingClientRect().top")
    who = page.evaluate("document.querySelector('.top .who').getBoundingClientRect().top")
    context.close()
    assert top_h <= 64 and abs(who - first) < 20, f"header row {top_h:.0f} px tall; account cluster {who - first:.0f} px below the brand"


@pytest.mark.parametrize("viewport", [{"width": 320, "height": 640}, {"width": 1280, "height": 400}], ids=["narrow", "short"])
def test_header_scrolls_away_where_there_is_no_room(browser, site, scene, viewport):
    context, page = _open(browser, site, scene, *STICKY_PAGE, viewport=viewport)
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    _, bottom = _header_box(page)
    context.close()
    assert bottom <= 0, f"the header stays on a {viewport['width']} x {viewport['height']} window"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("name,path,who", PAGES, ids=[p[0] for p in PAGES])
def test_axe_clean(browser, axe_source, site, scene, name, path, who, scheme):
    # axe is injected inline, so this context bypasses the page's CSP; nothing else does
    context, page = _open(browser, site, scene, path, who, color_scheme=scheme, bypass_csp=True)
    found = _violations(page, axe_source)
    context.close()
    assert not found, f"{name} ({scheme}): " + json.dumps(found, indent=1)


# Markup checks axe does not make (UI review 2, 2026-10-31): an id used twice (a label's for and a hint's aria-describedby reach only the
# first; Django gives every form the same id_<field> ids unless the view sets auto_id), a checkbox or radio squeezed below 24 px by a
# flex row, a summary under 24 px tall (2.5.8), a table without a caption, and a label or legend ending in a colon (the field template
# writes none, batch B of the review, and neither do the hand-written labels).
MARKUP = """() => {
  const ids = {}; document.querySelectorAll('[id]').forEach(e => { ids[e.id] = (ids[e.id] || 0) + 1; });
  const seen = e => e.getClientRects().length > 0;
  const size = e => { const r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; };
  return {
    duplicate_ids: Object.keys(ids).filter(k => ids[k] > 1),
    small_boxes: [...document.querySelectorAll('input[type=checkbox], input[type=radio]')].filter(seen)
      .filter(e => size(e)[0] < 24 || size(e)[1] < 24).map(e => e.outerHTML.slice(0, 80) + ' ' + size(e)),
    short_summaries: [...document.querySelectorAll('main summary')].filter(seen).filter(e => size(e)[1] < 24)
      .map(e => e.textContent.trim().slice(0, 60) + ' ' + size(e)),
    tables_without_caption: [...document.querySelectorAll('table')].filter(t => !t.querySelector('caption'))
      .map(t => (t.closest('[aria-label]') || t).getAttribute('aria-label') || t.outerHTML.slice(0, 80)),
    label_colons: [...document.querySelectorAll('label, legend')].map(e => e.textContent.trim()).filter(t => t.endsWith(':')).slice(0, 10),
  }; }"""


@pytest.mark.parametrize("name,path,who", PAGES, ids=[p[0] for p in PAGES])
def test_reflows_at_320px_with_sound_markup(browser, site, scene, name, path, who):
    context, page = _open(browser, site, scene, path, who, viewport={"width": 320, "height": 640})
    page.evaluate("document.querySelectorAll('main details').forEach(d => d.open = true)")  # what is folded away reflows too
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    markup = page.evaluate(MARKUP)
    context.close()
    assert overflow <= 0, f"{name} scrolls sideways by {overflow} px at 320 px wide"
    problems = {k: v for k, v in markup.items() if v}
    assert not problems, f"{name}: " + json.dumps(problems, indent=1)


@pytest.mark.parametrize("who", [None, "pge_admin"], ids=["signed-out", "signed-in"])
def test_the_narrow_header_folds_behind_a_menu_button(browser, site, scene, who):
    """At 320 px the navigation is behind a Menu button, signed in or not (UI review 2, #42), and the button shows and hides it."""
    context, page = _open(browser, site, scene, "/", who, viewport={"width": 320, "height": 640})
    btn = page.locator(".menu-btn")
    assert btn.is_visible() and btn.get_attribute("aria-expanded") == "false"
    assert not page.locator("#main-nav").is_visible()
    btn.click()
    assert btn.get_attribute("aria-expanded") == "true" and page.locator("#main-nav").is_visible()
    btn.click()
    assert not page.locator("#main-nav").is_visible()
    context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("who", [None, "pge_admin"], ids=["signed-out", "signed-in"])
def test_the_tour_by_keyboard(browser, axe_source, site, scene, scheme, who):
    """The front page's guided tour (core/tour.js): opened from the keyboard, each step's heading takes the focus and names the dialog,
    Back on the first step keeps the focus (aria-disabled), Next and the arrow keys move, the step's part of the page is outlined, axe
    finds nothing with the dialog open, and Escape closes it with the focus back on the link that opened it."""
    from cedars.core import help as site_help
    from cedars.services.accounts import service as access

    user = scene[who] if who else None
    orgs = list(access.organisations_of(user)) if user else []
    steps = site_help.tour_steps(
        site_help.Visitor(
            signed_in=bool(user),
            acting_for=bool(orgs),
            sysadmin=bool(user) and access.is_system_admin(user),
            org_admin=any(access.is_org_admin(user, o) for o in orgs),
            steward=bool(user) and access.is_steward(user),
        )
    )
    context, page = _open(browser, site, scene, "/", who, color_scheme=scheme, bypass_csp=True)
    page.add_script_tag(content=axe_source)
    start = page.locator("[data-tour-offer] [data-tour-start]")
    start.focus()
    page.keyboard.press("Enter")
    assert page.locator("#tour").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.id") == "tour-title"
    assert page.locator("#tour-title").text_content() == f"Step 1 of {len(steps)}: {steps[0].title}"
    assert page.evaluate("document.querySelector('.tour-target').getAttribute('data-tour')") == "intro"
    page.locator("[data-tour-back]").focus()
    page.keyboard.press("Enter")  # nothing before the first step; the button keeps the focus
    assert page.evaluate("document.activeElement.hasAttribute('data-tour-back')")
    assert page.locator("[data-tour-back]").get_attribute("aria-disabled") == "true"
    page.locator("[data-tour-next]").focus()
    page.keyboard.press("Enter")
    assert steps[1].title in page.locator("#tour-title").text_content()
    assert page.evaluate("document.querySelector('.tour-target').getAttribute('data-tour')") == steps[1].target
    found = _run_axe(page)
    assert not found, f"tour ({scheme}): " + json.dumps(found, indent=1)
    page.keyboard.press("ArrowRight")
    assert steps[2].title in page.locator("#tour-title").text_content()
    page.keyboard.press("Tab")  # the focus stays inside the modal dialog
    assert page.evaluate("!!document.activeElement.closest('#tour')")
    if who:  # a header menu is opened for its step and closed after it
        i = next(k for k, step in enumerate(steps) if step.target == "nav-filings")
        page.evaluate("(n) => { for (let k = 0; k < n; k++) document.querySelector('[data-tour-next]').click(); }", i - 2)
        assert page.evaluate("document.querySelector('[data-tour=nav-filings]').open")
        found = _run_axe(page)
        assert not found, f"tour, menu open ({scheme}): " + json.dumps(found, indent=1)
        page.evaluate("(n) => { for (let k = 0; k < n; k++) document.querySelector('[data-tour-next]').click(); }", 3)
        assert not page.evaluate("document.querySelector('[data-tour=nav-filings]').open")
    page.keyboard.press("Escape")
    assert not page.locator("#tour").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.hasAttribute('data-tour-start')")
    assert page.locator(".tour-target").count() == 0
    context.close()


def test_the_tour_without_scripts(browser, site, scene):
    """Without scripts the tour link opens the written tour, which lists every step."""
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(f"{site.url}/")
    assert page.locator("#tour").evaluate("d => d.open") is False
    page.locator("[data-tour-offer] [data-tour-start]").click()
    assert page.url == f"{site.url}/help/tour/" and page.locator("ol.tour-list > li").count() >= 6
    context.close()


def test_other_filters_by_keyboard(browser, site, scene):
    """The Programs catalog's Other filters panel, with the script: opened, a field chosen and a value ticked from the keyboard, applied
    with Enter; the link carries only that filter. Without the script, "Show its options" shows the field's inputs."""
    context, page = _open(browser, site, scene, "/programs/", None)
    summary = page.locator(".other-filters > summary")
    summary.focus()
    page.keyboard.press("Enter")
    assert page.locator(".other-filters").evaluate("d => d.open")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "other-field"
    assert page.locator(".other-pick button").is_hidden()  # only needed without the script
    page.select_option("#other-field", "status")
    assert page.locator(".other-field[data-field=status]").is_visible()
    assert page.locator(".other-field[data-field=bp_sector]").is_hidden()
    page.keyboard.press("Tab")
    page.keyboard.press("Space")
    assert page.evaluate("document.activeElement.name") == "status" and page.evaluate("document.activeElement.checked")
    value = page.evaluate("document.activeElement.value")
    with page.expect_navigation():
        page.locator("form.catalog-filters > button", has_text="Apply").press("Enter")
    assert page.url == f"{site.url}/programs/?status={value}"
    assert page.locator(".chips").is_visible()
    context.close()

    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(f"{site.url}/programs/")
    page.locator(".other-filters > summary").click()
    page.select_option("#other-field", "end_year")
    assert page.locator(".other-field[data-field=end_year]").is_hidden()
    page.locator(".other-pick button").click()
    assert page.locator(".other-filters").evaluate("d => d.open")
    assert page.locator(".other-field[data-field=end_year]").is_visible()
    page.fill("input[name=end_year_from]", "2030")
    page.locator("form.catalog-filters > button", has_text="Apply").click()
    assert "end_year_from=2030" in page.url and "End year: 2030 or later" in page.locator(".chips").inner_text()
    context.close()


# ---------- Page help (Jake, 2026-10-31): the (?) beside every page's heading, its guide and the tour of the page ----------


@pytest.mark.parametrize("name,path,who", PAGES, ids=[p[0] for p in PAGES])
def test_every_page_has_help_and_its_tour_finds_the_page(browser, site, scene, name, path, who):
    """The (?) is beside the heading, and every tour step not marked optional finds its part of the page (cedars/core/page_help.py):
    a section renamed or removed fails here rather than leaving a tour that points at nothing."""
    context, page = _open(browser, site, scene, path, who)
    if name in HELP_ITSELF:  # a page's help on a page of its own has no (?) about itself (UI review 2, #41)
        assert page.locator(".page-help").count() == 0, f"{name}: a help page with a (?)"
        context.close()
        return
    assert page.locator(".page-title > h1 + .page-help").count() == 1, f"{name}: no (?) beside the heading"
    steps = page.evaluate("window.cedarsTourFound ? window.cedarsTourFound() : null")
    context.close()
    assert steps, f"{name}: no tour"
    missing = [s["title"] for s in steps if not s["found"] and not s["optional"]]
    assert not missing, f"{name}: tour steps whose part of the page is missing: {missing}"


HELP_ITSELF = {"help-pages", "help-page", "help-page-front"}  # core/help/pages.html and page.html (page_help.WITHOUT_HELP)

HELP_PAGES = [
    ("/data/overview/?by=sector", None),
    ("/budgets/filings/{tual}/", "analyst"),
    ("/calculations/runs/{finished_run}/", "analyst"),
    ("/documents/{deliverable}/", "cpuc_admin"),
]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("path,who", HELP_PAGES, ids=[p[0].split("/")[1] for p in HELP_PAGES])
def test_page_help_by_keyboard(browser, axe_source, site, scene, path, who, scheme):
    """The (?) opens a modal dialog with the page's guide, named by its heading, which takes the focus; axe finds nothing with it open;
    Escape closes it with the focus back on the (?). Take a tour of this page closes it and starts the tour, and closing the tour puts
    the focus back on the (?) too."""
    context, page = _open(browser, site, scene, path, who, color_scheme=scheme, bypass_csp=True)
    page.add_script_tag(content=axe_source)
    button = page.locator(".page-help")
    assert button.get_attribute("aria-haspopup") == "dialog"
    box = button.bounding_box()
    assert box["width"] >= 24 and box["height"] >= 24
    button.focus()
    page.keyboard.press("Enter")
    assert page.locator("#page-help").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.id") == "page-help-title"
    found = _run_axe(page)
    assert not found, f"page help ({scheme}): " + json.dumps(found, indent=1)
    page.keyboard.press("Escape")
    assert not page.locator("#page-help").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.classList.contains('page-help')")
    page.keyboard.press("Enter")
    page.locator("#page-help [data-tour-start]").focus()
    page.keyboard.press("Enter")
    assert not page.locator("#page-help").evaluate("d => d.open") and page.locator("#tour").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.id") == "tour-title"
    assert page.locator("#tour-title").text_content().startswith("Step 1 of ")
    assert page.locator(".tour-target").count() == 1
    found = _run_axe(page)
    assert not found, f"page tour ({scheme}): " + json.dumps(found, indent=1)
    page.keyboard.press("ArrowRight")
    assert page.locator("#tour-title").text_content().startswith("Step 2 of ")
    page.keyboard.press("Escape")
    assert not page.locator("#tour").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.classList.contains('page-help')")
    context.close()


def test_page_help_without_scripts(browser, site, scene):
    """Without scripts the (?) is a link to the page's help on a page of its own, with the tour written out and a way back."""
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(f"{site.url}/rules/")
    page.locator(".page-help").click()
    assert page.url.startswith(f"{site.url}/help/pages/rules/index/?from=")
    assert page.locator("ol.tour-list > li").count() >= 2
    assert page.locator("a", has_text="Back to the page").get_attribute("href") == "/rules/"
    assert page.locator("a", has_text="Take the tour on the page").get_attribute("href") == "/rules/?tour=1"
    context.close()


def test_the_tour_leaves_disclosures_as_they_were(browser, site, scene):
    """A step that points at a disclosure opens it and closes it after; every other disclosure stays as the visitor left it
    (UI review 2, #40)."""
    opened = "() => Array.from(document.querySelectorAll('main details')).map(d => d.open)"

    def tour(page):
        states = []
        page.locator(".page-help").click()
        page.locator("#page-help [data-tour-start]").click()
        while True:
            states.append((page.locator("#tour-title").text_content(), page.evaluate(opened)))
            nxt = page.locator("[data-tour-next]")
            if nxt.text_content() == "Finish":
                nxt.click()
                return states
            nxt.click()

    context, page = _open(browser, site, scene, "/admin/organizations/{pge}/", "pge_admin")
    page.evaluate("() => { document.querySelector('main details').open = true; }")  # a member's disclosure the visitor opened
    before = page.evaluate(opened)
    assert all(states == before for _title, states in tour(page))
    assert page.evaluate(opened) == before
    context.close()

    context, page = _open(browser, site, scene, "/programs/", None)
    other = "() => document.querySelector('main details.other-filters').open"
    assert not page.evaluate(other)
    states = tour(page)
    assert any("Other filters" in title and state[0] for title, state in states)  # open while its step is shown
    assert not page.evaluate(other)  # and closed again after
    context.close()


def test_a_page_tour_starts_from_its_link(browser, site, scene):
    """The written tour's "Take the tour on the page" opens the page with its tour running."""
    context, page = _open(browser, site, scene, "/rules/?tour=1", None)
    assert page.locator("#tour").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.id") == "tour-title"
    context.close()


def _accessibility_checks(cpuc_admin, document):
    """The document accessibility check (BR-10.3): the scene's files are not real PDFs, so their checks say "not checked"; the report's
    main file is given findings (shown on its document page and as {access_check}), and one is reviewed."""
    from cedars.services.accessibility import service as accessibility
    from cedars.services.accessibility.models import FileCheck

    main = document.versions.order_by("-number").first().files.get(position=0)
    fc = FileCheck.objects.get(source="document", ref=str(main.pk))
    found = [{"code": "pdf_untagged", "where": "", "count": 1}, {"code": "pdf_no_language", "where": "", "count": 1}]
    FileCheck.objects.filter(pk=fc.pk).update(findings=found, note="")
    other = FileCheck.objects.exclude(pk=fc.pk).filter(source="document").first()
    FileCheck.objects.filter(pk=other.pk).update(findings=[{"code": "pdf_image_only", "where": "", "count": 1}], note="")
    accessibility.review(cpuc_admin, other, "An accessible copy is filed as the next version.")
    return {"access_check": fc.pk, "access_reviewed": other.pk}
