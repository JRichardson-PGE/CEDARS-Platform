"""Notification settings as tables (UI review 3, U15): each group of events a table with Off, Page, and Page and email columns, a choice
that sets the whole group, the Save bar in view; and the account page links to a person's settings and saved work."""

from cedars.services.notifications import service
from cedars.services.notifications.models import Subscription

from .conftest import page_only

FILINGS = [e for e in service.EVENTS if e.group == service.FILINGS]


def test_each_group_is_a_table_of_radio_buttons(pge_admin, signed_in):
    service.subscribe(pge_admin, "pack.published", Subscription.Channel.EMAIL)
    html = page_only(signed_in(pge_admin).get("/notifications/settings/").content.decode())
    groups = {e.group for e in service.EVENTS}
    assert html.count('<table class="list choice-grid" id="eg') == len(groups)
    assert html.count('type="radio" name="') >= 3 * len(service.EVENTS)
    assert html.count('<select id="eg') == len(groups) and "Leave each as chosen below" in html
    assert 'name="pack.published" value="email" aria-labelledby="' in html
    checked = html.split('name="pack.published" value="email"', 1)[1].split(">", 1)[0]
    assert "checked" in checked
    off = html.split('name="deadline.moved" value="off"', 1)[1].split(">", 1)[0]
    assert "checked" in off  # nothing chosen yet: off
    assert '<div class="actions save-bar"><button type="submit" class="primary">Save</button>' in html
    assert "<select" not in html.split('id="request-notices"', 1)[1].split("</table>", 1)[0]  # requests are radio rows too


def test_setting_a_whole_group_applies_to_every_event_in_it(pge_admin, signed_in):
    c = signed_in(pge_admin)
    r = c.post("/notifications/settings/", {"group_1": "in_app", "submission.published:budgets": "email", "pack.published": "email"})
    assert r.status_code == 302
    saved = set(Subscription.objects.filter(user=pge_admin).values_list("event", "module", "channel"))
    assert {(e.code, e.module, "in_app") for e in FILINGS} <= saved  # the group's choice wins over its rows
    assert ("pack.published", "", "email") in saved  # another group's row as chosen
    c.post("/notifications/settings/", {"group_1": "off"})
    assert not Subscription.objects.filter(user=pge_admin, event__in=[e.code for e in FILINGS]).exists()


def test_leaving_the_group_choice_keeps_each_row(pge_admin, signed_in):
    signed_in(pge_admin).post("/notifications/settings/", {"group_1": "", "fund_shift.effective": "email"})
    saved = set(Subscription.objects.filter(user=pge_admin).values_list("event", "channel"))
    assert saved == {("fund_shift.effective", "email")}


def test_the_account_page_links_to_your_settings_and_saved_work(pge_admin, signed_in):
    html = page_only(signed_in(pge_admin).get("/account/").content.decode())
    links = html.split('aria-labelledby="yours-h"', 1)[1].split("</nav>", 1)[0]
    for href in ('href="/notifications/settings/"', 'href="/notifications/"', 'href="/data/views/"', 'href="/data/downloads/"'):
        assert href in links, href
