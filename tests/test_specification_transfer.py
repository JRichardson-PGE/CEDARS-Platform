"""Moving a specification version between environments (cedars.services.specifications.transfer): a version is exported as one
package with a manifest of SHA-256s, and imported elsewhere as a draft after a preview of what it makes and what the environment lacks;
the version's page shows whether it is still exactly what was exported."""

import io
import json
import zipfile
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary.models import ValueList
from cedars.services.rules.models import Rule, RuleSet
from cedars.services.specifications import editor, transfer
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import SpecificationVersion

from .conftest import sign_in

pytestmark = pytest.mark.django_db
CODES = 'A, Alpha\nB, "Beta, the second", 2028, 2030\n'


@pytest.fixture
def base(cpuc_admin):
    """A published monthly version whose rule set has one rule."""
    published = RuleSet.objects.create(code="monthly_test", label="Monthly test", effective_from=timezone.localdate() - timedelta(days=30))
    Rule.objects.create(rule_set=published, rule_id="Vtest01", severity="warning", table="Measure", field="PreDesc", kind="rule",
                        definition={"field": "PreDesc", "rule": "equals", "value": "none", "connector": "all", "conds": []},
                        message="PreDesc should equal none", fix="Fill in PreDesc.", modules=["monthly"])  # fmt: skip
    RuleSet.objects.filter(pk=published.pk).update(status="published")
    doc = specifications.stored_starting("monthly")
    doc["version"]["name"] = "Monthly with a rule"
    return specifications.load(doc, rule_set=RuleSet.objects.get(pk=published.pk))


@pytest.fixture
def tested(cpuc_admin, base):
    """The sandbox's draft: a new field with a new value list. Exported, then discarded, as if this were the other environment."""
    d = editor.start_draft(cpuc_admin, base, "Monthly reports 2029", "The 2029 reporting changes")
    editor.add_field(cpuc_admin, d, "Commitments", "ReportingNote", type="text", definition="A note on the month", reason="r")
    editor.new_value_list(cpuc_admin, d.tables.get(name="Commitments").fields.get(name="ReportingNote"), "reporting_note_kind",
                          codes=CODES, reason="New codes")  # fmt: skip
    content = transfer.export(cpuc_admin, d)
    editor.discard(cpuc_admin, d, "Moved to the other environment")
    assert not ValueList.objects.filter(name="reporting_note_kind").exists()
    return content


def parts(content: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(content))
    return {n: z.read(n) for n in z.namelist()}


