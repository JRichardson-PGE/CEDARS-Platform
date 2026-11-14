"""Editing a draft in a workbook (cedars.services.specifications.workbook): the draft downloads as a workbook, is changed in Excel and
loaded back. Every difference is shown first and nothing changes until it is confirmed; then each change is made through the editor,
checked and audited as on its own page, or none is."""

import io
import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.rules.models import Rule, RuleSet
from cedars.services.specifications import editor, workbook
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification

from .conftest import sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def draft(cpuc_admin):
    """A monthly draft whose rule set has one rule on Measure.PreDesc."""
    published = RuleSet.objects.create(code="monthly_test", label="Monthly test", effective_from=timezone.localdate() - timedelta(days=30))
    Rule.objects.create(rule_set=published, rule_id="Vtest01", severity="warning", table="Measure", field="PreDesc", kind="rule",
                        definition={"field": "PreDesc", "rule": "equals", "value": "none", "connector": "all",
                                    "conds": [["Measure", "PreDesc", "equals", "x"]]},
                        message="When PreDesc is x, PreDesc should equal none", fix="Fill in PreDesc.", modules=["monthly"])  # fmt: skip
    RuleSet.objects.filter(pk=published.pk).update(status="published")
    doc = specifications.stored_starting("monthly")
    doc["version"]["name"] = "Monthly with a rule"
    base = specifications.load(doc, rule_set=RuleSet.objects.get(pk=published.pk))
    return editor.start_draft(cpuc_admin, base, "Monthly reports 2029", "The 2029 reporting changes")


def edited(content: bytes, change) -> bytes:
    wb = load_workbook(io.BytesIO(content))
    change(wb)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def header(ws) -> list[str]:
    return [c.value for c in ws[1]]


def row_of(ws, **match) -> int:
    h = header(ws)
    for r in range(2, ws.max_row + 1):
        if all(ws.cell(r, h.index(k) + 1).value == v for k, v in match.items()):
            return r
    raise AssertionError(f"no row {match}")


def put(ws, r: int, column: str, value) -> None:
    ws.cell(r, header(ws).index(column) + 1).value = value


def unlocked(v, table: str, skip=()) -> str:
    lock = editor.locks(v)
    return next(f.name for f in v.tables.get(name=table).fields.all() if not lock.get((table, f.name)) and f.name not in skip)


def test_the_workbook_as_downloaded_changes_nothing(cpuc_admin, draft):
    content = workbook.draft_xlsx(draft)
    wb = load_workbook(io.BytesIO(content))
    assert wb.sheetnames == ["About", "Settings", "Tables", "Fields", "Rules"]
    assert workbook.read(draft, content) == []
    assert workbook.preview(cpuc_admin, draft, content) == ([], [])


