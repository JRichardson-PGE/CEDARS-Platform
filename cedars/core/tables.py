"""Shared parts for long lists (docs/DESIGN.md, "Tables"): sortable column headings, the pager's links and the applied-filter chips.

Everything is in the link, so a sorted, filtered page can be shared, works without scripts, and the pager and the chips keep the rest:
a heading's link keeps the filters and goes back to page 1; a pager link keeps the filters and the sort; a chip's Remove link keeps
everything but that one filter. A page renders them with `{% include "ui/sort_th.html" %}` for each heading, `{% pager %}` above and
below the table, and `{% include "ui/filter_chips.html" %}`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from django.http import QueryDict

SORT = "sort"
PAGE = "page"


@dataclass(frozen=True)
class Column:
    """A sortable column: its key in the link (?sort=key, or ?sort=-key for descending), its heading, and how it is sorted. `numeric`
    right-aligns the heading; `descending_first` makes a first click sort the largest or newest first (counts, dates). A list in
    Python sorts by `value` (a function of a row); a queryset by `field` (a field path, or several), the key itself when not given."""

    key: str
    label: str
    numeric: bool = False
    descending_first: bool = False
    value: Callable | None = field(default=None, compare=False)
    field: str | tuple[str, ...] = ""


@dataclass
class Sort:
    """The sort a page was asked for, and the headings to draw (`head`, one per column, sortable or not)."""

    key: str
    descending: bool
    head: list[dict]
    label: str

    @property
    def words(self) -> str:
        """For a table's caption: "sorted by PrgID", "sorted by Name, descending"."""
        return f"sorted by {self.label}{', descending' if self.descending else ''}"

    def ordering(self, columns: Sequence[Column], tie: Sequence[str] = ("pk",)) -> list:
        """The `order_by` arguments for a queryset: the chosen column's field(s), blanks last either way, then `tie` so a page never
        shows a row twice or skips one."""
        from django.db.models import F

        col = next((c for c in columns if c.key == self.key), None)
        names = (col.field if isinstance(col.field, tuple) else (col.field or col.key,)) if col else (self.key,)
        out = [F(n).desc(nulls_last=True) if self.descending else F(n).asc(nulls_last=True) for n in names]
        return out + list(tie)

    def rows(self, rows: Iterable, columns: Sequence[Column]):
        """Sort a list in Python by the chosen column's `value` (a function of a row). Blank values sort last either way, so a
        descending sort does not open on the empty ones."""
        col = next((c for c in columns if c.key == self.key), None)
        if col is None or col.value is None:
            return list(rows)
        rows = list(rows)
        blank = [r for r in rows if _blank(col.value(r))]
        filled = [r for r in rows if not _blank(col.value(r))]
        filled.sort(key=lambda r: _sort_key(col.value(r)), reverse=self.descending)
        return filled + blank


def _blank(v) -> bool:
    return v is None or v == ""


def _sort_key(v):
    return v.lower() if isinstance(v, str) else v


def clean(query: QueryDict, drop: Iterable[str] = (PAGE,)) -> QueryDict:
    """The query without blank values or the given parameters (the page number by default): the base of every link a list draws."""
    skip = set(drop)
    out = QueryDict(mutable=True)
    for key in query:
        if key in skip:
            continue
        values = [x for x in query.getlist(key) if x.strip()]
        if values:
            out.setlist(key, values)
    return out


def href(query: QueryDict, anchor: str = "") -> str:
    text = query.urlencode()
    return f"?{text}{f'#{anchor}' if anchor else ''}" if text else f"?{f'#{anchor}' if anchor else ''}"