def repack(content: bytes, **changed: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n, b in parts(content).items():
            z.writestr(n, changed.get(n.replace(".", "_"), b))
    return out.getvalue()


def test_the_package_holds_the_whole_version_with_a_manifest(cpuc_admin, base):
    content = transfer.export(cpuc_admin, base)
    p = parts(content)
    manifest = json.loads(p["manifest.json"])
    assert set(p) == {"manifest.json", "version.json", "rules.json", "value_lists.json"}
    assert manifest["specification"] == "monthly" and manifest["version"] == "Monthly with a rule" and manifest["environment"] == "test"
    assert all(transfer.sha256(p[n]) == digest for n, digest in manifest["parts"].items())
    assert manifest["fingerprint"] == specifications.fingerprint(specifications.as_document(base))
    assert [r["rule_id"] for r in json.loads(p["rules.json"])["rules"]] == ["Vtest01"]
    assert "@" not in p["manifest.json"].decode()  # who exported it by name only
    event = AuditEvent.objects.get(action="specification.exported")
    assert event.after["sha256"] == transfer.sha256(content) and event.after["file"] == "monthly_monthly-with-a-rule_package.zip"


def test_an_import_is_previewed_then_made_a_draft_that_is_what_was_exported(cpuc_admin, base, tested):
    p = transfer.preview(cpuc_admin, tested)
    assert p.blocking == [] and p.name == "Monthly reports 2029" and p.current == base
    assert p.lists_made == ["reporting_note_kind"]
    assert any("ReportingNote" in d for d in p.differences)
    assert not SpecificationVersion.objects.filter(name="Monthly reports 2029").exists()  # the preview made nothing
    assert not ValueList.objects.filter(name="reporting_note_kind").exists()

    with pytest.raises(editor.EditError, match="Say why"):
        transfer.import_package(cpuc_admin, tested, "")
    v = transfer.import_package(cpuc_admin, tested, "Tested in the sandbox", "monthly_package.zip")
    assert v.status == "draft" and v.based_on == base and v.source == "Imported from the test environment"
    assert v.tables.get(name="Commitments").fields.get(name="ReportingNote").value_list == "reporting_note_kind"
    assert v.rule_set.status == "draft" and [r.rule_id for r in v.rule_set.rules.all()] == ["Vtest01"]
    vl = ValueList.objects.get(name="reporting_note_kind")
    assert vl.made_in == editor.made_in(v)  # made with the draft: published with it, dropped if it is discarded
    assert v.examples.exists()
    prov = transfer.provenance(v)
    assert prov.identical and prov.environment == "test" and prov.sha256 == transfer.sha256(tested)
    event = AuditEvent.objects.get(action="specification.imported")
    assert event.after["sha256"] == transfer.sha256(tested) and event.after["file"] == "monthly_package.zip"
    editor.change_field(cpuc_admin, v.tables.get(name="Commitments").fields.get(name="ReportingNote"), definition="Changed here")
    prov = transfer.provenance(SpecificationVersion.objects.get(pk=v.pk))
    assert not prov.identical and any("ReportingNote definition" in c for c in prov.changes)


def test_what_stops_an_import(cpuc_admin, base, tested, monkeypatch):
    editor.start_draft(cpuc_admin, base, "Another draft", "why")
    p = transfer.preview(cpuc_admin, tested)
    assert p.blocking == [
        "Monthly reports already has a draft, Another draft: publish or discard it first, since a specification has one draft."
    ]
    with pytest.raises(transfer.PackageError, match="already has a draft"):
        transfer.import_package(cpuc_admin, tested, "why")


def test_an_engine_release_not_registered_here_stops_it(cpuc_admin, base, monkeypatch):
    monkeypatch.setattr(editor, "_releases", [lambda: [{"release": "0.1.94", "active": True, "default": True, "accs": ["2026"]}]])
    d = editor.start_draft(cpuc_admin, base, "Monthly reports 2029", "why")
    d = editor.update_settings(cpuc_admin, d, name=d.name, summary="s", engine_release="0.1.94", reason="r")
    content = transfer.export(cpuc_admin, d)
    editor.discard(cpuc_admin, d, "moved")
    monkeypatch.setattr(editor, "_releases", [lambda: [{"release": "0.1.95", "active": True, "default": True, "accs": ["2026"]}]])
    assert transfer.preview(cpuc_admin, content).blocking == [
        "ReCET 0.1.94 is not a registered engine release: register it on Engine releases first."
    ]


def test_a_changed_or_foreign_file_is_refused(cpuc_admin, tested):
    doc = json.loads(parts(tested)["version.json"])
    doc["tables"][0]["guidance"] = "Changed after export"
    with pytest.raises(transfer.PackageError, match="does not match the SHA-256"):
        transfer.preview(cpuc_admin, repack(tested, version_json=json.dumps(doc).encode()))
    with pytest.raises(transfer.PackageError, match="not a specification package"):
        transfer.preview(cpuc_admin, b"not a zip")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("other.txt", "x")
    with pytest.raises(transfer.PackageError, match="no manifest"):
        transfer.preview(cpuc_admin, out.getvalue())


def test_only_a_system_administrator_exports_or_imports(base, pge_admin, tested):
    with pytest.raises(AccessDenied):
        transfer.export(pge_admin, base)
    with pytest.raises(AccessDenied):
        transfer.preview(pge_admin, tested)


def test_the_export_and_import_pages(client, cpuc_admin, pge_admin, base, tested):
    sign_in(client, cpuc_admin)
    page = client.get(reverse("specifications:version", args=["monthly", base.slug])).content.decode()
    assert 'id="export-form"' in page and 'id="provenance"' not in page
    r = client.post(reverse("specifications:export_version", args=["monthly", base.slug]))
    assert r.status_code == 200 and r["Content-Type"] == "application/zip" and parts(r.content)["manifest.json"]
    assert 'id="import-link"' in client.get(reverse("specifications:index")).content.decode()
    url = reverse("specifications:import_version")
    assert 'id="import-form"' in client.get(url).content.decode()
    text = client.post(url, {"file": SimpleUploadedFile("monthly_package.zip", tested)}).content.decode()
    assert 'id="import-preview"' in text and 'id="import-confirm"' in text and "reporting_note_kind" in text
    confirm = reverse("specifications:confirm_import")
    refused = client.post(confirm, {"sha256": transfer.sha256(tested), "reason": ""})
    assert refused.status_code == 302 and "held=1" in refused.url
    r = client.post(confirm, {"sha256": transfer.sha256(tested), "reason": "Tested in the sandbox"})
    v = SpecificationVersion.objects.get(name="Monthly reports 2029")
    assert r.status_code == 302 and r.url == reverse("specifications:edit", args=["monthly", v.slug])
    page = client.get(reverse("specifications:version", args=["monthly", v.slug])).content.decode()
    assert 'id="provenance"' in page and "Identical" in page
    client.logout()
    sign_in(client, pge_admin)
    assert client.get(url).status_code == 403
    assert client.post(reverse("specifications:export_version", args=["monthly", base.slug])).status_code == 403
