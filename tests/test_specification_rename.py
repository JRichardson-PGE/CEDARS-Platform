"""Renaming a field in a draft specification version: one field under a new name, not one removed and another added. The old name is
still accepted on upload, the draft's rules follow it, the comparison and the release note say it is renamed, and publishing renames
it in the data dictionary, which keeps the old name as a former name and the field's identity."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary import changes as dictionary_changes
from cedars.services.dictionary import service as dictionary
from cedars.services.dictionary.models import Field, FieldRename
from cedars.services.rules import service as rules
from cedars.services.rules.models import Rule, RuleSet
from cedars.services.specifications import editor
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification

from .conftest import sign_in

pytestmark = pytest.mark.django_db


def current(code: str):
    return specifications.current(Specification.objects.get(code=code))


def field(v, table: str, name: str):
    return v.tables.get(name=table).fields.get(name=name)


def soon(days: int):
    return timezone.localdate() + timedelta(days=days)


@pytest.fixture
def draft(cpuc_admin):
    return editor.start_draft(cpuc_admin, current("monthly"), "Monthly reports 2029", "The 2029 reporting changes")


def catalog(*names: str, label: str = "test catalog"):
    return dictionary.load_catalog(None, {"fields": [{"name": n, "kind": "input", "definition": f"{n} words"} for n in names]}, label=label)


# --- the rules


def test_a_rules_field_definition_and_conditions_follow_the_rename():
    d = {"field": "Comments", "rule": "equals", "value": "Comments", "list": "Comments", "columns": ["PrgID", "Comments"],
         "conds": [["Commitments", "Comments", "equals", "x"], ["Measure", "Comments", "equals", "Comments"]]}  # fmt: skip
    field_, out = rules.renamed_test("Commitments", "PrgID + Comments", d, "Commitments", "Comments", "Notes")
    assert field_ == "PrgID + Notes"
    assert out["field"] == "Notes" and out["value"] == "Notes" and out["columns"] == ["PrgID", "Notes"]
    assert out["list"] == "Comments"  # a value list may share the field's name: it is not the field
    assert out["conds"] == [["Commitments", "Notes", "equals", "x"], ["Measure", "Comments", "equals", "Comments"]]
    other, out = rules.renamed_test("Measure", "Comments", d, "Commitments", "Comments", "Notes")
    assert other == "Comments" and out["field"] == "Comments"  # another table's field of that name is another field
    assert out["conds"][0] == ["Commitments", "Notes", "equals", "x"]  # but its condition on the renamed table follows
    assert d["field"] == "Comments"  # the definition given is not changed in place


# --- the draft


def test_a_rename_keeps_the_field_and_accepts_the_old_name(cpuc_admin, draft):
    before = field(draft, "Measure", "PreDesc")
    f = editor.rename_field(cpuc_admin, before, "PreDescription", "CPUC calls them notes")
    assert f.pk == before.pk and f.position == before.position and f.renamed_from == "PreDesc"
    assert "PreDesc" in f.other_names and f.definition == before.definition
    e = AuditEvent.objects.get(action="specification.field_renamed")
    assert e.before["name"] == "PreDesc" and e.after["name"] == "PreDescription" and e.reason == "CPUC calls them notes"
    f = editor.rename_field(cpuc_admin, f, "BaseDescription", "Clearer still")
    assert f.renamed_from == "PreDesc"  # the name it had in the published version
    f = editor.rename_field(cpuc_admin, f, "PreDesc", "Back as it was")
    assert f.renamed_from == "" and f.name == "PreDesc" and "BaseDescription" not in f.other_names and "PreDesc" not in f.other_names


def test_a_locked_field_keeps_its_name_and_a_name_is_used_once(cpuc_admin, draft):
    with pytest.raises(editor.EditError, match="locked"):
        editor.rename_field(cpuc_admin, field(draft, "Measure", "PrgID"), "ProgramID", "why")
    with pytest.raises(editor.EditError, match="already called or accepted"):
        editor.rename_field(cpuc_admin, field(draft, "Measure", "PreDesc"), "StdDesc", "why")
    with pytest.raises(editor.EditError, match="letters, digits"):
        editor.rename_field(cpuc_admin, field(draft, "Measure", "PreDesc"), "2 notes", "why")
    with pytest.raises(editor.EditError, match="Say why"):
        editor.rename_field(cpuc_admin, field(draft, "Measure", "PreDesc"), "PreDescription", "")
    catalog("PreDesc", "PreDescription")
    with pytest.raises(editor.EditError, match="data dictionary already has a field called PreDescription"):
        editor.rename_field(cpuc_admin, field(draft, "Measure", "PreDesc"), "PreDescription", "why")


def test_the_drafts_rules_follow_and_the_comparison_shows_one_renamed_field(cpuc_admin):
    published = RuleSet.objects.create(code="monthly_test", label="Monthly test", effective_from=soon(-30))
    Rule.objects.create(rule_set=published, rule_id="Vtest01", severity="warning", table="Measure", field="PreDesc", kind="rule",
                        definition={"field": "PreDesc", "rule": "equals", "value": "none", "connector": "all",
                                    "conds": [["Measure", "PreDesc", "equals", "x"]]},
                        message="When PreDesc is x, PreDesc should equal none", fix="Fill in PreDesc.", modules=["monthly"])  # fmt: skip
    RuleSet.objects.filter(pk=published.pk).update(status="published")
    doc = specifications.stored_starting("monthly")
    doc["version"]["name"] = "Monthly with a rule"
    base = specifications.load(doc, rule_set=RuleSet.objects.get(pk=published.pk))
    draft = editor.start_draft(cpuc_admin, base, "Monthly reports 2029", "why")
    rs = draft.rule_set
    editor.rename_field(cpuc_admin, field(draft, "Measure", "PreDesc"), "PreDescription", "CPUC calls them notes")
    r = rs.rules.get()
    assert (
        r.field == "PreDescription"
        and r.definition["field"] == "PreDescription"
        and r.message == "When PreDescription is x, PreDescription should equal none"
    )
    assert r.definition["conds"] == [["Measure", "PreDescription", "equals", "x"]]
    assert r.replaces == "Vtest01" and r.rule_id != "Vtest01"
    c = specifications.compare(draft.based_on, draft)
    assert [(t, old, new) for t, old, new, _d in c.fields_renamed] == [("Measure", "PreDesc", "PreDescription")]
    assert not c.fields_added and not c.fields_removed
    lines = editor.change_lines(draft)
    assert "Measure.PreDesc is renamed PreDescription: a file with the old name still uploads, and data filed earlier keeps it." in lines
    assert not any("New field" in line or "is removed" in line for line in lines)
    assert [(o.rule_id, n.rule_id) for o, n in c.rules_renamed] == [("Vtest01", r.rule_id)] and not c.rules_changed
    assert "1 rule now names the renamed field; what it checks is unchanged." in lines


def test_publishing_renames_the_field_in_the_dictionary(cpuc_admin, draft):
    base = catalog("PreDesc", "PrgID")
    identity = base.fields.get(name="PreDesc").identity
    editor.rename_field(cpuc_admin, field(draft, "Measure", "PreDesc"), "PreDescription", "CPUC calls them notes")
    v = editor.publish(cpuc_admin, draft, effective_on=soon(20))
    assert field(v, "Measure", "PreDescription").renamed_from == "PreDesc"
    now = dictionary.current()
    assert now != base
    f = now.fields.get(name="PreDescription")
    assert f.identity == identity and ("PreDesc", dictionary.FORMER) in {(a.name, a.system) for a in f.aliases.all()}
    assert dictionary.resolve("PreDesc") == f  # the old name still finds it, on upload and by search
    rename = FieldRename.objects.get()
    assert rename.old_name == "PreDesc" and rename.new_name == "PreDescription" and "Monthly reports 2029" in rename.specification
    ch = dictionary_changes.changes(now)
    assert [(c.name, c.what) for c in ch.fields] == [("PreDescription", "renamed")] and ch.counts["renamed"] == 1
    assert [c.what for _v, c in dictionary_changes.history("PreDesc")] == ["renamed", "added"]
    # a later catalog that still calls it PreDesc takes the new name and keeps the identity
    later = catalog("PreDesc", "PrgID", label="a later catalog")
    assert set(later.fields.values_list("name", flat=True)) == {"PreDescription", "PrgID"}
    assert later.fields.get(name="PreDescription").identity == identity
    assert AuditEvent.objects.filter(action="dictionary.field_renamed").exists()


def test_another_specifications_draft_lists_the_rename_to_accept(cpuc_admin):
    claims = editor.start_draft(cpuc_admin, current("claims"), "Claims 2029", "why")
    locks = editor.locks(claims)
    t, f = next((t, f) for t in claims.tables.all() for f in t.fields.all() if (t.name, f.name) not in locks)
    FieldRename.objects.create(identity=Field._meta.get_field("identity").default(), old_name=f.name, new_name=f"{f.name}New",
                               catalog_version=catalog("X"), specification="Monthly reports: Monthly reports 2029")  # fmt: skip
    pending = editor.pending_renames(claims)
    assert [(p.table, p.field, p.new_name) for p in pending] == [(t.name, f.name, f"{f.name}New")]
    editor.rename_field(cpuc_admin, f, f"{f.name}New", "Renamed in the data dictionary")
    assert editor.pending_renames(claims) == []


def test_the_rename_pages(client, cpuc_admin, draft):
    sign_in(client, cpuc_admin)
    page = client.get(reverse("specifications:edit_field", args=["monthly", draft.slug, "Measure", "PreDesc"])).content.decode()
    assert 'id="rename-field"' in page and 'name="new_name"' in page
    locked = client.get(reverse("specifications:edit_field", args=["monthly", draft.slug, "Measure", "PrgID"])).content.decode()
    assert "A locked field keeps its name." in locked
    url = reverse("specifications:rename_field", args=["monthly", draft.slug, "Measure", "PreDesc"])
    r = client.post(url, {"new_name": "PreDescription", "reason": ""})
    assert r.status_code == 302 and field(draft, "Measure", "PreDesc")
    r = client.post(url, {"new_name": "PreDescription", "reason": "CPUC calls them notes"})
    assert r.status_code == 302 and field(draft, "Measure", "PreDescription").renamed_from == "PreDesc"
    fields = client.get(reverse("specifications:edit", args=["monthly", draft.slug])).content.decode()
    assert "renamed from PreDesc" in fields
    changes = client.get(reverse("specifications:edit_changes", args=["monthly", draft.slug])).content.decode()
    assert "Renamed from PreDesc, still accepted on upload" in changes


def test_the_old_names_dictionary_page_redirects_to_the_field(client, cpuc_admin, draft):
    catalog("PreDesc")
    editor.rename_field(cpuc_admin, field(draft, "Measure", "PreDesc"), "PreDescription", "CPUC calls them notes")
    editor.publish(cpuc_admin, draft, effective_on=soon(20))
    r = client.get(reverse("dictionary:field", args=["PreDesc"]))
    assert r.status_code == 302 and r.url == reverse("dictionary:field", args=["PreDescription"])
    page = client.get(r.url).content.decode()
    assert "PreDesc (Former name)" in page
