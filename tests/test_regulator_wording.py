"""Pages shown to an organization that files nothing (the CPUC, the statewide pseudo-PA, an evaluator) take the regulator's view: every
PA's filings and operating budgets to choose from, never "CPUC's operating budget" (Jake, 2026-11-02). tests/a11y/test_axe.py renders
every page of the accessibility scene as the CPUC administrator for the same wording."""

import re

import pytest
from django.urls import reverse

from cedars.services.accounts import service as access

from .conftest import page_only, sign_in
from .test_budgets import filer, filing, finish_run, job, programs, release  # noqa: F401 (fixtures)
from .test_fund_shifts import cycle, standing  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db


def owned_by(org_code: str, html: str) -> list[str]:
    """Wording that treats an organization as the one filing: "CPUC's operating budget", "CPUC operating budget", "TUAL: CPUC",
    "Once CPUC confirms". "The CPUC's own ..." is the regulator speaking and is allowed."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page_only(html)))
    c = re.escape(org_code)
    found = re.findall(rf"(?<!the )(?<!The )\b{c}'s \w+", text)
    found += re.findall(rf"\b{c} operating budget|: {c} \?|· {c} ·|\b{c} (?:confirms|files|to acknowledge)\b", text)
    return found


def test_only_program_administrators_file(cpuc, pge, cpuc_admin):
    sw = access.create_organisation(cpuc_admin, code="SW", name="Statewide", kind="pa")
    firm = access.create_organisation(cpuc_admin, code="EVAL", name="An evaluation firm", kind="other")
    untyped = access.create_organisation(cpuc_admin, code="NEWPA", name="A PA with no type yet", kind="pa")
    assert [o.files for o in (cpuc, sw, firm, pge, untyped)] == [False, False, False, True, True]
    assert access.filing_organisation(cpuc) is None and access.filing_organisation(None) is None
    assert access.filing_organisation(pge) == pge


def test_the_cpuc_sees_every_pas_budgets_not_its_own(client, cpuc_admin, standing, cycle):  # noqa: F811
    sign_in(client, cpuc_admin)
    deadline = standing.submission.deadline
    pages = {
        "budgets": client.get(reverse("budgets:index")),
        "filing": client.get(reverse("budgets:filing", args=[deadline.pk])),
        "shifts": client.get(reverse("budgets:shifts")),
        "operating": client.get(reverse("budgets:operating", args=[cycle.first_year])),
        "claims": client.get(reverse("claims:index")),
        "monthly": client.get(reverse("monthly:index")),
    }
    for name, r in pages.items():
        assert r.status_code == 200, name
        assert not owned_by("CPUC", r.content.decode()), (name, owned_by("CPUC", r.content.decode()))
    assert b"Program administrators' operating budgets, by cycle" in pages["shifts"].content
    operating = pages["operating"].content.decode()
    assert "<h1>Operating budgets, " in operating and '?org=PGE">PGE</a>' in operating
    assert "Choose a program administrator" in pages["filing"].content.decode() and '?org=PGE">PGE</a>' in pages["filing"].content.decode()
    assert client.get(reverse("budgets:operating_csv", args=[cycle.first_year])).status_code == 404  # a CSV needs a PA
    chosen = client.get(reverse("budgets:operating", args=[cycle.first_year]) + "?org=PGE")
    assert chosen.status_code == 200 and b"PGE operating budget" in chosen.content
    assert client.get(reverse("budgets:operating", args=[cycle.first_year]) + "?org=CPUC").status_code == 404


def test_a_pa_still_sees_its_own(client, filer, standing, cycle):  # noqa: F811
    sign_in(client, filer)
    assert b"PGE's operating budget" in client.get(reverse("budgets:shifts")).content
    assert b"<h1>PGE operating budget, " in client.get(reverse("budgets:operating", args=[cycle.first_year])).content
    filing_page = client.get(reverse("budgets:filing", args=[standing.submission.deadline.pk])).content.decode()
    assert "Choose a program administrator" not in filing_page and ": PGE" in filing_page
