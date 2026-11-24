"""The fund shift amounts page (UI review 3, U16): the program picker limited to the PA's programs, each line's operating budget beside
it and the running net, near matches for a PrgID that cannot be used, pasted lines, and a new shift opening on the current cycle."""

from datetime import date

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
    # a PA's own programs only: the statewide program it leads is entered on its own page, linked from here
    assert 'list="programs"' in html and '<option value="PGE_Res_HVAC">' in html and '<option value="SW_X">' not in html
    assert reverse("budgets:shift_statewide", args=[s.pk]) in html
    assert 'id="shift-budget"' in html and "PGE's operating budget now" in html and "Paste lines" in html
    assert "After this shift" in html and "data-add-line" in html and "budgets/shift_lines.js" in html
    held = shifts.entry_budget(pge, s)
    assert held["PGE_Res_HVAC 2028"] == {"held": "2400.00"}
    # a PrgID that is not the PA's: the near matches are named
    r = client.post(url, {"prg_id": ["PGE_Res_HVA", "PGE_Com_Ltg"], "year": ["2028", "2028"], "amount": ["10", "-10"]})
    assert "PrgID PGE_Res_HVA is not in the program registry. Did you mean PGE_Res_HVAC" in r.content.decode()
    # with a statewide change the lines cover the PA's portion: a saved line shows what its program-year holds now and after
    shifts.set_lines(filer, s, [shifts.LineIn("SW_X", "2028", "20")], shifts.STATEWIDE)
    r = client.post(url, {"prg_id": ["PGE_Com_Ltg"], "year": ["2028"], "amount": ["-10"]})
    assert r.status_code == 302
    html = client.get(url).content.decode()
    assert 'data-portion="12.00"' in html and '<td class="num" data-line-after>$390.00</td>' in html
    assert '<td class="num" data-total>-$10.00</td>' in html and '<td class="num" data-left>$2.00</td>' in html  # 20 x 0.6 - 10
    client.post(url, {"prg_id": ["PGE_Com_Ltg"], "year": ["2028"], "amount": ["-12"]})
    assert '<td class="num" data-left>$0.00 (covered)</td>' in client.get(url).content.decode()
    # pasted lines are added to the typed ones, and the statewide change stays
    r = client.post(url, {"prg_id": ["PGE_Res_HVAC", ""], "year": ["2028", ""], "amount": ["5", ""], "paste": "PGE_Com_Ltg\t2028\t-5"})
    assert r.status_code == 302
    assert sorted((ln.prg_id, int(ln.amount)) for ln in FundShift.objects.get(pk=s.pk).lines.all()) == [
        ("PGE_Com_Ltg", -5),
        ("PGE_Res_HVAC", 5),
        ("SW_X", 20),
    ]


def test_new_fund_shift_offers_the_current_cycle_first(client, filer, cycle):  # noqa: F811
    sign_in(client, filer)
    html = client.get(reverse("budgets:shift_new")).content.decode()
    current = shifts.current_cycle()
    assert current is not None and f'<option value="{current.pk}" selected>' in html
