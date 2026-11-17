"""The shared parts of a long list (cedars/core/tables.py; UI review 3, U9): sortable headings, the pager above and below, filter chips."""

from django.core.paginator import Paginator
from django.http import QueryDict
from django.template import Context, Template

from cedars.core import tables

COLUMNS = [
    tables.Column("name", "Name", value=lambda r: r["name"]),
    tables.Column("n", "Count", numeric=True, descending_first=True, value=lambda r: r["n"]),
    tables.Column("note", "Note"),
]


def test_a_heading_sorts_by_its_column_and_turns_round():
    s = tables.sorting(QueryDict("q=x&page=3"), COLUMNS, "name", plain=("note",))
    assert (s.key, s.descending, s.words) == ("name", False, "sorted by Name")
    name, count, note = s.head
    # the sorted heading turns round; another starts in its own first direction; the filters stay and the page goes back to 1
    assert name == {"key": "name", "label": "Name", "num": False, "sortable": True, "sort": "ascending", "href": "?q=x&sort=-name"}
    assert count["href"] == "?q=x&sort=-n" and count["sort"] is None and count["num"]
    assert note["sortable"] is False and note["href"] == ""
    s = tables.sorting(QueryDict("sort=-n"), COLUMNS, "name")
    assert (s.key, s.descending, s.words) == ("n", True, "sorted by Count, descending")
    assert s.head[1]["href"] == "?sort=n"
    # an unknown or plain key falls back to the page's own default
    assert tables.sorting(QueryDict("sort=nonsense"), COLUMNS, "-n").key == "n"
    assert tables.sorting(QueryDict("sort=note"), COLUMNS, "name", plain=("note",)).key == "name"


def test_rows_sort_with_blanks_last_either_way():
    rows = [{"name": "b", "n": 2}, {"name": "", "n": None}, {"name": "A", "n": 10}]
    up = tables.sorting(QueryDict("sort=name"), COLUMNS, "name").rows(rows, COLUMNS)
    down = tables.sorting(QueryDict("sort=-n"), COLUMNS, "name").rows(rows, COLUMNS)
    assert [r["name"] for r in up] == ["A", "b", ""]  # case does not matter
    assert [r["n"] for r in down] == [10, 2, None]


def test_chips_remove_one_filter_and_keep_the_rest():
    q = QueryDict("pa=PGE&pa=SCE&q=hvac&sort=-name&page=2")
    chips = tables.chips(q, [("pa", "PA", "PGE", "PGE"), ("q", "Contains", "hvac")])
    assert chips[0] == {"label": "PA", "value": "PGE", "remove": "?pa=SCE&q=hvac&sort=-name"}
    assert chips[1]["remove"] == "?pa=PGE&pa=SCE&sort=-name"


def test_the_pager_is_drawn_above_and_below_with_first_and_last():
    page = Paginator(list(range(304)), 50).get_page(4)
    t = Template('{% pager page query=query label="Factor pages" where="above" anchor="f" %}')
    html = t.render(Context({"page": page, "query": "year=all&page=4"}))
    assert 'aria-label="Factor pages, above the list"' in html and 'class="pager pager-above"' in html
    assert 'href="?year=all#f">First' in html and 'href="?year=all&amp;page=3#f" rel="prev">Previous' in html
    assert 'href="?year=all&amp;page=7#f">Last' in html and "Page 4 of 7" in html and "(151 to 200 of 304)" in html
    first = Template('{% pager page label="Runs" previous="Newer" next="Older" %}').render(
        Context({"page": Paginator(list(range(60)), 50).get_page(1)})
    )
    assert "First" not in first and "Newer" not in first and 'href="?page=2" rel="next">Older' in first
    assert Template("{% pager page %}").render(Context({"page": Paginator([1], 50).get_page(1)})).strip() == ""
