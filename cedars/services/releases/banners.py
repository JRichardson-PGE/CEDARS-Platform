"""The bars at the top of every page, coming changes and site notices, and one rule for putting them away (UI review 3).

A bar can be put away once its note has been read: a coming change's note once its release note has been opened, a site notice's at
once (the bar holds the whole notice). A bar that has been put away stays away until it holds a note that was not in it then, so a new
note always brings it back.

What a visitor has opened and put away is kept in one cookie in their browser (no account needed, and it outlives sign-out and the
session's limits): `o` the notes opened, `a` the notes put away, each a list of keys, "c<pk>" for a release note and "n<pk>" for a site
notice. Nothing else is in it, and an unreadable cookie is read as empty."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.conf import settings

COOKIE = "cedars_bars"
MAX_AGE = 365 * 24 * 3600
KEEP = 60  # keys kept in each list: far more notes than are ever shown at once
_KEY = re.compile(r"^[cn][0-9]{1,9}$")


@dataclass
class Seen:
    opened: list[str] = field(default_factory=list)
    away: list[str] = field(default_factory=list)


@dataclass
class Bar:
    """One bar: what it holds (`notes` for the coming changes, `notice` for a site notice), its keys, whether it shows, and whether it
    can be put away now."""

    kind: str  # "coming" or "notice"
    keys: list[str]
    can_put_away: bool
    notes: list = field(default_factory=list)
    notice: object = None

    @property
    def name(self) -> str:
        """The value the Put away form sends: "coming", or "notice-<pk>"."""
        return "coming" if self.kind == "coming" else f"notice-{self.notice.pk}"


def note_key(note) -> str:
    return f"c{note.pk}"


def notice_key(notice) -> str:
    return f"n{notice.pk}"


def read(request) -> Seen:
    seen = Seen()
    for part in request.COOKIES.get(COOKIE, "").split(";"):
        name, _, keys = part.partition("=")
        if name in ("o", "a"):
            good = [k for k in keys.split(".") if _KEY.match(k)][-KEEP:]
            setattr(seen, "opened" if name == "o" else "away", good)
    return seen


def write(response, seen: Seen) -> None:
    value = f"o={'.'.join(seen.opened[-KEEP:])};a={'.'.join(seen.away[-KEEP:])}"
    response.set_cookie(
        COOKIE,
        value,
        max_age=MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=settings.SESSION_COOKIE_SECURE,
    )


def _add(keys: list[str], new: list[str]) -> list[str]:
    return [k for k in keys if k not in new] + list(new)


def opened(request, response, note) -> None:
    """Marks a release note as opened (its page was shown), so a bar announcing it can be put away."""
    seen = read(request)
    if note_key(note) not in seen.opened:
        seen.opened = _add(seen.opened, [note_key(note)])
        write(response, seen)


def bars(request, coming: list, notices: list) -> list[Bar]:
    """The bars this visitor sees on a page: each site notice not put away, then the coming changes unless put away. The coming-changes
    bar names its first two notes (and how many more); it can be put away once each note it names has been opened."""
    seen = read(request)
    out = []
    for n in notices:
        key = notice_key(n)
        if key not in seen.away:
            out.append(Bar("notice", [key], True, notice=n))
    keys = [note_key(n) for n in coming]
    if keys and not set(keys) <= set(seen.away):
        named = keys[:2]
        out.append(Bar("coming", keys, all(k in seen.opened for k in named), notes=coming))
    return out


def put_away(request, response, name: str, coming: list, notices: list) -> bool:
    """Puts away the bar `name` ("coming" or "notice-<pk>") as it shows now, if it may be. False when there is no such bar or it may not
    be put away yet."""
    bar = next((b for b in bars(request, coming, notices) if b.name == name), None)
    if bar is None or not bar.can_put_away:
        return False
    seen = read(request)
    seen.away = _add(seen.away, bar.keys)
    write(response, seen)
    return True
