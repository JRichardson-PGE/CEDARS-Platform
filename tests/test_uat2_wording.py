"""UAT 2 (2026-11-01) wording: findings counted by record (U08), run settings in words (U10), the sign-in library's pages and messages in
the site's voice (U11), import_rules' summary (U14)."""

import io
import shutil

import pytest
from django.core.management import call_command
from django.template.loader import get_template, render_to_string

from cedars.modules.calculations.views import settings_view
from cedars.services.rules import service as rules

pytestmark = pytest.mark.django_db


def _f(row, rule, severity="reject", table="Measure", field="NTG", record=""):
    return rules.Finding(row, record or f"M{row}", table, field, rule, severity, f"{rule} at {row}", "fix it")


def test_findings_are_counted_by_record_and_the_unit_agrees():
    found = [
        _f(2, "CLM-DEEMED"),
        _f(2, "CLM-DEEMED", field="EUL"),  # the same record twice: one record
        _f(3, "CLM-DEEMED"),
        _f(4, "W1", "warning"),
        _f(1, "CLM-COL", table="Measure", field="NTG", record=""),
        _f(1, "CLM-COL", table="Measure", field="EUL", record=""),
    ]
    out = {r["rule_id"]: r for r in rules.by_rule(found)}
    assert (out["CLM-DEEMED"]["count"], out["CLM-DEEMED"]["unit"]) == (2, "records")
    assert (out["W1"]["count"], out["W1"]["unit"]) == (1, "record")
    assert (out["CLM-COL"]["count"], out["CLM-COL"]["unit"]) == (2, "columns")
    assert [r["rule_id"] for r in rules.by_rule(found)] == ["CLM-COL", "CLM-DEEMED", "W1"]  # rejects first, then the most touched


def test_run_settings_name_their_choices():
    shown = dict(settings_view({"filing_validation": "claims", "acc_purpose": "claims", "include_societal": True}))
    assert shown["CEDARS filing checks"] == "Claims rules" and shown["Priced for"] == "Claims"


def test_recovery_codes_pages_use_sentence_case():
    for name in ("mfa/recovery_codes/base.html", "mfa/recovery_codes/index.html", "mfa/recovery_codes/generate.html"):
        t = get_template(name)
        assert "/cedars/core/templates/" in t.origin.name
        assert "Recovery Codes" not in open(t.origin.name).read()


def test_sign_in_and_new_codes_messages_in_the_sites_words(make_user):
    u = make_user("someone@example.org", "Sam One")
    assert render_to_string("account/messages/logged_in.txt", {"user": u}).strip() == "Signed in as Sam One."
    text = render_to_string("mfa/messages/recovery_codes_generated.txt", {"MFA_RECOVERY_CODES_SHOW_ONCE": True}).strip()
    assert text == "New recovery codes are ready. Save them somewhere safe now: they are not shown again."


@pytest.mark.skipif(shutil.which("recet") is None, reason="the pinned engine is not installed")
def test_import_rules_says_a_published_set_is_published(cpuc_admin):
    out = io.StringIO()
    call_command("import_rules", "claims", "--publish", stdout=out)
    assert out.getvalue().strip().endswith("published")
