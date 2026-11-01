"""Help for each page (Jake, 2026-10-31): the (?) button beside every page's heading opens a guide to that page and a tour of it.

The help is data, kept beside the pages it describes: each app has a `page_help.yaml` (cedars/<services|modules>/<app>/, and cedars/core/)
whose entries are keyed by the page's template name without ".html" ("budgets/filing"). A template puts the button right after its
heading with `{% page_help "budgets/filing" %}` (the tag is a builtin, cedars/core/templatetags/page_help.py); a template shown for
several kinds of page passes a variant too (`{% page_help "data/dashboard" page.slug %}`, an entry "data/dashboard/overview").

An entry:
  title:  what the page is, as the dialog's heading says it ("A budget filing")
  guide:  paragraphs on what the page is for and what you can do on it
  how:    optional list of short how-to items
  see:    slug of the Help guide that goes further (core.help.GUIDES); every entry has one
  tour:   steps, each {title, text} and where it points: `target` (a CSS selector) or `heading` (the start of a heading's, legend's,
          summary's or caption's text); a heading's section, fieldset, disclosure or table is what is outlined. `optional: true` marks a
          part that only some people see or that only some states of the page have; the tour leaves out any step whose part is not on
          the page, and the tests require every step not marked optional to be found on each page they visit.
  tour_from: "front" (the front page only): the tour is core.help.tour_steps for the visitor, as before.

Without scripts the (?) button is a link to /help/pages/<key>/, which shows the same guide and the tour written out."""

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent  # cedars/
AREAS = {  # the Help index groups the pages by the file they come from, in this order
    "core": "Help, signing in and the front page",
    "data": "Portfolio dashboard and record downloads",
    "programs": "Programs",
    "budgets": "Budgets and fund shifts",
    "claims": "Claims",
    "monthly": "Monthly reports",
    "submissions": "Filing calendar and corrections",
    "calculations": "Calculations",
    "impact_profiles": "Impact profiles",
    "documents": "Documents and evaluation studies",
    "dictionary": "Data dictionary",
    "rules": "Validation rules",
    "refdata": "Reference data",
    "notifications": "Notifications",
    "administration": "Your account and administration",
    "retention": "Retention",
}
STEP_KEYS = {"title", "text", "target", "heading", "optional"}
ENTRY_KEYS = {"title", "guide", "how", "see", "tour", "tour_from"}
# The pages that are themselves a page's help have no (?) of their own: it would open help about the help (UI review 2, #41).
WITHOUT_HELP = {"core/help/page", "core/help/pages"}


@dataclass(frozen=True)
class TourStep:
    title: str
    text: str
    target: str = ""  # a CSS selector
    heading: str = ""  # or the start of a heading's text
    optional: bool = False
    link: str = ""  # (core.help.Step's fields, so the front page's steps go through the same template)
    link_text: str = ""

    @property
    def selector(self) -> str:
        return self.target


@dataclass(frozen=True)
class PageHelp:
    key: str
    area: str
    title: str
    guide: tuple[str, ...]
    how: tuple[str, ...] = ()
    see: str = ""
    tour: tuple[TourStep, ...] = field(default_factory=tuple)
    tour_from: str = ""

    @property
    def area_label(self) -> str:
        return AREAS.get(self.area, self.area)


# Every tour ends on the button that opened it, so the way back is learned once.
LAST_STEP = TourStep(
    "Help on every page",
    "The question mark beside a page's heading opens this page's guide and its tour. All of Help, with the user guides and how to get "
    "support, is in the header.",
    target=".page-help",
)


class PageHelpError(ValueError):
    pass


def files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("page_help.yaml") if "node_modules" not in p.parts)


def _area(path: Path) -> str:
    return path.parent.name


def _steps(key: str, raw) -> tuple[TourStep, ...]:
    steps = []
    for i, s in enumerate(raw or []):
        if not isinstance(s, dict) or set(s) - STEP_KEYS:
            raise PageHelpError(f"{key}: tour step {i + 1} has unknown fields {sorted(set(s) - STEP_KEYS) if isinstance(s, dict) else s}")
        if not s.get("title") or not s.get("text"):
            raise PageHelpError(f"{key}: tour step {i + 1} needs a title and a text")
        if bool(s.get("target")) == bool(s.get("heading")):
            raise PageHelpError(f"{key}: tour step {i + 1} needs one of target or heading")
        steps.append(
            TourStep(
                " ".join(s["title"].split()),
                " ".join(s["text"].split()),
                s.get("target", ""),
                s.get("heading", ""),
                bool(s.get("optional")),
            )
        )
    return tuple(steps)


def _para(text) -> str:
    return " ".join(str(text).split())


@cache
def entries() -> dict[str, PageHelp]:
    """Every page's help, by key. Raises PageHelpError on a malformed entry or a key in two files (the tests load it)."""
    out: dict[str, PageHelp] = {}
    for path in files():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key, e in data.items():
            if key in out:
                raise PageHelpError(f"{key}: in {path} and in {out[key].area}'s page_help.yaml")
            if not isinstance(e, dict) or set(e) - ENTRY_KEYS:
                raise PageHelpError(f"{key} ({path}): unknown fields {sorted(set(e) - ENTRY_KEYS) if isinstance(e, dict) else e}")
            if not e.get("title") or not e.get("guide"):
                raise PageHelpError(f"{key} ({path}): needs a title and a guide")
            guide = e["guide"] if isinstance(e["guide"], list) else [e["guide"]]
            tour = _steps(key, e.get("tour"))
            if not tour and not e.get("tour_from"):
                raise PageHelpError(f"{key} ({path}): needs a tour")
            out[key] = PageHelp(
                key,
                _area(path),
                _para(e["title"]),
                tuple(_para(g) for g in guide),
                tuple(_para(h) for h in e.get("how") or []),
                e.get("see", ""),
                tour + (LAST_STEP,) if tour else (),
                e.get("tour_from", ""),
            )
    return out


def lookup(key: str, variant: str = "") -> PageHelp | None:
    """A page's help: the variant's entry ("data/dashboard/overview") when there is one, else the page's."""
    found = entries()
    return (variant and found.get(f"{key}/{variant}")) or found.get(key)


def by_area() -> list[tuple[str, list[PageHelp]]]:
    groups: dict[str, list[PageHelp]] = {}
    for e in entries().values():
        groups.setdefault(e.area, []).append(e)
    order = list(AREAS)

    def rank(group):
        return order.index(group[0]) if group[0] in order else len(order)

    return [(AREAS.get(a, a), sorted(es, key=lambda e: e.title.lower())) for a, es in sorted(groups.items(), key=rank)]


def steps_for(entry: PageHelp, request) -> list:
    """The tour's steps on this page for this visitor."""
    if entry.tour_from == "front":
        from . import help as site_help

        return [*site_help.tour_steps(site_help.visitor(request))]
    return list(entry.tour)
