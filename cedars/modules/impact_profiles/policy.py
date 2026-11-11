"""The Impact profiles module's policy settings, which a CPUC administrator changes on the settings page: the IOU territories a
profile with no territory is written for, and the first start date of the measure versions whose load shapes the platform takes.

Both were server settings; they are policy, so they are data now (LibrarySettings, one row per change, the newest in force). Until an
administrator first saves them, the server settings IMPACT_PROFILE_TERRITORIES and LOAD_SHAPE_FIRST_START are the values in force."""

import re
from dataclasses import dataclass
from datetime import date

from django.conf import settings

from cedars.services import audit

from . import access
from .models import LibrarySettings

CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,19}$")


@dataclass(frozen=True)
class Policy:
    territories: tuple[str, ...]
    load_shape_first_start: date
    record: LibrarySettings | None = None  # None: the server settings' defaults, not saved yet


def defaults() -> Policy:
    return Policy(tuple(settings.IMPACT_PROFILE_TERRITORIES), date.fromisoformat(settings.LOAD_SHAPE_FIRST_START))


def current() -> Policy:
    row = LibrarySettings.objects.first()
    if row is None:
        return defaults()
    return Policy(tuple(row.territories), row.load_shape_first_start, row)


def territories() -> tuple[str, ...]:
    return current().territories


def load_shape_first_start() -> date:
    return current().load_shape_first_start


def parse_territories(text: str) -> list[str]:
    """Codes separated by commas, spaces or lines, upper-cased, in the order given, each once."""
    out: list[str] = []
    for code in re.split(r"[\s,;]+", text.strip().upper()):
        if code and code not in out:
            out.append(code)
    return out


def change(actor, *, territories: list[str], load_shape_first_start: date, reason: str) -> LibrarySettings:
    """Save new settings (a new row; the old ones stay as the history) and audit the change."""
    access.require_admin(actor, "change the impact profile settings")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Give a reason for the change.")
    if not territories:
        raise ValueError("Name at least one IOU territory.")
    bad = [t for t in territories if not CODE.match(t)]
    if bad:
        raise ValueError(f"Not a territory code: {', '.join(bad)}. Use letters, digits and underscores, starting with a letter.")
    before = current()
    if list(before.territories) == list(territories) and before.load_shape_first_start == load_shape_first_start:
        raise ValueError("Nothing changed.")
    row = LibrarySettings.objects.create(
        territories=list(territories),
        load_shape_first_start=load_shape_first_start,
        set_by=actor if getattr(actor, "pk", None) else None,
        reason=reason,
    )
    audit.record(
        "impact_profile_settings.changed",
        obj=row,
        reason=reason,
        before={"territories": list(before.territories), "load_shape_first_start": before.load_shape_first_start.isoformat()},
        after={"territories": list(territories), "load_shape_first_start": load_shape_first_start.isoformat()},
        actor=actor,
    )
    return row


def history(limit: int = 50):
    return LibrarySettings.objects.select_related("set_by")[:limit]
