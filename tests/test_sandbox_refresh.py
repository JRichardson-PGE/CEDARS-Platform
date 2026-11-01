"""Sandbox refresh (BR-11.2): production's published, non-PII data written to a bundle and added to the sandbox. Production and the
sandbox are one test database here: production's rows are made inside a savepoint, the bundle written, and the savepoint rolled back,
which leaves the database as a sandbox that has never seen them."""

from datetime import date

import pytest
from django.core.management import CommandError, call_command
from django.db import transaction

from cedars.core import storage
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary.models import CatalogVersion, Code, Field, ValueList, ValueListVersion
from cedars.services.refdata.models import PackVersion, TieOut
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program, ProgramVersion
from cedars.services.rules.models import Rule, RuleSet
from cedars.services.sandbox import bundle
from cedars.services.sandbox.models import CopiedRecord, Refresh
from cedars.services.snapshots import service as snapshots
from cedars.services.snapshots.models import Snapshot
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Submission, SubmissionVersion
from tests.test_snapshots import claims_deadline, confirm, priced

pytestmark = pytest.mark.django_db


@pytest.fixture
def filer(make_user, pge, pge_admin):
    u = make_user("analyst@pge.example.org")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


def production(cpuc_admin, pge, filer, capture):
    """Published data of every kind the bundle carries, and some it must leave out."""
    mce = access.create_organisation(cpuc_admin, code="MCE", name="MCE", kind="pa", pa_type="CCA")
    registry.create_program(cpuc_admin, prg_id="MCE_Res_01", administrator=mce, valid_from=date(2028, 1, 1), name="Home upgrades")
    rs = RuleSet.objects.create(code="claims", label="Claims rules", effective_from=date(2028, 1, 1), created_by=cpuc_admin)
    Rule.objects.create(rule_set=rs, rule_id="V000001", severity="reject", table="Measure", field="NTGRkWh", message="Give it", kind="rule")
    RuleSet.objects.filter(pk=rs.pk).update(status="published", published_by=cpuc_admin)
    RuleSet.objects.create(code="claims", label="A draft", effective_from=date(2029, 1, 1))  # a draft: not copied
    pack = PackVersion.objects.create(
        kind="acc_electric", label="2026 ACC", version="v1b", status="published", fingerprint="f" * 64,
        storage_uri="s3://refdata/acc/2026/", manifest={"acc.parquet": {"sha256": "a" * 64, "bytes": 10}}, registered_by=cpuc_admin,
    )  # fmt: skip
    TieOut.objects.create(pack=pack, name="control_totals", passed=True, recorded_by=cpuc_admin)
    PackVersion.objects.create(kind="acc_electric", label="2028 ACC", status="draft", fingerprint="d" * 64, storage_uri="s3://x/")
    cat = CatalogVersion.objects.create(label="ReCET 0.1.99", loaded_by=cpuc_admin)
    Field.objects.create(version=cat, name="NTGRkWh", kind="input")
    vl = ValueList.objects.get_or_create(name="sector", defaults={"label": "Sector"})[0]
    number = (vl.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    vlv = ValueListVersion.objects.create(value_list=vl, number=number, effective_from=date(2030, 1, 1), reason="new code")
    Code.objects.create(version=vlv, code="Agricultural Plus")
    ValueListVersion.objects.filter(pk=vlv.pk).update(status="published")
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, rows=4), capture)
    return {"value_list_version": number, "version": v.pk}


def make_bundle(tmp_path, cpuc_admin, pge, filer, capture):
    """Production's rows in a savepoint, the bundle written, then everything but the bundle (and the files) rolled back."""
    where = tmp_path / "bundle"
    with transaction.atomic():
        made = production(cpuc_admin, pge, filer, capture)
        snap = Snapshot.objects.get(version_id=made["version"])
        made["snapshot"] = snap.pk
        made["bytes"] = storage.store("snapshots").get_bytes(snap.key)
        made["key"] = snap.key
        manifest = bundle.export(str(where))
        transaction.set_rollback(True)
    storage.store("snapshots").delete_prefix(made["key"].rsplit("/", 1)[0] + "/")
    return where, manifest, made


def test_the_specs_copy_no_person_and_never_the_vault():
    assert bundle.check_specs() == []
    labels = {label.lower() for label, _, _ in bundle.all_specs()}
    assert not any(label.startswith(("vault.", "audit.", "notifications.")) for label in labels)
    assert not labels & {"accounts.user", "accounts.membership", "accounts.grant", "accounts.apitoken", "submissions.comment"}


def test_the_bundle_holds_only_whats_published(tmp_path, cpuc_admin, pge, filer, django_capture_on_commit_callbacks):
    where, manifest, made = make_bundle(tmp_path, cpuc_admin, pge, filer, django_capture_on_commit_callbacks)
    assert manifest["environment"] == "test" and manifest["format"] == bundle.FORMAT
    import gzip
    import json

    data = json.loads(gzip.open(where / "data.json.gz", "rt").read())
    assert [r["fields"]["label"] for r in data["rules.RuleSet"]] == ["Claims rules"]
    assert [r["fields"]["label"] for r in data["refdata.PackVersion"]] == ["2026 ACC"]
    assert "CPUC" not in [r["fields"]["code"] for r in data["accounts.Organisation"]]
    text = json.dumps(data)
    assert "admin@cpuc.example.org" not in text and "analyst@pge.example.org" not in text  # no person, anywhere
    [version] = data["submissions.SubmissionVersion"]
    assert "files" not in version["fields"] and "created_by" not in version["fks"]
    assert (where / f"files/{made['snapshot']}.parquet").read_bytes() == made["bytes"]


