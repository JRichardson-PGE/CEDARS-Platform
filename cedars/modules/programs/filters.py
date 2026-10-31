"""The catalog's "Other filters": any program attribute beyond the search, PA, open or closed, segment and program year.

Each filter is a GET parameter named after its field, so a filtered catalog is a link that can be shared and the CSV download carries
the same filter. Several fields apply together; several values of one field keep programs with any of them. Pages render on the server
and work without scripts (decision K): the panel is a native <details>, and a "Show its options" button chooses the field when there is
no script (programs/catalog.js swaps the field's inputs in place instead).
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.http import QueryDict

from cedars.services.registry.importers import TRUE
from cedars.services.registry.models import Program, ProgramVersion

TEXT_MAX = 200
YEAR_MIN, YEAR_MAX = 1990, 2100
# Parameters that steer the page, never a filter: dropped from shared links, the pager and the download.
PAGE_PARAMS = ("page", "field", "pick")

STATUS_LABELS = {
    "new": "New",
    "active": "Active",
    "closed_to_new_commitments": "Closed to new commitments",
    "closure_pending": "Closure pending",
    "closed": "Closed",
}


def _flag(name: str) -> Callable[[Program, ProgramVersion], bool]:
    return lambda p, v: str((v.flags or {}).get(name, "")).strip().lower() in TRUE


def _flag_value(name: str) -> Callable[[Program, ProgramVersion], str]:
    return lambda p, v: str((v.flags or {}).get(name, "")).strip()


@dataclass(frozen=True)
class Field:
    """One filterable program attribute. kind: "choice" (tick values from those in the catalog), "text" (contains, ignoring case),
    "years" (from and to, both optional), "yesno"."""

    code: str
    label: str
    kind: str
    group: str
    value: Callable[[Program, ProgramVersion], object]
    help: str = ""
    suggest: bool = False  # a text field that offers the catalog's values as suggestions
    option_label: Callable[[str], str] | None = None

    @property
    def params(self) -> tuple[str, ...]:
        return (f"{self.code}_from", f"{self.code}_to") if self.kind == "years" else (self.code,)

    def values(self, p: Program, v: ProgramVersion) -> list[str]:
        """The row's values of a choice or text field, as a list (a program has several BP sectors)."""
        got = self.value(p, v)
        if isinstance(got, list | tuple | set):
            return [str(x) for x in got if str(x).strip()]
        return [str(got)] if got not in (None, "") else []

    def label_of(self, value: str) -> str:
        return self.option_label(value) if self.option_label else value


