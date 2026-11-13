"""The specification editor (cedars.services.specifications.editor and its pages): a CPUC system administrator starts a draft from a
published version, changes its settings, fields and rules, and publishes it with notice and a release note; locked fields stay; a
published version never changes and new filing sets take it only from the day it takes effect."""

import ast
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from cedars.modules.claims import service as claims
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.releases.models import ReleaseNote
from cedars.services.specifications import editor
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification, SpecificationVersion

from .test_claims import year  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
ROOT = Path(__file__).resolve().parent.parent


def current(code: str) -> SpecificationVersion:
    return specifications.current(Specification.objects.get(code=code))


def field(v, table: str, name: str):
    return v.tables.get(name=table).fields.get(name=name)


@pytest.fixture
def draft(cpuc_admin):
    return editor.start_draft(cpuc_admin, current("monthly"), "Monthly reports 2029", "The 2029 reporting changes")


def soon(days: int):
    return timezone.localdate() + timedelta(days=days)


# --- drafts


def test_a_draft_is_a_copy_of_a_published_version(cpuc_admin, draft):
    base = current("monthly")
    assert draft.status == "draft" and draft.based_on == base and draft.slug == "monthly-reports-2029"
    assert [t.name for t in draft.tables.all()] == [t.name for t in base.tables.all()]
    assert draft.tables.get(name="Commitments").fields.count() == base.tables.get(name="Commitments").fields.count()
    assert [c.rule_id for c in draft.checks.all()] == [c.rule_id for c in base.checks.all()]
    assert specifications.compare(base, draft).empty
    assert AuditEvent.objects.filter(action="specification.draft_started", reason="The 2029 reporting changes").exists()


def test_only_a_system_administrator_starts_a_draft_and_one_at_a_time(cpuc_admin, pge_admin, draft):
    with pytest.raises(AccessDenied):
        editor.start_draft(pge_admin, current("claims"), "Claims 2029", "why")
    with pytest.raises(editor.EditError, match="already has a draft"):
        editor.start_draft(cpuc_admin, current("monthly"), "Another", "why")
    with pytest.raises(editor.EditError, match="copied from a published version"):
        editor.start_draft(cpuc_admin, draft, "Copy of a draft", "why")
    with pytest.raises(editor.EditError, match="already has a version called"):
        editor.start_draft(cpuc_admin, current("claims"), "Starting version", "why")
    with pytest.raises(editor.EditError, match="Say why"):
        editor.start_draft(cpuc_admin, current("claims"), "Claims 2029", "")


def test_the_draft_rule_set_is_a_copy_in_the_library_published_only_with_the_version(cpuc_admin):
    rules = pytest.importorskip("cedars.services.rules.service")
    pytest.importorskip("recet.filing_rules")
    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "budget_filing"))
    doc = specifications.stored_starting("budget_filing")
    doc["version"]["name"] = "Budget filings 2029"
    base = specifications.load(doc, rule_set=rs)
    v = editor.start_draft(cpuc_admin, base, "Budget filings 2030", "New rules")
    assert v.rule_set.status == "draft" and v.rule_set.based_on == rs and v.rule_set.rules.count() == rs.rules.count()
    with pytest.raises(AccessDenied, match="published with it"):
        rules.publish(cpuc_admin, v.rule_set)
    # discarding the draft discards its rules too
    draft_rules = v.rule_set.pk
    editor.discard(cpuc_admin, v, "Not needed")
    assert not SpecificationVersion.objects.filter(pk=v.pk).exists()
    assert not rules.RuleSet.objects.filter(pk=draft_rules).exists()


# --- settings


def test_settings_rename_the_draft_and_check_the_engine_release(cpuc_admin, draft):
    from cedars.modules.calculations import service as calculations

    v = editor.update_settings(cpuc_admin, draft, name="Monthly reports, 2029 edition", summary="For 2029", engine_release="")
    assert v.slug == "monthly-reports-2029-edition" and v.summary == "For 2029"
    with pytest.raises(editor.EditError, match="not a registered engine release"):
        editor.update_settings(cpuc_admin, v, name=v.name, summary=v.summary, engine_release="9.9.9")
    calculations.add_release(cpuc_admin, release="0.1.80", calculations=["recet"], accs={"recet": ["2026"]}, default=True)
    v = editor.update_settings(cpuc_admin, v, name=v.name, summary=v.summary, engine_release="0.1.80", reason="Priced on 0.1.80")
    assert v.engine_release == "0.1.80"
    assert AuditEvent.objects.filter(action="specification.settings_changed", reason="Priced on 0.1.80").exists()