def sorting(
    query: QueryDict,
    columns: Sequence[Column | tuple[str, str]],
    default: str,
    *,
    plain: Iterable[str] = (),
    drop: Iterable[str] = (),
    anchor: str = "",
) -> Sort:
    """Read ?sort= against `columns` (Column, or (key, label) for a plain ascending text column) and build the headings. `default` is
    the sort a page opens on ("key" or "-key"); an unknown key falls back to it. `plain` names headings drawn without a sort link
    (their key in the order of the table, mixed in by position: give them as Column too). `drop` names parameters a heading's link
    leaves out besides the page (a search box's one-off value, say)."""
    cols = [c if isinstance(c, Column) else Column(*c) for c in columns]
    plain = set(plain)
    by_key = {c.key: c for c in cols if c.key not in plain}
    asked = (query.get(SORT) or "").strip()
    key, desc = asked.lstrip("-"), asked.startswith("-")
    if key not in by_key:
        key, desc = default.lstrip("-"), default.startswith("-")
    base = clean(query, drop=(PAGE, SORT, *drop))
    head = []
    for c in cols:
        if c.key in plain:
            head.append({"key": c.key, "label": c.label, "num": c.numeric, "sortable": False, "sort": None, "href": ""})
            continue
        on = c.key == key
        # a second click on the sorted column turns it round; a first click uses the column's own first direction
        next_desc = (not desc) if on else c.descending_first
        q = base.copy()
        q[SORT] = f"-{c.key}" if next_desc else c.key
        head.append(
            {
                "key": c.key,
                "label": c.label,
                "num": c.numeric,
                "sortable": True,
                "sort": ("descending" if desc else "ascending") if on else None,
                "href": href(q, anchor),
            }
        )
    return Sort(key=key, descending=desc, head=head, label=by_key[key].label if key in by_key else key)


def chips(query: QueryDict, items: Iterable[tuple], *, anchor: str = "", drop: Iterable[str] = ()) -> list[dict]:
    """Each applied filter as {label, value, remove}: items are (parameter, label, value shown[, value]); with the fourth part only
    that one value of a repeated parameter is removed. Remove keeps every other filter and the sort, and goes back to page 1."""
    base = clean(query, drop=(PAGE, *drop))
    out = []
    for item in items:
        param, label, shown, *value = item
        q = base.copy()
        if value:
            rest = [x for x in q.getlist(param) if x != value[0]]
            if rest:
                q.setlist(param, rest)
            else:
                q.pop(param, None)
        else:
            q.pop(param, None)
        out.append({"label": label, "value": shown, "remove": href(q, anchor)})
    return out


def form_chips(form, *, skip: Iterable[str] = ()) -> list[tuple]:
    """(parameter, label, value shown) for each field of a bound, valid filter form that has a value: a choice shows its label, a box
    ticked shows "Yes". The items `chips` takes."""
    if not form.is_bound or not form.is_valid():
        return []
    skip = set(skip)
    out = []
    for name, f in form.fields.items():
        if name in skip:
            continue
        value = form.cleaned_data.get(name)
        if value in (None, "", False, [], ()):
            continue
        if value is True:
            shown = "Yes"
        elif hasattr(f, "choices"):
            shown = str(dict((str(k), v) for k, v in f.choices).get(str(value), value))
        else:
            shown = str(value)
        out.append((name, str(f.label or name), shown))
    return out


def pager_links(page, query: QueryDict | str = "", anchor: str = "") -> dict:
    """The links a pager draws for a Django Page: first, previous, next and last (blank where there is none), keeping the query."""
    base = QueryDict(query, mutable=True) if isinstance(query, str) else query.copy()
    base._mutable = True
    base.pop(PAGE, None)

    def to(n: int) -> str:
        q = base.copy()
        if n > 1:
            q[PAGE] = str(n)
        return href(q, anchor)

    n, last = page.number, page.paginator.num_pages
    return {
        "first": to(1) if n > 2 else "",
        "previous": to(n - 1) if page.has_previous() else "",
        "next": to(n + 1) if page.has_next() else "",
        "last": to(last) if n < last - 1 else "",
        "number": n,
        "pages": last,
        "start": page.start_index(),
        "end": page.end_index(),
        "count": page.paginator.count,
    }