FIELDS = [
    Field("status", "Status", "choice", "Program", lambda p, v: v.status, option_label=lambda s: STATUS_LABELS.get(s, s)),
    Field("primary_sector", "Primary sector", "choice", "Program", lambda p, v: v.primary_sector),
    Field(
        "bp_sector",
        "Budget sector (BPSector)",
        "choice",
        "Program",
        lambda p, v: sorted((v.sectors or {}).keys()),
        help="Programs with any budget share in the sectors ticked.",
    ),
    Field("category", "Program category", "choice", "Program", lambda p, v: v.category),
    Field("commercial_name", "Commercial name", "text", "Program", lambda p, v: v.commercial_name, help="Contains these words."),
    Field(
        "start_year",
        "Start year",
        "years",
        "Program",
        lambda p, v: v.start_year,
        help="Programs with no start year are left out.",
    ),
    Field(
        "end_year",
        "End year",
        "years",
        "Program",
        lambda p, v: v.end_year,
        help="A program with no end year runs on, so it counts as later than any year.",
    ),
    Field("implementer", "Implementer type", "choice", "Delivery", lambda p, v: v.implementer),
    Field(
        "contractor",
        "Implementation contractor",
        "text",
        "Delivery",
        lambda p, v: v.implementation_contractor,
        help="Contains these words.",
        suggest=True,
    ),
    Field("direct_install", "Direct install", "choice", "Delivery", _flag_value("DirectInstall")),
    Field("financing", "Financing", "choice", "Delivery", _flag_value("Financing")),
    Field(
        "statewide_program",
        "Statewide program delivered",
        "text",
        "Delivery",
        lambda p, v: v.statewide_program,
        help="Contains these words.",
        suggest=True,
    ),
    Field(
        "parent_program", "Parent program", "text", "Delivery", lambda p, v: v.parent_program, help="Contains these words.", suggest=True
    ),
    Field("resource", "Resource", "yesno", "Flags", _flag("Resource_Flag")),
    Field("nonresource", "Non-resource", "yesno", "Flags", _flag("NonResource_Flag")),
    Field("deemed", "Deemed", "yesno", "Flags", _flag("Deemed_Flag")),
    Field("custom", "Custom", "yesno", "Flags", _flag("Custom_Flag")),
    Field("upstream", "Upstream", "yesno", "Flags", _flag("Upstream_Flag")),
    Field("midstream", "Midstream", "yesno", "Flags", _flag("Midstream_Flag")),
    Field("downstream", "Downstream", "yesno", "Flags", _flag("Downstream_Flag")),
    Field("audit", "Audit", "yesno", "Flags", _flag("Audit_Flag")),
    Field("exclude_budget", "Excluded from budget", "yesno", "Exclusions", lambda p, v: v.exclude_from_budget),
    Field("exclude_ce", "Excluded from cost-effectiveness", "yesno", "Exclusions", lambda p, v: v.exclude_from_ce),
    Field("exclude_statewide", "Excluded from statewide allocation", "yesno", "Exclusions", lambda p, v: v.exclude_from_statewide),
]
BY_CODE = {f.code: f for f in FIELDS}
GROUPS = list(dict.fromkeys(f.group for f in FIELDS))


def _year(text: str) -> int | None:
    try:
        y = int(text.strip())
    except (TypeError, ValueError):
        return None
    return y if YEAR_MIN <= y <= YEAR_MAX else None


def parse(query: QueryDict) -> tuple[dict, list[str]]:
    """The other filters a query sets, {code: value}, and a note for each value left out because it cannot be read.
    choice: [values]; text: str; years: (from or None, to or None); yesno: True / False."""
    active, notes = {}, []
    for f in FIELDS:
        if f.kind == "choice":
            picked = list(dict.fromkeys(x.strip() for x in query.getlist(f.code) if x.strip()))
            if picked:
                active[f.code] = picked
        elif f.kind == "text":
            text = query.get(f.code, "").strip()[:TEXT_MAX]
            if text:
                active[f.code] = text
        elif f.kind == "yesno":
            text = query.get(f.code, "").strip().lower()
            if text in ("yes", "no"):
                active[f.code] = text == "yes"
            elif text:
                notes.append(f"{f.label}: “{text}” is not yes or no, so that filter was left out.")
        else:
            ends = []
            for name, word in zip(f.params, ("from", "to"), strict=True):
                text = query.get(name, "").strip()
                y = _year(text) if text else None
                if text and y is None:
                    notes.append(f"{f.label} {word}: “{text[:20]}” is not a year from {YEAR_MIN} to {YEAR_MAX}, so it was left out.")
                ends.append(y)
            if ends[0] is not None and ends[1] is not None and ends[0] > ends[1]:
                ends.reverse()
            if ends != [None, None]:
                active[f.code] = tuple(ends)
    return active, notes


def matches(active: dict, p: Program, v: ProgramVersion) -> bool:
    for code, want in active.items():
        f = BY_CODE[code]
        if f.kind == "choice":
            if not set(want) & set(f.values(p, v)):
                return False
        elif f.kind == "text":
            if not any(want.lower() in x.lower() for x in f.values(p, v)):
                return False
        elif f.kind == "yesno":
            if bool(f.value(p, v)) != want:
                return False
        else:
            y, (lo, hi) = f.value(p, v), want
            if y is None:
                # An open end year runs on: later than any "from", never within a "to".
                if not (f.code == "end_year" and hi is None):
                    return False
            elif lo is not None and y < lo or hi is not None and y > hi:
                return False
    return True