# --- fields


def test_add_change_move_and_remove_a_field(cpuc_admin, draft):
    f = editor.add_field(
        cpuc_admin, draft, "Commitments", "ReportingNote", type="text", max_length=500, definition="A note on the month",
        requirement="optional", position=0, reason="Asked for by the working group",
    )  # fmt: skip
    assert f.position == 0 and [x.name for x in draft.tables.get(name="Commitments").fields.all()][0] == "ReportingNote"
    with pytest.raises(editor.EditError, match="already has"):
        editor.add_field(cpuc_admin, draft, "Commitments", "reportingnote", type="text", definition="x")
    with pytest.raises(editor.EditError, match="definition"):
        editor.add_field(cpuc_admin, draft, "Commitments", "Another", type="text")
    with pytest.raises(editor.EditError, match="not a whole number"):
        editor.add_field(cpuc_admin, draft, "Commitments", "Count", type="integer", definition="A count", example="1.5")
    f = editor.change_field(cpuc_admin, f, requirement="required", example="See the memo", other_names=["Note"], position=3, reason="r")
    assert f.requirement == "required" and f.other_names == ["Note"] and f.position == 3
    with pytest.raises(editor.EditError, match="no value list called"):
        editor.change_field(cpuc_admin, f, value_list="no_such_list")
    with pytest.raises(editor.EditError, match="already accepts"):
        editor.change_field(cpuc_admin, field(draft, "Commitments", "YearMonth"), other_names=["Note"])
    with pytest.raises(editor.EditError, match="Say why"):
        editor.remove_field(cpuc_admin, f, "")
    editor.remove_field(cpuc_admin, f, "Not needed after all")
    positions = list(draft.tables.get(name="Commitments").fields.values_list("position", flat=True))
    assert positions == list(range(len(positions)))  # renumbered, no gaps


def test_a_dictionary_field_brings_its_definition_and_personal_data_stays_in_the_vault(cpuc_admin):
    import json

    from cedars.services.dictionary import service as dictionary

    from .test_search import CATALOG

    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    v = editor.start_draft(cpuc_admin, current("claims"), "Claims 2029", "New fields")
    taken = {f.name for f in v.tables.get(name="Claim").fields.all()}
    known = next(f for f, _ in dictionary.table_fields("calculations", "Measure") if f.name not in taken and f.definition)
    f = editor.add_field(cpuc_admin, v, "Claim", known.name, requirement="optional")
    assert f.definition == known.definition
    pii = next((f for f in dictionary.current().fields.filter(audience="confidential")), None)
    if pii is not None:
        with pytest.raises(editor.EditError, match="PII vault"):
            editor.add_field(cpuc_admin, v, "Claim", pii.name, requirement="optional")


def test_locked_fields_keep_their_type_stay_required_and_cannot_be_removed(cpuc_admin):
    v = editor.start_draft(cpuc_admin, current("claims"), "Claims 2029", "Locks")
    locks = editor.locks(v)
    assert "key" in locks[("Claim", "ClaimID")] and ("Measure", "PrgID") in locks and ("PII_Site", "SiteID") in locks
    claim_id = field(v, "Claim", "ClaimID")
    with pytest.raises(editor.EditError, match="cannot be removed"):
        editor.remove_field(cpuc_admin, claim_id, "no")
    with pytest.raises(editor.EditError, match="stays required"):
        editor.change_field(cpuc_admin, claim_id, requirement="optional")
    with pytest.raises(editor.EditError, match="its type stays"):
        editor.change_field(cpuc_admin, claim_id, type="integer")
    # its wording may change
    assert editor.change_field(cpuc_admin, claim_id, guidance="Start it with the PA code.", reason="Clearer").guidance


