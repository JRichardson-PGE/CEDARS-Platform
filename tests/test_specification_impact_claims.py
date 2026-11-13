"""The impact test on confirmed claims: the year so far and the claims rule set are read as the filing's own check reads them, so an
unchanged draft finds nothing new, and a new required field is reported where the filing lacks it."""

import pytest

from cedars.services.specifications import editor, impact

from .test_budgets import release  # noqa: F401 - fixture
from .test_claims import confirmed, filer, programs, q1, year  # noqa: F401 - fixtures
from .test_specification_examples import current

pytestmark = pytest.mark.django_db


def test_a_confirmed_claim_is_checked_under_the_draft(cpuc_admin, filer, year, pge, programs, release):  # noqa: F811
    v = confirmed(filer, pge, q1())
    draft = editor.start_draft(cpuc_admin, current("claims"), "Claims 2029", "why")
    editor.add_field(cpuc_admin, draft, "Claim", "ReportingNote", requirement="required", definition="A note", reason="why")
    [line] = impact.run(cpuc_admin, draft).report["filings"]
    assert line["submission_version"] == v.pk and not line["note"] and line["records"] > 0
    assert (line["newly_rejected"], line["newly_warned"], line["cleared"]) == (0, 0, 0)  # nothing else changed
    assert line["absent"] == ["Claim.ReportingNote"]
