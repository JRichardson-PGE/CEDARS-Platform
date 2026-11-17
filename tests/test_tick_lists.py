"""Compact tick lists (UI review 3): cedars/core/tick_lists.py and the pages that use them (the document library's filter, document
subscriptions)."""

import pytest

from cedars.core import tick_lists
from cedars.modules.documents.models import AttributeList, AttributeOption

from .conftest import sign_in


def test_the_summary_names_what_is_chosen():
    assert tick_lists.summary([]) == "None chosen"
    assert tick_lists.summary([], "Any") == "Any"
    assert tick_lists.summary(["Commercial"]) == "1 chosen: Commercial"
    assert tick_lists.summary(list("ABCDE")) == "5 chosen: A, B, C and 2 more"


def test_chosen_values_come_first_and_a_long_list_narrows():
    t = tick_lists.tick_list("options", "Sector", [(1, "Ag", False), (2, "Com", True), (3, "Res", False, "retired")], id="s")
    assert [k.label for k in t.ticks] == ["Com", "Ag", "Res"] and t.ticks[2].note == "retired"
    assert t.summary == "1 chosen: Com" and not t.narrow
    long = tick_lists.tick_list("options", "Cycle", [(i, str(2000 + i), False) for i in range(tick_lists.NARROW_AT + 1)], id="c")
    assert long.narrow and long.summary == "None chosen"


def _sector_list():
    al = AttributeList.objects.filter(code="sector").first()
    if al is None:
        pytest.skip("no sector list")
    return al


@pytest.mark.django_db
def test_the_library_filter_folds_each_list(client):
    al = _sector_list()
    options = list(al.options.all())
    if not options or not al.collections:
        pytest.skip("the sector list is not used by a collection")
    chosen = options[-1]
    html = client.get("/documents/", {"options": chosen.pk}).content.decode()
    assert html.count('class="tick-list"') >= 1 and "data-tick-summary" in html
    assert f"1 chosen: {chosen.label}" in html
    first_box = html.index(f'<ul class="tick-options" id="filter-{al.pk}-ticks">')
    assert html.index(f'value="{chosen.pk}" checked', first_box) < html.index(f'value="{options[0].pk}"', first_box)  # chosen first
    assert "Any</span>" in client.get("/documents/").content.decode()


@pytest.mark.django_db
def test_subscriptions_fold_each_list(client, make_user):
    al = _sector_list()
    option = AttributeOption.objects.filter(attribute_list=al).first()
    user = make_user("reader@example.org", "Rae Reader")
    sign_in(client, user)
    html = client.get("/documents/subscriptions/").content.decode()
    assert f'id="follow-{al.pk}-ticks"' in html and "None followed" in html
    client.post("/documents/subscriptions/", {"options": [option.pk], "channel": "email"})
    html = client.get("/documents/subscriptions/").content.decode()
    assert f"1 chosen: {option.label}" in html
