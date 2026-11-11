"""The parts the filing pages share (UI review 3, U1; submissions/filing_parts.py): the step line, the outcome with the one next action,
the findings grouped by rule with links to the rule and to each record, the page that follows its pricing run, Confirm and publish on
the version page, and filed savings that disagree with the priced ones marked."""

import re

import pytest
from django.urls import reverse

from cedars.modules.calculations.models import Run
from cedars.modules.claims import service
from cedars.services.submissions import filing_parts
from cedars.services.submissions.models import SubmissionVersion

from .conftest import sign_in
from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_claims import claim, claims_zip, deadline, filer, finish_run, programs, q1, row, tables, year  # noqa: F401

pytestmark = pytest.mark.django_db


def main(html: str) -> str:
    return html.split('<main id="main"', 1)[-1].split("</main>", 1)[0]


def primaries(html: str) -> list[str]:
    return re.findall(r'<button type="submit" class="primary">([^<]+)</button>', main(html))


def bad() -> bytes:
    """Q1 with two records the module rejects: a ClaimID of another PA and a program not in the registry."""
    return claims_zip(
        tables(
            claim("SCE-2028-1"),
            claim("PGE-2028-003", prg="NOPE"),
            ProgramCost=[row("ProgramCost", PrgID="PGE_Res")],
            Site=[row("Site", SiteID="PGE-S1")],
        )
    )


def test_a_rejected_claim_leads_with_the_step_the_outcome_and_the_findings_by_rule(client, filer, year, pge, programs):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad(), "bad.zip")
    assert v.status == SubmissionVersion.Status.DRAFT
    sign_in(client, filer)
    html = client.get(reverse("claims:period", args=[deadline().pk])).content.decode()
    body = main(html)
    # the step line: the current step in words and marked for screen readers
    assert 'aria-current="step"' in body and "Step 2 of 5, Check:" in body and "rejected by 2 rules" in body
    assert body.index('id="filing-steps"') < body.index('id="outcome"') < body.index('id="findings"') < body.index('id="upload-h"')
    # the findings: every rule with its records, each linked to its correction page
    assert 'id="finding-CLM-ID"' in body and 'id="finding-CLM-PRG"' in body
    record = reverse("submissions:record", args=[v.pk, "Measure"]) + "?k=PGE-2028-003"
    assert f'href="{record}"' in body
    assert reverse("submissions:records", args=[v.pk]) in body
    # uploading is the next step: its section is open and its button the page's only primary one
    assert primaries(html) == ["Upload and check"] and '<details class="section" id="upload">' not in body
    # the file's details and the statewide card are folded below
    assert '<details class="section" id="version-details">' in body


def test_a_validated_claim_offers_pricing_as_the_one_action_and_folds_the_upload(client, filer, year, pge, programs):  # noqa: F811
    service.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    sign_in(client, filer)
    html = client.get(reverse("claims:period", args=[deadline().pk])).content.decode()
    assert primaries(html) == ["Price the year to date"]
    assert "Step 3 of 5, Price:" in html and '<details class="section" id="upload">' in main(html)


def test_the_page_follows_its_pricing_run_and_says_when_it_ends(client, filer, year, pge, programs, release):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    run = Run.objects.get(pk=service.price(filer, v))
    sign_in(client, filer)
    html = client.get(reverse("claims:period", args=[deadline().pk])).content.decode()
    status = reverse("submissions:pricing_status", args=[v.pk]) + f"?run={run.pk}"
    assert f'data-watch-url="{status}"' in html and "core/watch.js" in html and "Reload this page when it has finished" not in html
    assert "Step 3 of 5, Price:" in html and "being priced" in html and primaries(html) == []
    state = client.get(status).json()
    assert state["ended"] is False and "Being priced" in state["text"]
    # only the run started for this version is reported
    assert client.get(reverse("submissions:pricing_status", args=[v.pk]) + "?run=00000000-0000-0000-0000-000000000000").json()["ended"]
    finish_run(run)
    state = client.get(status).json()
    assert state["ended"] and state["priced"]
    html = client.get(reverse("claims:period", args=[deadline().pk])).content.decode()
    assert primaries(html) == ["Confirm and publish"] and "Step 4 of 5, Confirm:" in html