def test_the_engine_locks_the_fields_it_prices_with():
    pytest.importorskip("recet.editor")
    v = current("budget_filing")
    locks = editor.locks(v)
    assert editor.ENGINE_WHY in locks[("Measure", "UnitkWh1stBaseline")]
    assert editor.ENGINE_WHY in locks[("ProgramCost", "AdminCostsOther")]


MODULES = {
    "claims": ["cedars/modules/claims", "cedars/services/vault"],
    "budgets": ["cedars/modules/budgets"],
    "monthly": ["cedars/modules/monthly"],
}


@pytest.mark.parametrize("module", sorted(MODULES))
def test_every_field_a_modules_code_names_is_locked(module):
    """A field name the module's code names (a string literal that is a field of its specification) is in its locked list, so a draft
    cannot take away a field the code reads. The lists of checks only describe fields, so they are left out."""
    v = specifications.for_module(module)
    names = {f.name for t in v.tables.prefetch_related("fields") for f in t.fields.all()}
    declared = {f for fs in editor.declared_locks(module).values() for f in fs}
    named = set()
    for folder in MODULES[module]:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if "migrations" in path.parts or path.name in ("coded_checks.py", "locked_fields.py"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[A-Za-z_]\w*", node.value):
                    named.add(node.value)
    missing = sorted((named & names) - declared)
    assert not missing, f"Add these to the {module} module's locked_fields.LOCKED, with why its code reads them: {missing}"


# --- publishing


def test_a_draft_that_changes_nothing_or_lacks_a_summary_cannot_be_published(cpuc_admin, draft):
    texts = [p.text for p in editor.problems(draft)]
    assert any("Nothing changes" in t for t in texts)
    draft = editor.update_settings(cpuc_admin, draft, name=draft.name, summary="", engine_release="")
    assert any("summary" in p.text for p in editor.problems(draft))
    with pytest.raises(editor.EditError, match="cannot be published yet"):
        editor.publish(cpuc_admin, draft, effective_on=soon(30))


def _ready(cpuc_admin, draft):
    editor.change_field(cpuc_admin, field(draft, "Commitments", "Comments"), guidance="Say what changed this month.", reason="Clearer")
    return draft


def test_publish_needs_the_usual_notice_or_a_reason(cpuc_admin, draft):
    _ready(cpuc_admin, draft)
    assert editor.problems(draft) == []
    with pytest.raises(editor.EditError, match="after the day it is published"):
        editor.publish(cpuc_admin, draft, effective_on=timezone.localdate())
    with pytest.raises(editor.EditError, match="less than the usual"):
        editor.publish(cpuc_admin, draft, effective_on=soon(3))
    v = editor.publish(cpuc_admin, draft, effective_on=soon(3), short_notice_reason="A decision takes effect next week")
    assert v.status == "published" and v.short_notice_reason == "A decision takes effect next week"


def test_publishing_freezes_the_version_announces_it_and_takes_effect_on_its_day(cpuc_admin, draft):
    base = current("monthly")
    _ready(cpuc_admin, draft)
    v = editor.publish(cpuc_admin, draft, effective_on=soon(20), changes="Comments has guidance.")
    assert v.status == "published" and v.effective_on == soon(20) and v.fingerprint
    note = ReleaseNote.objects.get(pk=v.release_note_id)
    assert note.published_at and note.effective_on == soon(20) and note.changes.get().text == "Comments has guidance."
    # announced, not yet in effect: new filing sets keep the version in effect until its day
    assert current("monthly") == base
    assert specifications.current(v.specification, on=soon(20)) == v
    assert specifications.coming(v.specification) == [v]
    with pytest.raises(editor.EditError, match="never changes"):
        editor.change_field(cpuc_admin, field(v, "Commitments", "Comments"), guidance="again")
    assert AuditEvent.objects.filter(action="specification.published").exists()


def test_new_filing_sets_take_a_version_only_from_its_day(cpuc_admin, year):  # noqa: F811
    v = editor.start_draft(cpuc_admin, current("claims"), "Claims 2029", "2029")
    editor.change_field(cpuc_admin, field(v, "Claim", "ClaimID"), guidance="Start it with the PA code.", reason="Clearer")
    v = editor.publish(cpuc_admin, v, effective_on=soon(15))
    due = {kind: timezone.now() + timedelta(days=30 * (i + 1)) for i, (kind, _) in enumerate(claims.PERIODS)}
    claims.add_year(cpuc_admin, year=2029, acc_version="2026", due=due)
    assert {d.specification_version.name for d in claims.periods(2029)} == {"Starting version"}


# --- pages


def test_the_version_page_offers_a_new_version_to_system_administrators_only(client, signed_in, cpuc_admin, pge_admin):
    url = f"/specifications/monthly/{current('monthly').slug}/"
    assert 'id="change-h"' not in client.get(url).content.decode()
    assert 'id="change-h"' not in signed_in(pge_admin).get(url).content.decode()
    c = signed_in(cpuc_admin)
    assert "Start a new version from this one" in c.get(url).content.decode()
    r = c.post(f"/specifications/monthly/{current('monthly').slug}/new-draft", {"name": "Monthly reports 2029", "reason": "2029"})
    assert r.status_code == 302 and r["Location"] == "/specifications/monthly/monthly-reports-2029/edit/"


def test_the_draft_pages_for_a_system_administrator(signed_in, cpuc_admin, pge_admin, draft):
    base = f"/specifications/monthly/{draft.slug}/edit/"
    for path in ("", "settings/", "rules/", "checks/", "changes/", "publish/", "tables/Commitments/", "tables/Commitments/add/",
                 "tables/Commitments/fields/Comments/"):  # fmt: skip
        assert signed_in(pge_admin).get(base + path).status_code == 403, path
        assert signed_in(cpuc_admin).get(base + path).status_code == 200, path
    c = signed_in(cpuc_admin)
    page = c.get(base).content.decode()
    assert 'id="field-Commitments-PrgID"' in page and "Locked" in page
    r = c.post(base + "tables/Commitments/fields/Comments/", {
        "requirement": "optional", "type": "text", "max_length": "300", "guidance": "Say what changed.", "position": "0",
        "reason": "Clearer",
    })  # fmt: skip
    assert r.status_code == 302
    f = field(draft, "Commitments", "Comments")
    assert f.max_length == 300 and f.guidance == "Say what changed." and f.position == 0
    r = c.post(base + "tables/Commitments/add/", {
        "new_name": "ReportingNote", "requirement": "optional", "type": "text", "definition": "A note", "position": "1", "reason": "New",
    })  # fmt: skip
    assert r.status_code == 302 and field(draft, "Commitments", "ReportingNote").position == 1
    assert "New field Commitments.ReportingNote" in c.get(base + "changes/").content.decode()
    r = c.post(base + "publish/", {"effective_on": soon(20).isoformat(), "changes": "Two changes to Commitments.", "action": ""})
    assert r.status_code == 302 and r["Location"] == f"/specifications/monthly/{draft.slug}/"
    draft.refresh_from_db()
    assert draft.status == "published"
    assert c.get(base).status_code == 302  # a published version is not edited


def test_a_locked_field_page_says_why(signed_in, cpuc_admin, draft):
    page = signed_in(cpuc_admin).get(f"/specifications/monthly/{draft.slug}/edit/tables/Commitments/fields/PrgID/").content.decode()
    assert 'id="field-lock"' in page and "A locked field cannot be removed" in page


def test_discarding_a_draft_on_its_page(signed_in, cpuc_admin, draft):
    r = signed_in(cpuc_admin).post(f"/specifications/monthly/{draft.slug}/edit/discard", {"reason": "Started by mistake"})
    assert r.status_code == 302 and not SpecificationVersion.objects.filter(pk=draft.pk).exists()


def test_the_rule_pages_send_rule_changes_to_the_specification(signed_in, cpuc_admin):
    rules = pytest.importorskip("cedars.services.rules.service")
    pytest.importorskip("recet.filing_rules")
    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "budget_filing"))
    page = signed_in(cpuc_admin).get(f"/rules/{rs.pk}/").content.decode()
    assert "/specifications/budget_filing/current/" in page
