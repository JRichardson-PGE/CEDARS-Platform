"""The fund shift amounts page (UI review 3, U16): the program picker limited to the PA's programs, each line's operating budget beside
it and the running net, near matches for a PrgID that cannot be used, pasted lines, and a new shift opening on the current cycle."""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from cedars.modules.budgets import shifts
from cedars.modules.budgets.models import FundShift
from cedars.services.registry.models import FilingCycle

from .conftest import sign_in
from .test_budgets import filer, filing, job, programs, release  # noqa: F401 (fixtures)
from .test_fund_shifts import cycle, standing, statewide  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db


def test_pasted_lines_are_read():
    text = "PrgID\tYear\tAmount\nPGE_Res_HVAC\t2028\t-1,000\n\nPGE_Com_Ltg, 2028, 1,000.50\nSW_X;2029;$20\nPGE_X 2030 -5\n"
    got = [(ln.prg_id, ln.year, ln.amount) for ln in shifts.paste_lines(text)]
    assert got == [
        ("PGE_Res_HVAC", "2028", "-1,000"),
        ("PGE_Com_Ltg", "2028", "1,000.50"),
        ("SW_X", "2029", "$20"),
        ("PGE_X", "2030", "-5"),
    ]


def test_a_new_shift_opens_on_the_current_cycle():
    a = FilingCycle(first_year=2024, last_year=2027)
    b = FilingCycle(first_year=2028, last_year=2031)
    c = FilingCycle(first_year=2032, last_year=2039)
    assert shifts.current_cycle([c, b, a], today=date(2026, 11, 11)) is a
    assert shifts.current_cycle([c, b], today=date(2026, 11, 11)) is b  # none holds this year: the nearest one after it
    assert shifts.current_cycle([b, a], today=date(2040, 1, 1)) is b
    assert shifts.current_cycle([], today=date(2026, 1, 1)) is None


def test_the_amounts_page(client, filer, pge, cycle, standing, statewide):  # noqa: F811
    sign_in(client, filer)
    s = shifts.create(filer, pge, cycle, title="Into HVAC", reason="demand")
    url = reverse("budgets:shift_lines", args=[s.pk])
    html = client.get(url).content.decode()
    assert 'list="programs"' in html and '<option value="SW_X">SW X · statewide</option>' in html
    assert 'id="shift-budget"' in html and "PGE's operating budget now" in html and "Paste lines" in html
    assert "data-add-line" in html and "budgets/shift_lines.js" in html
    held = shifts.entry_budget(pge, s)
    hvac = held["PGE_Res_HVAC 2028"]
    assert hvac["factor"] is None and Decimal(held["SW_X 2028"]["factor"]) == Decimal("0.6")
    # a PrgID that is not the PA's: the near matches are named
    r = client.post(url, {"prg_id": ["PGE_Res_HVA", "PGE_Com_Ltg"], "year": ["2028", "2028"], "amount": ["10", "-10"]})
    assert "PrgID PGE_Res_HVA is not in the program registry. Did you mean PGE_Res_HVAC" in r.content.decode()
    # a saved line shows what its program-year holds, and the net counts a statewide line by the PA's factor
    r = client.post(url, {"prg_id": ["SW_X", "PGE_Com_Ltg"], "year": ["2028", "2028"], "amount": ["20", "-12"]})
    assert r.status_code == 302
    html = client.get(url).content.decode()
    assert "$0.00</td>" in html  # 20 x 0.6 - 12
    # pasted lines are added to the typed ones
    r = client.post(url, {"prg_id": ["PGE_Res_HVAC", ""], "year": ["2028", ""], "amount": ["5", ""], "paste": "PGE_Com_Ltg\t2028\t-5"})
    assert r.status_code == 302
    assert sorted((ln.prg_id, int(ln.amount)) for ln in FundShift.objects.get(pk=s.pk).lines.all()) == [
        ("PGE_Com_Ltg", -5),
        ("PGE_Res_HVAC", 5),
    ]


def test_new_fund_shift_offers_the_current_cycle_first(client, filer, cycle):  # noqa: F811
    sign_in(client, filer)
    html = client.get(reverse("budgets:shift_new")).content.decode()
    current = shifts.current_cycle()
    assert current is not None and f'<option value="{current.pk}" selected>' in html