def test_every_difference_is_shown_first_then_made_and_audited(cpuc_admin, draft):
    gone = unlocked(draft, "Measure", skip=("PreDesc",))
    changed = unlocked(draft, "Measure", skip=("PreDesc", gone))

    def change(wb):
        put(wb["Settings"], row_of(wb["Settings"], Setting="Summary"), "Value", "The 2029 reporting changes, from the workbook")
        put(wb["Tables"], row_of(wb["Tables"], Table="Commitments"), "Guidance", "One row per commitment.")
        f = wb["Fields"]
        put(f, row_of(f, Table="Measure", Field="PreDesc"), "Rename to", "PreDescription")
        r = row_of(f, Table="Measure", Field=changed)
        put(f, r, "Definition", "What the commitment is, in the workbook's words")
        put(f, r, "Also accepted as", "CommitNote")
        f.delete_rows(row_of(f, Table="Measure", Field=gone))
        f.append(["Commitments", "ReportingNote", "", "Optional", "Text", 500, None, "", "", "", "", "", "A note on the month", "", ""])
        rules = wb["Rules"]
        put(rules, row_of(rules, Rule="Vtest01"), "Severity", "Rejects the record")
        test = {"field": "PreDescription", "rule": "is not null", "value": "", "connector": "all", "conds": []}
        rules.append(["", "Warning", "Measure", "PreDescription", "rule", json.dumps(test), "", ""])

    content = edited(workbook.draft_xlsx(draft), change)
    changes, problems = workbook.preview(cpuc_admin, draft, content)
    words = [c.words for c in changes]
    assert problems == []
    assert words[0].startswith("Settings: summary from ") and words[0].endswith(" to The 2029 reporting changes, from the workbook.")
    assert "Commitments: guidance to One row per commitment.." in words
    assert f"Measure: remove the field {gone}." in words
    assert "Measure.PreDesc: rename it PreDescription (its old name is still accepted)." in words
    assert any(w.startswith(f"Measure.{changed}: other names from") and "definition from" in w for w in words)
    assert "Commitments: add the field ReportingNote." in words
    assert any(w.startswith("Rule Vtest01 (Measure.PreDesc): change its test") for w in words)
    assert any(w.startswith("New rule on Measure.PreDescription") for w in words)
    summary = draft.summary
    draft.refresh_from_db()
    assert draft.summary == summary  # nothing changed by the preview
    assert draft.tables.get(name="Measure").fields.filter(name="PreDesc").exists()
    assert not AuditEvent.objects.filter(action="specification.field_renamed").exists()

    with pytest.raises(editor.EditError, match="Say why"):
        workbook.load(cpuc_admin, draft, content, "")
    done = workbook.load(cpuc_admin, draft, content, "The working group's sheet")
    assert len(done) == len(changes)
    draft.refresh_from_db()
    assert draft.summary == "The 2029 reporting changes, from the workbook"
    assert draft.tables.get(name="Commitments").guidance == "One row per commitment."
    measure = draft.tables.get(name="Measure")
    assert not measure.fields.filter(name=gone).exists()
    renamed = measure.fields.get(name="PreDescription")
    assert "PreDesc" in renamed.other_names
    c = draft.tables.get(name="Measure").fields.get(name=changed)
    assert c.definition == "What the commitment is, in the workbook's words" and c.other_names == ["CommitNote"]
    assert draft.tables.get(name="Commitments").fields.get(name="ReportingNote").max_length == 500
    rules = {r.field: r for r in draft.rule_set.rules.all()}
    assert len(draft.rule_set.rules.all()) == 2
    assert rules["PreDescription"].severity in ("reject", "warning")
    old = draft.rule_set.rules.get(replaces="Vtest01")
    assert old.severity == "reject" and old.field == "PreDescription"  # its test changed, under the field's new name
    assert draft.rule_set.rules.filter(severity="warning", field="PreDescription").exists()
    reasons = set(AuditEvent.objects.filter(action__startswith="specification.").exclude(
        action__in=["specification.draft_started", "specification.workbook_loaded"]).values_list("reason", flat=True))  # fmt: skip
    assert reasons <= {"The working group's sheet"} or all("The working group's sheet" in r for r in reasons)
    event = AuditEvent.objects.get(action="specification.workbook_loaded")
    assert event.after == {"sha256": workbook.sha256(content), "changes": len(changes)}
    assert workbook.read(draft, workbook.draft_xlsx(draft)) == []  # the draft as it now stands


def test_the_rows_order_is_the_templates(cpuc_admin, draft):
    t = draft.tables.get(name="Commitments")
    names = [f.name for f in t.fields.all()]

    def swap(wb):
        f = wb["Fields"]
        a, b = row_of(f, Table="Commitments", Field=names[0]), row_of(f, Table="Commitments", Field=names[1])
        for col in range(1, f.max_column + 1):
            f.cell(a, col).value, f.cell(b, col).value = f.cell(b, col).value, f.cell(a, col).value

    content = edited(workbook.draft_xlsx(draft), swap)
    [c] = workbook.read(draft, content)
    assert c.words == "Commitments: put the fields in the rows' order."
    workbook.load(cpuc_admin, draft, content, "Note first")
    assert [f.name for f in t.fields.all()][:2] == [names[1], names[0]]
    assert AuditEvent.objects.filter(action="specification.fields_reordered").count() == 1


