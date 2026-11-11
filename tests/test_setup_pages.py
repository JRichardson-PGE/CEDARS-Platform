"""The CPUC's set-up pages (UI review 3): Set up a claim year starts its five due dates from the last claim year's pattern, a set-up
page links its parts at the top with one main button and its rarely used actions folded, and an empty Filings board offers the CPUC's
administrators Set up a filing."""

import re
from datetime import date

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.modules.claims import service as claims

from .conftest import page_only

pytestmark = pytest.mark.django_db


def _year(admin, year, days):
    """A claim year whose five confirmations are due on the given dates (month, day), Q4's and the annual one the next year."""
    due = {}
    for (kind, n), (m, d) in zip(claims.PERIODS, days, strict=True):
        due[kind] = claims.due_at(date(year + (1 if n in (0, 4) else 0), m, d))
    return claims.add_year(admin, year=year, acc_version="2026", due=due)


PATTERN = [(5, 1), (8, 1), (11, 1), (2, 29), (5, 1)]  # Q4's due on 29 February of a leap year


def test_a_new_claim_year_starts_from_the_last_years_due_dates(signed_in, cpuc_admin):
    assert claims.suggested_due(2030) == (None, {})
    _year(cpuc_admin, 2027, [(5, 1), (8, 1), (11, 1), (2, 1), (5, 1)])
    _year(cpuc_admin, 2028, PATTERN[:3] + [(2, 28), (5, 1)])
    last, due = claims.suggested_due(2030)
    assert last == 2028  # the latest earlier year with deadlines, not the first
    assert due["Q1 claim"] == date(2030, 5, 1) and due["Annual (final) claim"] == date(2031, 5, 1)
    assert due["Q4 claim"] == date(2031, 2, 28)

    # the form starts on next year, from the latest year before it
    initial = timezone.localdate().year + 1
    c = signed_in(cpuc_admin)
    if initial <= 2027:
        assert "No earlier claim year has due dates to start from" in page_only(c.get("/claims/years/new/").content.decode())
        _year(cpuc_admin, initial - 1, [(5, 15), (8, 15), (11, 15), (2, 15), (5, 15)])
        page = page_only(c.get("/claims/years/new/").content.decode())
        assert f"Filled in from the {initial - 1} claim year's due dates" in page
        assert f'name="due_q1" value="{initial}-05-15"' in page and f'name="due_annual" value="{initial + 1}-05-15"' in page
    else:  # from 2027 on, next year's form starts from the 2028 claim year set up above
        page = page_only(c.get("/claims/years/new/").content.decode())
        assert "Filled in from the 2028 claim year's due dates" in page and f'name="due_q1" value="{initial}-05-01"' in page


def test_a_leap_day_due_date_moves_to_the_28th(cpuc_admin):
    _year(cpuc_admin, 2027, PATTERN)  # Q4 falls on 29 February 2028, a leap year
    _, moved = claims.suggested_due(2028)
    assert moved["Q4 claim"] == date(2029, 2, 28)


def test_a_set_up_page_links_its_parts_and_has_one_main_button(signed_in, cpuc_admin):
    _year(cpuc_admin, 2031, [(5, 1), (8, 1), (11, 1), (2, 1), (5, 1)])
    page = page_only(signed_in(cpuc_admin).get("/claims/years/2031/setup/").content.decode())
    nav = page.split('<nav class="setup-sections" aria-label="On this page">', 1)[1].split("</nav>", 1)[0]
    for anchor in ("#setup-pricing", "#setup-filings", "#setup-due-dates", "#setup-filers", "#remove-setup"):
        assert f'href="{anchor}"' in nav and f'id="{anchor[1:]}"' in page
    assert len(re.findall(r'<button[^>]*class="primary"', page)) == 1
    assert '<button type="submit" class="primary">Save how they are priced</button>' in page
    # each save keeps its own reason
    for action in ("due", "filers", "remove", "cancel"):
        form = page.split(f'name="action" value="{action}"', 1)[1].split("</form>", 1)[0]
        assert f'name="{action}-reason"' in form, action
    # the rarely used actions are folded
    fold = page.split('<details class="other-filters setup-fold" id="cancel-restore">', 1)[1].split("</details>", 1)[0]
    assert 'id="cancel-filing-form"' in fold
    remove = page.split('id="remove-setup"', 1)[1]
    assert remove.index("<details") < remove.index('value="remove"')


def test_an_empty_filings_board_offers_the_cpuc_set_up_a_filing(signed_in, cpuc_admin, pge_admin):
    admin = page_only(signed_in(cpuc_admin).get("/filings/").content.decode())
    status = admin.split('id="filing-status"', 1)[1].split("</section>", 1)[0]
    assert "No filing is on the calendar yet" in status
    assert f'<a class="button primary" href="{reverse("submissions:deadline_new")}">Set up a filing</a>' in status, status
    member = page_only(signed_in(pge_admin).get("/filings/").content.decode())
    status = member.split('id="filing-status"', 1)[1].split("</section>", 1)[0]
    assert "No filing is on the calendar yet" in status and "Set up a filing" not in status
