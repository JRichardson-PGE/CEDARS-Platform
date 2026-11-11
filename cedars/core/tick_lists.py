"""Compact tick lists (UI review 3, 2026-11-11): a long list of checkboxes shown as a closed disclosure that says what is chosen
("Sector: 2 chosen: Commercial, Residential"), with the chosen values first and, for a long list, a box that narrows it. Built once
here and drawn by `core/_tick_list.html`; `core/tick_lists.js` adds the narrowing box and keeps the summary current.

The boxes are plain checkboxes in the page's form, so the list works without scripts: open it, tick, send the form."""

from __future__ import annotations

from dataclasses import dataclass, field

NARROW_AT = 10  # a list longer than this gets a box that narrows it
NAMED = 3  # chosen values named in the summary, then "and N more"


@dataclass(frozen=True)
class Tick:
    value: str
    label: str
    checked: bool = False
    note: str = ""  # a word after the label, e.g. "retired"


@dataclass
class TickList:
    name: str  # the form parameter every box sends
    legend: str
    id: str
    ticks: list[Tick] = field(default_factory=list)
    none: str = "None chosen"  # the summary when nothing is ticked ("Any" for a filter)

    @property
    def chosen(self) -> list[Tick]:
        return [t for t in self.ticks if t.checked]

    @property
    def summary(self) -> str:
        return summary([t.label for t in self.chosen], self.none)

    @property
    def narrow(self) -> bool:
        return len(self.ticks) > NARROW_AT


def summary(labels: list[str], none: str = "None chosen") -> str:
    """'None chosen', '1 chosen: Commercial', '5 chosen: A, B, C and 2 more' (core/tick_lists.js writes the same)."""
    if not labels:
        return none
    named = ", ".join(labels[:NAMED])
    more = f" and {len(labels) - NAMED} more" if len(labels) > NAMED else ""
    return f"{len(labels)} chosen: {named}{more}"


def tick_list(name: str, legend: str, options, *, id: str, none: str = "None chosen") -> TickList:
    """A tick list from (value, label, checked) or (value, label, checked, note) tuples, in the list's own order with the ticked ones
    first."""
    ticks = [Tick(str(o[0]), str(o[1]), bool(o[2]), o[3] if len(o) > 3 else "") for o in options]
    return TickList(name, legend, id, [t for t in ticks if t.checked] + [t for t in ticks if not t.checked], none)