def test_a_refused_change_is_shown_and_nothing_is_made(cpuc_admin, draft):
    lock = editor.locks(draft)
    locked = next(name for (table, name), why in lock.items() if table == "Measure" and why)

    def change(wb):
        f = wb["Fields"]
        put(f, row_of(f, Table="Measure", Field="PreDesc"), "Definition", "Changed")
        f.delete_rows(row_of(f, Table="Measure", Field=locked))

    content = edited(workbook.draft_xlsx(draft), change)
    changes, problems = workbook.preview(cpuc_admin, draft, content)
    assert len(changes) == 2 and len(problems) == 1 and f"{locked} is locked and cannot be removed" in problems[0]
    with pytest.raises(editor.EditError, match="is locked"):
        workbook.load(cpuc_admin, draft, content, "why")
    assert draft.tables.get(name="Measure").fields.get(name="PreDesc").definition != "Changed"  # all or none


def test_a_workbook_that_cannot_be_read_is_refused_with_where(cpuc_admin, draft):
    content = workbook.draft_xlsx(draft)
    with pytest.raises(workbook.LoadError, match="not an Excel workbook"):
        workbook.read(draft, b"not a workbook")
    bad_type = edited(content, lambda wb: put(wb["Fields"], row_of(wb["Fields"], Table="Measure", Field="PreDesc"), "Type", "Words"))
    with pytest.raises(workbook.LoadError, match=r"Fields, row \d+: 'Words' is not a field type"):
        workbook.read(draft, bad_type)
    no_rule = edited(content, lambda wb: put(wb["Rules"], row_of(wb["Rules"], Rule="Vtest01"), "Rule", "Vnope"))
    with pytest.raises(workbook.LoadError, match="no rule Vnope"):
        workbook.read(draft, no_rule)
    other = editor.start_draft(cpuc_admin, specifications.current(Specification.objects.get(code="claims")), "Claims 2029", "why")
    with pytest.raises(workbook.LoadError, match="not of the draft Claims 2029"):
        workbook.read(other, content)


def test_only_a_system_administrator_loads_a_workbook(draft, pge_admin):
    with pytest.raises(AccessDenied):
        workbook.preview(pge_admin, draft, workbook.draft_xlsx(draft))


def test_the_workbook_page(client, cpuc_admin, pge_admin, draft):
    sign_in(client, cpuc_admin)
    url = reverse("specifications:edit_workbook", args=["monthly", draft.slug])
    page = client.get(url).content.decode()
    assert 'id="workbook-download"' in page and 'id="workbook-form"' in page and 'id="workbook-preview"' not in page
    r = client.get(reverse("specifications:workbook_file", args=["monthly", draft.slug]))
    assert r.status_code == 200 and r["Content-Disposition"].endswith('_draft.xlsx"')
    content = edited(r.content, lambda wb: put(wb["Settings"], row_of(wb["Settings"], Setting="Summary"), "Value", "From the page"))
    page = client.post(url, {"file": SimpleUploadedFile("draft.xlsx", content)})
    assert page.status_code == 200
    text = page.content.decode()
    assert 'id="workbook-changes"' in text and "summary from" in text and 'id="workbook-confirm"' in text
    load = reverse("specifications:load_workbook", args=["monthly", draft.slug])
    editor.update_settings(cpuc_admin, draft, name=draft.name, summary="Changed meanwhile", engine_release="", reason="meanwhile")
    moved = client.post(load, {"sha256": workbook.sha256(content), "reason": "why"})  # the draft changed since: shown again
    assert moved.status_code == 302 and "held=1" in moved.url
    assert "summary from Changed meanwhile" in client.get(moved.url).content.decode()
    refused = client.post(load, {"sha256": workbook.sha256(content), "reason": ""})
    assert refused.status_code == 302 and "held=1" in refused.url
    assert 'id="workbook-confirm"' in client.get(refused.url).content.decode()  # the preview is shown again
    r = client.post(load, {"sha256": workbook.sha256(content), "reason": "From the working group"})
    assert r.status_code == 302
    draft.refresh_from_db()
    assert draft.summary == "From the page"
    assert client.post(load, {"sha256": workbook.sha256(content), "reason": "again"}).status_code == 302  # no longer held
    client.logout()
    sign_in(client, pge_admin)
    assert client.get(url).status_code == 403
