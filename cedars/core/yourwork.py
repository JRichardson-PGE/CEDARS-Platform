"""Your work: the signed-in person's own work, first on the front page and on the section pages (UI review 3, U5). My queue stays the
full list of what waits on the person; this is the short version of it plus the person's own recent work: the organization's next filings,
the last runs, cost-test sets being built, saved views and downloads, a pending access request.

Each part is a provider a service or module registers in its AppConfig.ready, as My queue's kinds are (core never imports a module):
provider(request, page) -> Part | None, where page is the page asking ("front", "calculations", "reference", "published"). None leaves
the part out, so a person sees only the parts that concern them. Nothing is stored: every part is read live, like My queue."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from django.utils import timezone

log = logging.getLogger(__name__)

FRONT = "front"
PAGES = (FRONT, "calculations", "reference", "published")
SHOWN = 3  # lines a part shows; its "more" link leads to the full list


@dataclass
class Line:
    what: str  # the filing, run, set or request, in words
    url: str = ""  # its page
    action: str = ""  # the link's words ("Continue", "Open"); blank: the words themselves are the link
    status: str = ""  # a status badge's words (ui.status_tone gives its tone)
    detail: str = ""  # one more short line ("due 2026-11-01", "started 2026-11-10")
    overdue: bool = False
    action_url: str = ""  # where the action leads, when not to `url` (a run's results)

    @property
    def action_href(self) -> str:
        return self.action_url or self.url


@dataclass
class Part:
    key: str
    title: str
    lines: list[Line] = field(default_factory=list)
    total: int | None = None  # how many there are in all, when that is more than the lines shown
    more_url: str = ""
    more_label: str = ""
    empty: str = ""  # what to say when there are no lines


@dataclass
class Provider:
    key: str
    order: int
    pages: tuple[str, ...]
    provider: Callable


_providers: dict[str, Provider] = {}


def register(key: str, provider: Callable, *, order: int, pages: tuple[str, ...] = (FRONT,)) -> None:
    """A part of Your work: provider(request, page) -> Part | None, shown on each of `pages`. Registering a key again replaces it."""
    unknown = set(pages) - set(PAGES)
    if unknown:
        raise ValueError(f"Your work has no page {sorted(unknown)}")
    _providers[key] = Provider(key, order, tuple(pages), provider)


def providers(page: str | None = None) -> list[Provider]:
    found = [p for p in _providers.values() if page is None or page in p.pages]
    return sorted(found, key=lambda p: (p.order, p.key))


def parts(request, page: str) -> list[Part]:
    """The signed-in person's parts for a page, in order. Signed out: none. A provider that fails is logged and left out, so one module's
    fault never hides the rest (as in My queue)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return []
    out = []
    for p in providers(page):
        try:
            part = p.provider(request, page)
        except Exception:
            log.exception("your-work provider %s failed", p.key)
            continue
        if part is not None:
            out.append(part)
    return out


def day(value: date | datetime | None) -> date | None:
    return timezone.localdate(value) if isinstance(value, datetime) else value


# --- the queue's own part: core reads My queue itself


def _queue(request, page):
    from django.urls import reverse

    from . import queue

    items = []
    for s in queue.sections(request.user):
        items.extend((i, s.kind) for i in s.items)
    items.sort(key=lambda pair: (pair[0].due_day or date.max, pair[0].since_day or date.max, pair[0].what))
    lines = []
    for item, kind in items[:SHOWN]:
        due = item.due_day
        detail = f"due {due.isoformat()}" if due else ""
        lines.append(
            Line(
                what=item.what,
                url=item.url,
                action=kind.action,
                status="Overdue" if item.overdue else "",
                detail=" · ".join(x for x in (kind.label, detail) if x),
                overdue=item.overdue,
            )
        )
    n = len(items)
    return Part(
        "queue",
        "Waiting on you",
        lines,
        total=n,
        more_url=reverse("core:queue"),
        more_label=f"My queue ({n})" if n else "My queue",
        empty="Nothing is waiting on you.",
    )


register("queue", _queue, order=10)
