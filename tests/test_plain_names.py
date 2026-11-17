"""Plain names wherever a person reads a thing (UI review 3, U12): permissions, history lines, data pack checks, the engine's documents
and missing-column findings name what they are in words, not by a code."""

from datetime import timedelta

import pytest
from django.utils import timezone

from cedars.core import engine_docs
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.refdata.models import TieOutRequirement, check_words
from cedars.services.registry import service as registry

from .conftest import page_only
from .test_programs import programs  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db


def test_an_audit_action_reads_as_words():
    assert audit.words("program_change.requested") == "Program change requested"
    assert audit.words("program_change.requested", verb_only=True) == "Requested"
    assert audit.words("organisation.renamed") == "Organization renamed"  # the words are US English; the action keeps its name
    assert audit.words("calculation_run.cancelled") == "Run canceled"
    assert audit.words("claim_year.setup_changed") == "Claim year set-up changed"
    assert audit.words("pack.etrm_indexed") == "Data pack eTRM permutations indexed"
    assert audit.words("") == ""


def test_a_program_page_names_the_fields_its_versions_changed(signed_in, make_user, programs):  # noqa: F811
    html = page_only(signed_in(make_user("someone@example.com")).get("/programs/PGE21001/").content.decode())
    assert "Status: Active to Closed" in html and "End year: (blank) to 2021" in html
    assert "Endyear" not in html


def test_a_change_request_history_says_what_happened(signed_in, pge_admin, programs):  # noqa: F811
    req = registry.request_change(
        pge_admin, programs["hvac"], effective=timezone.localdate() + timedelta(days=30), reason="moves it", segment="market_support"
    )
    html = page_only(signed_in(pge_admin).get(f"/programs/requests/{req.pk}/").content.decode())
    assert "Requested by" in html and "program_change." not in html


def test_the_audit_log_shows_an_action_in_words_and_its_name(signed_in, cpuc_admin, programs):  # noqa: F811
    html = page_only(signed_in(cpuc_admin).get("/admin/audit/").content.decode())
    assert "Program created" in html and "<code>program.created</code>" in html


def test_a_token_lists_its_modules_by_name(signed_in, make_user, pge, pge_admin):
    analyst = make_user("analyst@example.org")
    access.grant(pge_admin, analyst, pge, "impact_profiles.read")
    c = signed_in(analyst)
    c.post("/account/tokens", {"name": "loader", "organisation": "PGE", "scopes": ["impact_profiles"], "days": "90"})
    html = page_only(c.get("/account/").content.decode())
    assert "Impact profiles" in html and ">impact_profiles<" not in html


def test_pack_checks_have_plain_names(signed_in, cpuc_admin):
    assert check_words("source_workbook") == "Matches the published workbook"
    assert check_words("load_shapes_match") == "Load shapes match"
    assert TieOutRequirement.objects.filter(name="reference_measures").first().words == "Reference measures price alike"
    html = page_only(signed_in(cpuc_admin).get("/reference-data/packs/checks/").content.decode())
    assert "Matches the published workbook" in html and '<code class="small">source_workbook</code>' in html  # the name, for scripts


def test_an_engine_document_link_names_the_document_and_its_section():
    ref = engine_docs.reference("INPUT_FIELDS.md#13-keys-that-select-prices-profiles-and-rates")
    assert ref == {
        "doc": "Input fields",
        "section": "1.3 Keys that select prices, profiles and rates",
        "href": "/calculations/engine-docs/input-fields/#13-keys-that-select-prices-profiles-and-rates",
    }
    assert engine_docs.reference("OUTPUT_FIELDS.md#elecben")["section"] == "ElecBen"
    gone = engine_docs.reference("OUTPUT_FIELDS.md#no-such-heading")
    assert gone["section"] == "" and gone["href"] == "/calculations/engine-docs/output-fields/"  # the top of the document
    assert engine_docs.reference("README.md#x") is None


def test_a_dictionary_field_links_to_its_documentation_by_name(signed_in, make_user):
    import json

    from cedars.services.dictionary import service

    from .test_dictionary import CATALOG

    service.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    html = page_only(signed_in(make_user("someone@example.com")).get("/dictionary/fields/AC_Elec_Impact_Profile/").content.decode())
    assert (
        '<a href="/calculations/engine-docs/input-fields/#13-keys-that-select-prices-profiles-and-rates">1.3 Keys that select prices, '
        "profiles and rates, in the engine's Input fields</a>"
    ) in html
    assert ".md#" not in html.split("Calculation documentation", 1)[1].split("</dd>", 1)[0]


def test_access_messages_name_the_permission_in_words(pge, pge_admin, make_user):
    from cedars.modules.calculations import drafts
    from cedars.services.accounts.service import AccessDenied

    someone = make_user("someone@example.com")
    with pytest.raises(AccessDenied, match="Calculations: submit permission in PGE"):
        drafts._create(someone, pge, "a job", "blank", None)
