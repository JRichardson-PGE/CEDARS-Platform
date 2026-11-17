"""Built-in checks follow the filing's specification version: a check written in code that a version may leave out runs only when the
version lists it, with the severity the version gives it; CEDARS' own processing always runs. The draft's Built-in checks page sets
them (cedars.services.specifications.editor.set_checks)."""

from dataclasses import dataclass

import pytest
from django.urls import reverse

from cedars.services.audit.models import AuditEvent
from cedars.services.rules import coded
from cedars.services.specifications import editor
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification
from cedars.services.submissions.models import Deadline, SubmissionVersion

from .conftest import sign_in
from .test_monthly import cost, filer, month, programs, report, year  # noqa: F401 - fixtures
from .test_monthly import service as monthly

pytestmark = pytest.mark.django_db


def current(code: str):
    return specifications.current(Specification.objects.get(code=code))


def version_with(code: str, name: str, *, leave_out=(), severities=None):
    """A published version of `code` like its starting version, without the checks `leave_out` and with `severities` changed."""
    doc = specifications.stored_starting(code)
    doc["version"]["name"] = name
    doc["checks"] = [
        {**c, "severity": (severities or {}).get(c["rule_id"], c["severity"])} for c in doc["checks"] if c["rule_id"] not in leave_out
    ]
    return specifications.load(doc)


@dataclass
class Finding:
    rule_id: str
    severity: str


# --- the lists


def test_only_the_data_rules_and_cost_warnings_are_optional():
    known = coded.by_id()
    optional = {i for i, c in known.items() if c.optional}
    assert optional == {"CLM-SITE", "CLM-NMEC", "CLM-SEM", "CLM-DEEMED", "CLM-FUELSUB", "CLM-PAYMENT", "CLM-CCAREN", "BUD-NOCOST",
                        "MON-NOCOST", "MON-YTD"}  # fmt: skip
    assert all(known[i].severities == ("reject", "warning") for i in optional)
    assert not any(c.severities for i, c in known.items() if i not in optional)  # CEDARS' own processing keeps its severity


def test_a_choosable_severity_is_reject_or_warning_its_own_included():
    c = coded.CodedCheck("X-1", "reject", "", "", "", "", optional=True, severities=("warning",))
    with pytest.raises(ValueError, match="choosable severity"):
        coded.register_coded_checks("test-x", "Test", "", [c])
    with pytest.raises(ValueError, match="choosable severity"):
        coded.register_coded_checks("test-x", "Test", "", [coded.CodedCheck("X-1", "reject or warning", "", "", "", "", True,
                                                                            ("reject", "warning"))])  # fmt: skip


# --- applying a version's checks


def test_a_version_drops_the_optional_checks_it_does_not_list_and_sets_their_severity():
    v = version_with("monthly", "Monthly test", leave_out=["MON-NOCOST", "MON-PRG"], severities={"MON-YTD": "reject", "MON-DUP": "warning"})
    found = [Finding("MON-NOCOST", "warning"), Finding("MON-YTD", "warning"), {"rule_id": "MON-YTD", "severity": "warning"},
             Finding("MON-PRG", "reject"), Finding("MON-DUP", "reject"), Finding("V1a2b3c", "warning")]  # fmt: skip
    out = specifications.apply_checks(v, found)
    assert [(f["rule_id"], f["severity"]) if isinstance(f, dict) else (f.rule_id, f.severity) for f in out] == [
        ("MON-YTD", "reject"),
        ("MON-YTD", "reject"),
        ("MON-PRG", "reject"),  # CEDARS' own processing runs although the version leaves it out
        ("MON-DUP", "reject"),  # and keeps its own severity
        ("V1a2b3c", "warning"),  # a rule of the rule set is not a built-in check
    ]
    assert found[1].severity == "warning"  # the findings given are not changed in place


def test_no_version_or_a_version_with_no_checks_keeps_every_finding():
    found = [Finding("MON-NOCOST", "warning")]
    assert specifications.apply_checks(None, found) == found
    doc = specifications.stored_starting("monthly")
    doc["version"]["name"], doc["checks"] = "Monthly with no checks", []
    v = specifications.load(doc)
    assert specifications.apply_checks(v, found) == found