def clean_query(query: QueryDict) -> QueryDict:
    """The query without blank values or the page's own parameters: the link to share, page through or download."""
    out = QueryDict(mutable=True)
    for key in query:
        if key in PAGE_PARAMS:
            continue
        values = [x for x in query.getlist(key) if x.strip()]
        if values:
            out.setlist(key, values)
    return out


def _href(query: QueryDict) -> str:
    text = query.urlencode()
    return f"?{text}" if text else "?"


def _without(query: QueryDict, key: str, value: str | None = None) -> str:
    q = query.copy()
    if value is None:
        q.pop(key, None)
    else:
        rest = [x for x in q.getlist(key) if x != value]
        if rest:
            q.setlist(key, rest)
        else:
            q.pop(key, None)
    return _href(q)


def _years_words(lo, hi) -> str:
    if lo is not None and hi is not None:
        return str(lo) if lo == hi else f"{lo} to {hi}"
    return f"{lo} or later" if lo is not None else f"{hi} or earlier"


def chips(query: QueryDict, main: list[tuple[str, str, str]], active: dict) -> list[dict]:
    """Every applied filter as {label, value, remove}: the main ones ([(param, label, value)]) first, then the other filters, one chip
    per ticked value. remove is the link to the same catalog without it."""
    q = clean_query(query)
    out = [{"label": label, "value": value, "remove": _without(q, param)} for param, label, value in main]
    for f in FIELDS:
        if f.code not in active:
            continue
        want = active[f.code]
        if f.kind == "choice":
            out += [{"label": f.label, "value": f.label_of(x), "remove": _without(q, f.code, x)} for x in want]
        elif f.kind == "years":
            q2 = q.copy()
            for name in f.params:
                q2.pop(name, None)
            out.append({"label": f.label, "value": _years_words(*want), "remove": _href(q2)})
        else:
            words = ("Yes" if want else "No") if f.kind == "yesno" else f"contains “{want}”"
            out.append({"label": f.label, "value": words, "remove": _without(q, f.code)})
    return out


def panel(query: QueryDict, active: dict, rows: list[tuple[Program, ProgramVersion]]) -> dict:
    """What the Other filters panel shows: each field with its current value and, for choices and suggestions, the values found in
    `rows` (the programs the main filters keep) plus any value ticked that they lack, so it can be unticked."""
    chosen = query.get("field", "")
    if chosen not in BY_CODE:
        chosen = ""
    fields = []
    for f in FIELDS:
        want = active.get(f.code)
        item = {"code": f.code, "label": f.label, "kind": f.kind, "group": f.group, "help": f.help, "active": want is not None}
        if f.kind == "choice" or f.suggest:
            found = sorted({x for p, v in rows for x in f.values(p, v)}, key=str.lower)
            if f.kind == "choice":
                ticked = want or []
                found += [x for x in ticked if x not in found]
                item["options"] = [(x, f.label_of(x), x in ticked) for x in found]
            else:
                item["suggestions"] = found
        if f.kind == "text":
            item["value"] = want or ""
        elif f.kind == "yesno":
            item["value"] = "" if want is None else ("yes" if want else "no")
        elif f.kind == "years":
            lo, hi = want or (None, None)
            item["from"], item["to"] = lo, hi
        fields.append(item)
    return {
        "fields": fields,
        "groups": [(g, [x for x in fields if x["group"] == g]) for g in GROUPS],
        "chosen": chosen,
        "open": bool(query.get("pick")),
        "applied": len(active),
        "year_min": YEAR_MIN,
        "year_max": YEAR_MAX,
    }
