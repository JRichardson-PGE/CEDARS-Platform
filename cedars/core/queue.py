"""My queue (BR-09.5: assignment queues): the work waiting on the signed-in user, across every module, on one page (/queue/). Each kind of
work is a provider a service or module registers in its AppConfig.ready (core never imports a module): provider(user) -> [Item], the items
waiting on that user now. Nothing is stored: the queue is read live from each module's own records, so an item leaves it the moment the
work is done (a request decided, a version confirmed, a shift acknowledged, a draft published).

Registered today: correction requests to decide, confirmations, comments waiting for a reply (submissions); program change requests to
decide (registry); access requests to decide (accounts); fund shifts to acknowledge (Budgets); document drafts to publish, responses to
recommendations and unanswered questions (Documents)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from django.utils import timezone

log = logging.getLogger(__name__)


@dataclass
class Item:
    what: str  # the filing, request or document, in words
    url: str  # where the work is done
    organisation: str = ""  # the organization it is for (its code)
    since: datetime | date | None = None  # waiting since
    due: date | datetime | None = None  # when it is due, if it has a due date
    note: str = ""  # one more line ("You decide as Avery Prover's delegate.")

    @property
    def since_day(self) -> date | None:
        return timezone.localdate(self.since) if isinstance(self.since, datetime) else self.since

    @property
    def due_day(self) -> date | None:
        return timezone.localdate(self.due) if isinstance(self.due, datetime) else self.due

    @property
    def overdue(self) -> bool:
        return self.due_day is not None and self.due_day < timezone.localdate()


@dataclass
class Kind:
    key: str
    label: str  # the section's heading
    action: str  # the link's words ("Decide", "Confirm" ...)
    order: int
    provider: Callable
    help: str = ""


@dataclass
class Section:
    kind: Kind
    items: list[Item] = field(default_factory=list)


_kinds: dict[str, Kind] = {}


def register(key: str, label: str, provider: Callable, *, action: str, order: int, help: str = "") -> None:
    """A kind of work: provider(user) -> [Item] waiting on the user. Registering a key again replaces it."""
    _kinds[key] = Kind(key, label, action, order, provider, help)


def kinds() -> list[Kind]:
    return sorted(_kinds.values(), key=lambda k: (k.order, k.label))


def sections(user) -> list[Section]:
    """Every registered kind with the user's items, oldest due first then oldest waiting first. A provider that fails is logged and left
    out, so one module's fault never hides the rest of the queue."""
    out = []
    for k in kinds():
        try:
            items = list(k.provider(user))
        except Exception:
            log.exception("queue provider %s failed", k.key)
            continue
        items.sort(key=lambda i: (i.due_day or date.max, i.since_day or date.max, i.what))
        out.append(Section(k, items))
    return out