def test_the_pricing_state_is_for_those_who_may_read_the_filing(client, filer, year, pge, programs, release, make_user):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    run = Run.objects.get(pk=service.price(filer, v))
    sign_in(client, make_user("someone@example.org", "Some One"))
    assert client.get(reverse("submissions:pricing_status", args=[v.pk]) + f"?run={run.pk}").status_code == 404


def test_the_version_page_offers_confirm_and_marks_filed_savings_that_differ(client, filer, year, pge, programs, release):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    finish_run(Run.objects.get(pk=service.price(filer, v)))
    sign_in(client, filer)
    html = client.get(reverse("claims:version", args=[v.pk])).content.decode()
    # the same status words as the filing page, and the one action where the results are reviewed
    assert "Validated and priced" in html and primaries(html) == ["Confirm and publish"]
    assert f'action="{reverse("claims:confirm", args=[deadline().pk])}"' in html
    # PGE_Res filed 800 kWh (PGE-2028-001) but priced to 170 kWh
    assert 'id="savings-differ"' in html and ">Differs</span>" in html
    r = client.post(reverse("claims:confirm", args=[deadline().pk]))
    assert r.status_code == 302
    v.refresh_from_db()
    assert v.locked
    html = client.get(reverse("claims:version", args=[v.pk])).content.decode()
    assert "Confirm and publish" not in primaries(html) and 'id="confirm-here"' not in html


def test_findings_by_rule_lists_every_missing_column_and_counts_records_once(filer, year, pge, programs):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, bad(), "bad.zip")
    groups = {g.rule_id: g for g in filing_parts.findings_by_rule(v, link_records=True)}
    assert groups["CLM-ID"].severity == "reject" and groups["CLM-ID"].count == len(groups["CLM-ID"].records)
    assert all(r["url"] for r in groups["CLM-PRG"].records)
    shown = filing_parts.findings_by_rule(v, limit=1)
    assert all(len(g.records) <= 1 for g in shown) and any(g.more for g in shown)


def test_a_check_written_in_code_links_to_its_row_on_the_built_in_checks_page(client, filer, year, pge, programs):  # noqa: F811
    from cedars.services.rules import coded

    v = service.upload_claims(filer, deadline(), pge, bad(), "bad.zip")
    checks = {c.rule_id: c for g in coded.groups() for c in g.checks}
    prg = {g.rule_id: g for g in filing_parts.findings_by_rule(v)}["CLM-PRG"]
    assert prg.url == reverse("rules:built_in") + "#CLM-PRG"
    assert prg.text == checks["CLM-PRG"].finds and prg.fix
    assert filing_parts.rule_url(v, "NOT-A-RULE", False) == ""
    sign_in(client, filer)
    body = main(client.get(reverse("claims:period", args=[deadline().pk])).content.decode())
    assert f'<a class="id" href="{reverse("rules:built_in")}#CLM-PRG">CLM-PRG</a>' in body
    # the row the link points at is on the Built-in checks page
    assert 'id="CLM-PRG"' in client.get(reverse("rules:built_in")).content.decode()


def test_the_stage_words():
    v = SubmissionVersion(number=1, status=SubmissionVersion.Status.VALIDATED, findings={"records": 3, "rejected": 0, "warnings": 1})
    assert filing_parts.stage(None, has_file=False, priced=False).key == "upload"
    assert filing_parts.stage(v, has_file=True, priced=False).key == "price"
    assert filing_parts.stage(v, has_file=True, priced=False, pricing={"id": "x", "ended": False}).key == "pricing"
    assert filing_parts.stage(v, has_file=True, priced=False, pricing={"id": "x", "ended": True}).key == "failed"
    assert filing_parts.stage(v, has_file=True, priced=True).key == "confirm"
    assert filing_parts.status_words(v, True) == "Validated and priced" and filing_parts.status_words(v, False) == "Validated"
    steps = filing_parts.stage(v, has_file=True, priced=True).steps
    assert [s["done"] for s in steps] == [True, True, True, False, False] and steps[3]["current"]
