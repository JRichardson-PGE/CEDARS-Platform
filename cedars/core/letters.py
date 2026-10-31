"""Change letters (BR-09.3): the public record of a correction to a published filing, written when version 2 or later of it is
published. Claims keeps its own (with the claims added, changed and withdrawn); Budgets and Monthly reports keep theirs as each
program's figures before and after. This is what their pages share: the program rows' formatting and `core/_letter_programs.html`."""

from decimal import Decimal


def money(x) -> str:
    if x is None:
        return ""
    d = Decimal(str(x))
    return f"-${-d:,.0f}" if d < 0 else f"${d:,.0f}"


def fmt(kind: str, x) -> str:
    if x is None:
        return ""
    if kind == "money":
        return money(x)
    if kind == "ratio":
        return f"{float(x):.2f}"
    if kind == "count":
        return f"{int(x):,}"
    return f"{float(x):,.0f}"


def difference(kind: str, before, after) -> str:
    if before is None or after is None:
        return ""
    d = float(after) - float(before)
    if abs(d) < (0.005 if kind == "ratio" else 0.5):
        return "No change"
    return ("+" if d > 0 else "-") + fmt(kind, abs(d)).lstrip("-")


def cells(figures, before: dict | None, after: dict | None) -> list[tuple[str, str, str]]:
    before, after = before or {}, after or {}
    return [
        (fmt(kind, before.get(key)), fmt(kind, after.get(key)), difference(kind, before.get(key), after.get(key)))
        for key, _, kind in figures
    ]


def program_rows(changes: dict, figures) -> dict:
    """The template context of `core/_letter_programs.html` from a letter's changes ({programs: [{prg_id, name, before, after}],
    totals: {before, after}}) and its figures [(key, label, kind)]."""
    programs = [
        {
            "prg_id": p["prg_id"],
            "name": p.get("name", ""),
            "new": p.get("before") is None,
            "gone": p.get("after") is None,
            "cells": cells(figures, p.get("before"), p.get("after")),
        }
        for p in changes.get("programs", [])
    ]
    totals = changes.get("totals", {})
    return {
        "head": [label for _, label, _ in figures],
        "programs": programs,
        "total": cells(figures, totals.get("before"), totals.get("after")),
        "colspan": 3 * len(figures) + 1,
    }


def summary(changes: dict, figures, *, limit: int = 5) -> str:
    """A few lines for a notification: how many programs changed, the first few with their first figure before and after, and the
    totals."""
    programs = changes.get("programs", [])
    key, label, kind = figures[0]
    lines = [f"{len(programs)} program{'s' if len(programs) != 1 else ''} changed." if programs else "No program's figures changed."]
    for p in programs[:limit]:
        b, a = (p.get("before") or {}).get(key), (p.get("after") or {}).get(key)
        what = "new" if p.get("before") is None else "no longer filed" if p.get("after") is None else f"{fmt(kind, b)} to {fmt(kind, a)}"
        lines.append(f"{p['prg_id']}: {label.lower()} {what}")
    if len(programs) > limit:
        lines.append(f"and {len(programs) - limit} more.")
    t = changes.get("totals", {})
    tb, ta = (t.get("before") or {}).get(key), (t.get("after") or {}).get(key)
    if tb is not None or ta is not None:
        lines.append(f"Total {label.lower()}: {fmt(kind, tb) or 'none'} to {fmt(kind, ta) or 'none'}.")
    return "\n".join(lines)