def test_a_monthly_report_runs_the_checks_its_version_lists(filer, year, pge, programs):  # noqa: F811
    v = version_with("monthly", "Monthly without the cost warning", leave_out=["MON-NOCOST"], severities={"MON-YTD": "reject"})
    Deadline.objects.filter(pk=month(2).pk).update(specification_version=v)
    measures = ["M1|PGE_Res_HVAC|2028Q1|1|0|0|0|0", "M2|PGE_Com_Ltg|2028Q1|1|0|0|0|0"]
    data = report("202802", costs=[cost(year="2028")], measures=measures)  # PGE_Com_Ltg has measures but no ProgramCost row
    s = monthly.upload(filer, month(2), pge, data, "february.zip")
    assert "MON-NOCOST" not in {f["rule_id"] for f in monthly.findings(s)}
    data = report("202803", costs=[cost(year="2028")], measures=measures)
    s = monthly.upload(filer, month(3), pge, data, "march.zip")  # on the starting version
    assert ("MON-NOCOST", "warning") in {(f["rule_id"], f["severity"]) for f in monthly.findings(s)}
    assert s.status == SubmissionVersion.Status.VALIDATED  # warnings only


# --- the draft


@pytest.fixture
def draft(cpuc_admin):
    return editor.start_draft(cpuc_admin, current("monthly"), "Monthly reports 2029", "The 2029 reporting changes")


def test_a_draft_sets_which_optional_checks_run_and_their_severity(cpuc_admin, draft):
    v = editor.set_checks(cpuc_admin, draft, runs=["MON-NOCOST"], severities={"MON-NOCOST": "reject"}, reason="Costs are required")
    listed = {c.rule_id: c.severity for c in v.checks.all()}
    assert "MON-YTD" not in listed and listed["MON-NOCOST"] == "reject" and listed["MON-PRG"] == "reject"
    e = AuditEvent.objects.get(action="specification.checks_changed")
    assert e.before == {"MON-NOCOST": "warning", "MON-YTD": "warning"} and e.after == {"MON-NOCOST": "reject", "MON-YTD": ""}
    lines = editor.change_lines(v)
    assert "Built-in check MON-YTD no longer runs." in lines and "Built-in check MON-NOCOST now rejects the record." in lines
    v = editor.set_checks(cpuc_admin, v, runs=["MON-NOCOST", "MON-YTD"], severities={}, reason="Put it back")
    assert "MON-YTD" in {c.rule_id for c in v.checks.all()}
    rows = [r.check.rule_id for _g, _l, rs in editor.check_rows(v) for r in rs]
    assert [c.rule_id for c in v.checks.all()] == rows  # in the code's order


def test_cedars_own_checks_always_run_as_written(cpuc_admin, draft):
    with pytest.raises(editor.EditError, match="CEDARS' own"):
        editor.set_checks(cpuc_admin, draft, runs=[], severities={"MON-PRG": "warning"}, reason="why")
    v = editor.set_checks(cpuc_admin, draft, runs=[], severities={}, reason="Only CEDARS' own")
    assert {c.rule_id for c in v.checks.all()} == {c.rule_id for c in coded.group("monthly").checks if not c.optional}
    with pytest.raises(editor.EditError, match="Say why"):
        editor.set_checks(cpuc_admin, v, runs=["MON-YTD"], severities={}, reason="")
    with pytest.raises(editor.EditError):
        editor.set_checks(cpuc_admin, current("monthly"), runs=[], severities={}, reason="A published version never changes")


def test_a_new_draft_lists_the_checks_the_code_has_now(cpuc_admin):
    base = version_with("monthly", "Monthly before MON-PRG", leave_out=["MON-PRG", "MON-YTD"])
    v = editor.start_draft(cpuc_admin, base, "Monthly after", "why")
    listed = {c.rule_id for c in v.checks.all()}
    assert "MON-PRG" in listed  # added to the code since: it runs anyway, and the new version says so
    assert "MON-YTD" not in listed  # optional: still left out until the CPUC puts it back


def test_the_checks_page_changes_the_draft(client, cpuc_admin, draft):
    sign_in(client, cpuc_admin)
    url = reverse("specifications:edit_checks", args=["monthly", draft.slug])
    page = client.get(url).content.decode()
    assert 'name="run" value="MON-YTD"' in page and 'id="severity-MON-NOCOST"' in page and 'name="run" value="MON-PRG"' not in page
    r = client.post(url, {"run": ["MON-NOCOST"], "severity-MON-NOCOST": "reject", "reason": ""})
    assert r.status_code == 200 and "MON-YTD" in {c.rule_id for c in draft.checks.all()}  # nothing saved without a reason
    assert 'value="MON-YTD" id="run-MON-YTD">' in r.content.decode()  # the post is shown again: MON-YTD unticked
    r = client.post(url, {"run": ["MON-NOCOST"], "severity-MON-NOCOST": "reject", "reason": "Costs are required"})
    assert r.status_code == 302
    assert {c.rule_id: c.severity for c in draft.checks.all()}.get("MON-NOCOST") == "reject"
    assert "MON-YTD" not in {c.rule_id for c in draft.checks.all()}
    built_in = client.get(reverse("rules:built_in")).content.decode()
    assert "A version may leave it out or make it a reject" in built_in