def test_a_refresh_adds_what_the_sandbox_lacks(tmp_path, settings, cpuc_admin, pge, filer, django_capture_on_commit_callbacks):
    where, manifest, made = make_bundle(tmp_path, cpuc_admin, pge, filer, django_capture_on_commit_callbacks)
    assert not Organisation.objects.filter(code="MCE").exists() and not Snapshot.objects.exists()
    settings.ENVIRONMENT = "sandbox"
    call_command("refresh_sandbox", "--from", str(where))
    refresh = Refresh.objects.get()
    assert refresh.status == "done" and refresh.bundle_environment == "test"

    mce = Organisation.objects.get(code="MCE")
    assert mce.pa_type == "CCA" and list(mce.codes.values_list("code", flat=True)) == ["MCE"]
    assert Program.objects.get(prg_id="MCE_Res_01").administrator == mce
    assert ProgramVersion.objects.get(program__prg_id="MCE_Res_01").created_by is None
    rs = RuleSet.objects.get(code="claims", effective_from=date(2028, 1, 1))
    assert rs.status == "published" and rs.published_by is None and rs.rules.count() == 1
    assert not RuleSet.objects.filter(label="A draft").exists()
    pack = PackVersion.objects.get(label="2026 ACC")
    assert pack.storage_uri == "s3://refdata/acc/2026/" and pack.tie_outs.get().recorded_by is None
    assert not PackVersion.objects.filter(label="2028 ACC").exists()
    assert CatalogVersion.objects.get(label="ReCET 0.1.99").fields.count() == 1
    vlv = ValueListVersion.objects.get(value_list__name="sector", number=made["value_list_version"])
    assert vlv.status == "published" and list(vlv.codes.values_list("code", flat=True)) == ["Agricultural Plus"]

    snap = Snapshot.objects.get(pk=made["snapshot"])
    assert storage.store("snapshots").get_bytes(snap.key) == made["bytes"]
    assert snap.version.status == "published" and snap.version.files == [] and snap.version.created_by is None
    assert snap.version.submission.organisation == pge
    assert list(snapshots.current_release("claims").snapshots.all()) == [snap]  # the dashboards read it
    assert snap.pii_scan.outcome == "clear"
    assert CopiedRecord.objects.filter(refresh=refresh, table="snapshots.snapshot", local_id=str(snap.pk)).exists()
    assert AuditEvent.objects.filter(action="sandbox.refreshed").exists()

    # a second refresh of the same bundle adds nothing
    call_command("refresh_sandbox", "--from", str(where))
    again = Refresh.objects.order_by("-started_at").first()
    assert sum(c["added"] for c in again.counts.values()) == 0
    assert again.counts["rules.RuleSet"]["matched"] == 1

    # the sandbox's clean-up of old test filings leaves production's copied filing alone
    old = list(submissions.expired_sandbox_filings(cutoff=snap.version.submission.created_at.replace(year=2100)))
    assert f"submission:{snap.version.submission_id}" not in [ref for ref, _ in old]


def test_a_clash_with_the_sandboxs_own_records_is_listed(tmp_path, settings, cpuc_admin, pge, filer, django_capture_on_commit_callbacks):
    where, manifest, made = make_bundle(tmp_path, cpuc_admin, pge, filer, django_capture_on_commit_callbacks)
    # the sandbox has its own test filing for the same deadline, and its own draft rules for the same date
    own = priced(filer, claims_deadline(cpuc_admin, 1), pge, rows=2)
    RuleSet.objects.create(code="claims", label="Tester's draft", effective_from=date(2028, 1, 1))
    settings.ENVIRONMENT = "sandbox"
    refresh = bundle.load(str(where))
    why = {s["table"]: s["why"] for s in refresh.skipped}
    assert "its own record" in why["submissions.Submission"]
    assert "its own draft version" in why["rules.RuleSet"]
    assert "was not copied" in why["snapshots.Snapshot"]
    assert Submission.objects.get().versions.get() == SubmissionVersion.objects.get(pk=own.pk)  # the tester's filing is untouched
    assert RuleSet.objects.get(code="claims").label == "Tester's draft"
    assert Organisation.objects.filter(code="MCE").exists()  # the rest still came


def test_each_side_refuses_the_other_ones_job(tmp_path, settings, cpuc_admin, pge, filer, django_capture_on_commit_callbacks):
    settings.ENVIRONMENT = "sandbox"
    with pytest.raises(CommandError, match="made in production"):
        call_command("export_sandbox_bundle", "--to", str(tmp_path / "b"))
    settings.ENVIRONMENT = "test"
    where, manifest, made = make_bundle(tmp_path, cpuc_admin, pge, filer, django_capture_on_commit_callbacks)
    settings.ENVIRONMENT = "prod"
    with pytest.raises(CommandError, match="this is prod"):
        call_command("refresh_sandbox", "--from", str(where))
    settings.ENVIRONMENT = "sandbox"
    (where / "data.json.gz").write_bytes(b"changed")
    with pytest.raises(CommandError, match="does not match"):
        call_command("refresh_sandbox", "--from", str(where))
    assert not Refresh.objects.exists()
