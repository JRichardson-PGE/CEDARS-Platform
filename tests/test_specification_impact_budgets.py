"""The impact test on a confirmed budget filing: its job's records are checked under the draft's rules and lists."""

import pytest

from cedars.modules.budgets import service as budgets
from cedars.services.specifications import editor, impact

from .test_budgets import filer, filing, priced_version, programs, release  # noqa: F401 - fixtures
from .test_specification_examples import current

pytestmark = pytest.mark.django_db


def test_a_confirmed_budget_filing_is_checked_under_the_draft(cpuc_admin, filer, filing, pge, programs, release):  # noqa: F811
    v, _run = priced_version(filer, filing, pge, release)
    v = budgets.confirm(filer, v)
    draft = editor.start_draft(cpuc_admin, current("budget_filing"), "Budget filings 2030", "why")
    editor.add_field(cpuc_admin, draft, "Measure", "ReportingNote", requirement="required", definition="A note", reason="why")
    [line] = impact.run(cpuc_admin, draft).report["filings"]
    assert line["submission_version"] == v.pk and not line["note"] and line["records"] == 6
    assert line["newly_rejected"] == 0 and line["absent"] == ["Measure.ReportingNote"]
