"""Built-in checks (rules.coded): each module's list of the checks its code raises, shown read only at /rules/built-in/."""

import re
from pathlib import Path

import pytest
from django.urls import reverse

from cedars.services.rules import coded

ROOT = Path(__file__).parent.parent / "cedars"
# module: (folder whose code raises the checks, rule ID prefix)
SOURCES = {
    "budgets": ("modules/budgets", "BUD"),
    "claims": ("modules/claims", "CLM"),
    "monthly": ("modules/monthly", "MON"),
    "vault": ("services/vault", "PII"),
    "impact_profiles": ("modules/impact_profiles", "LS"),
}


def raised(folder: str, prefix: str) -> set[str]:
    ids = set()
    for p in (ROOT / folder).rglob("*.py"):
        if p.name != "coded_checks.py" and "migrations" not in p.parts:
            ids |= set(re.findall(rf'"({prefix}-[A-Z0-9]+)"', p.read_text(encoding="utf-8")))
    return ids


def test_every_list_names_exactly_the_checks_its_code_raises():
    groups = {g.module: g for g in coded.groups()}
    assert set(groups) == set(SOURCES)
    for module, (folder, prefix) in SOURCES.items():
        listed = {c.rule_id for c in groups[module].checks}
        code = raised(folder, prefix)
        assert code, f"no {prefix}- rule IDs found in {folder}: the test is looking in the wrong place"
        assert code - listed == set(), f"{module}: add these checks to its list (coded_checks.py): {sorted(code - listed)}"
        assert listed - code == set(), f"{module}: its list names checks the code no longer raises: {sorted(listed - code)}"
        for c in groups[module].checks:
            assert c.tables and c.finds and c.fix, c


def test_a_list_refuses_a_repeated_id_or_an_unknown_severity():
    c = coded.CodedCheck("X-ONE", "reject", "T", "F", "Finds.", "Fix.")
    with pytest.raises(ValueError):
        coded.register_coded_checks("test", "Test", "", [c, c])
    with pytest.raises(ValueError):
        coded.register_coded_checks("test", "Test", "", [coded.CodedCheck("X-TWO", "fatal", "T", "F", "Finds.", "Fix.")])
    assert "test" not in {g.module for g in coded.groups()}


@pytest.mark.django_db
def test_the_page_lists_them_to_signed_in_people(client, signed_in, pge_admin):
    url = reverse("rules:built_in")
    assert client.get(url).status_code == 302  # signed in only, as the rule library is
    signed_in(pge_admin)
    html = client.get(url).content.decode()
    total = sum(len(g.checks) for g in coded.groups())
    assert f"{total} checks." in html
    for label in ("Budget and application filings", "Claims", "Monthly reports", "Claims PII files", "Load-shape uploads"):
        assert f">{label}</h2>" in html
    assert '<tr id="MON-YTD"><th scope="row">MON-YTD</th><td>Warning</td>' in html
    assert '<tr id="CLM-CHAIN"><th scope="row">CLM-CHAIN</th><td>Rejects the record or warns, by case</td>' in html
    assert reverse("rules:built_in") in client.get(reverse("rules:index")).content.decode()


@pytest.mark.django_db
def test_the_filters(client, signed_in, pge_admin):
    signed_in(pge_admin)
    url = reverse("rules:built_in")
    html = client.get(url, {"severity": "warning"}).content.decode()
    assert 'id="MON-YTD"' in html and 'id="CLM-CHAIN"' in html and 'id="BUD-PRG"' not in html
    html = client.get(url, {"q": "ytd"}).content.decode()
    assert 'id="MON-YTD"' in html and 'id="MON-PRG"' not in html and ">Claims</h2>" not in html
    assert "1 of " in html
    assert "No built-in checks match." in client.get(url, {"q": "zzqx"}).content.decode()
