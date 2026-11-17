"""A cost test written as a formula: `TRC = (ElecBen + GasBen + adder(TaxCredits)) / (ElecSupplyCost + share x ProgramCosts)`.

The formula is another way to fill the test page's benefits, numerator adders, costs and program-cost share; it says nothing a set's
file cannot. Each stream counts once and a formula only adds: a weight, a number or a minus sign is refused (that would need an
engine change). `adder(X)` puts X in the numerator adders. `share x ProgramCosts` is the test's own share of the program costs
(of its positive benefits); `share(TRC) x ProgramCosts` shares them as TRC does. `*` and `×` may stand for `x`."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

TIMES = r"\s*(?:x|\*|×)\s*"
SHARE = re.compile(rf"^share(?:\(\s*([A-Za-z][A-Za-z0-9]*)\s*\))?{TIMES}ProgramCosts$", re.IGNORECASE)
ADDER = re.compile(r"^adder\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)$", re.IGNORECASE)
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEAD = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=(?!=)\s*(.*)$", re.DOTALL)


@dataclass
class Parsed:
    """What a formula names: the test's id when it starts with `<T> =`, and the four fields of the test page."""

    test: str = ""
    benefits: list[str] = field(default_factory=list)
    numerator_adders: list[str] = field(default_factory=list)
    costs: list[str] = field(default_factory=list)
    share: str = ""  # "" none, "positive_benefits" its own share, or another test's id
    problems: list[str] = field(default_factory=list)


def render(tid: str, t: dict) -> str:
    """A test as a formula (the inverse of `parse`)."""
    ben = [*(t.get("benefits") or []), *(f"adder({a})" for a in t.get("numerator_adders") or [])]
    cost = list(t.get("costs") or [])
    pc = t.get("program_costs")
    share = pc.get("share") if isinstance(pc, dict) else ""
    if share == "positive_benefits":
        cost.append("share x ProgramCosts")
    elif share:
        cost.append(f"share({share}) x ProgramCosts")
    return f"{tid} = ({' + '.join(ben)}) / ({' + '.join(cost)})"


def _split_top(text: str, sep: str) -> list[str] | None:
    """`text` split on `sep` outside brackets; None when the brackets do not pair up."""
    out, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if depth:
        return None
    out.append("".join(cur))
    return out


def _unwrap(s: str) -> str:
    s = s.strip()
    while s.startswith("(") and s.endswith(")") and _split_top(s[1:-1], "\0") is not None:
        s = s[1:-1].strip()
    return s


def _suggest(word: str, names) -> str:
    names = list(names)
    lower = {n.lower(): n for n in names}
    if word.lower() in lower:
        return f" Did you mean {lower[word.lower()]}? Names are written with their capitals."
    near = difflib.get_close_matches(word, names, n=1, cutoff=0.6)
    return f" Did you mean {near[0]}?" if near else ""


def parse(text: str, streams, shares=()) -> Parsed:
    """Read a formula against the set: `streams` the names a test may read, `shares` the tests whose program-cost share it may take
    (`builder.share_choices`' test ids). Every problem is reported, in reading order, in words for the person typing."""
    names = list(streams)
    known = set(names)
    p = Parsed()
    body = " ".join((text or "").split())
    if not body:
        p.problems.append("Write the test as a formula, e.g. TRC = (ElecBen + GasBen) / (ElecSupplyCost + share x ProgramCosts).")
        return p
    m = HEAD.match(body)
    if m:
        p.test, body = m.group(1), m.group(2)
    halves = _split_top(body, "/")
    if halves is None:
        p.problems.append("The brackets do not pair up: each ( needs a ).")
        return p
    if len(halves) != 2 or not halves[0].strip() or not halves[1].strip():
        p.problems.append("Write one / between the benefits and the costs: (benefits) / (costs).")
        return p
    written = {"benefits": False, "costs": False}
    for where, half in (("benefits", halves[0]), ("costs", halves[1])):
        seen: set[str] = set()  # a stream may be on both sides (PCT: RebatesandIncentsPV), but once on each
        if not _unwrap(half):
            continue
        terms = _split_top(_unwrap(half), "+")
        if terms is None:
            p.problems.append("The brackets do not pair up: each ( needs a ).")
            return p
        for raw in terms:
            term = _unwrap(raw)
            written[where] = written[where] or bool(term)
            if not term:
                p.problems.append(f"The {where} have an empty term: two + together, or a + at an end.")
                continue
            if sm := SHARE.match(term):
                if where == "benefits":
                    p.problems.append(f"“{term}” is a cost: put the program costs below the line, in the costs.")
                elif p.share:
                    p.problems.append("The program costs are named twice; a test takes one share of them.")
                elif sm.group(1):
                    other = sm.group(1)
                    if other in shares:
                        p.share = other
                    else:
                        ok = ", ".join(shares) or "none in this set yet"
                        p.problems.append(
                            f"“{term}”: {other} does not compute its own share of the program costs, so no test can take it "
                            f"(tests that do: {ok}). Write share x ProgramCosts for this test's own share."
                        )
                else:
                    p.share = "positive_benefits"
                continue
            if am := ADDER.match(term):
                name = am.group(1)
                if where == "costs":
                    p.problems.append(f"“{term}”: an adder is added to the benefits; write it above the line.")
                    continue
                target = p.numerator_adders
            elif NAME.match(term):
                name = term
                if name.lower() == "programcosts":
                    p.problems.append(
                        "“ProgramCosts” is taken through a share: write share x ProgramCosts (this test's own share) or "
                        "share(TRC) x ProgramCosts (TRC's)."
                    )
                    continue
                target = p.benefits if where == "benefits" else p.costs
            elif re.search(r"[-*×]|\d|\bx\b", term):
                p.problems.append(
                    f"“{term}”: a formula only adds streams, each once. A weight, a number or a minus sign would need an engine change; "
                    "put a cost in the costs instead of taking it away."
                )
                continue
            else:
                p.problems.append(f"“{term}” is not a stream name, adder(…) or share x ProgramCosts.")
                continue
            if name not in known:
                p.problems.append(f"“{name}” is not a stream of this set.{_suggest(name, names)}")
                continue
            if name in seen:
                p.problems.append(f"{name} is named twice in the {where}; each stream counts once on a side.")
                continue
            seen.add(name)
            target.append(name)
    if not written["benefits"]:
        p.problems.append("A test needs at least one benefit stream above the line.")
    if not p.costs and not written["costs"]:
        p.problems.append("A test needs at least one cost stream below the line.")
    return p


ROLE_WORDS = {"benefit": "Benefit", "adder": "Adder", "cost": "Cost"}


def grid(data: dict) -> dict:
    """A set's tests as a review grid (Jake 2026-11-06: the grid "as a review of how the cost tests are built"): one row per stream
    with what it is in each test (benefit, adder, cost or nothing), the program costs' row with each test's share, and each test's
    formula. Rows no test uses are listed apart (`unused`), so the page can fold them away. Read-only; works from the set's mapping."""
    tests = [(t, v) for t, v in (data.get("tests") or {}).items() if isinstance(v, dict)]
    streams = [(n, s) for n, s in (data.get("streams") or {}).items() if isinstance(s, dict)]
    cols = []
    for tid, t in tests:
        pc = t.get("program_costs")
        share = pc.get("share") if isinstance(pc, dict) else ""
        cols.append(
            {
                "id": tid,
                "label": t.get("label") or "",
                "pricing": t.get("pricing") or "",
                "share": "its own share" if share == "positive_benefits" else f"{share}'s share" if share else "none",
                "variants": ", ".join(t.get("variants") or []) or "none",
                "headline": bool(t.get("headline")),
                "formula": render(tid, t),
            }
        )

    def roles(name, t):  # a stream may be on both sides (PCT: RebatesandIncentsPV)
        lists = (("benefit", "benefits"), ("adder", "numerator_adders"), ("cost", "costs"))
        return [r for r, k in lists if name in (t.get(k) or [])]

    used, unused, program = [], [], None
    for name, s in streams:
        if s.get("source") == "program":
            program = {"name": name, "definition": s.get("definition") or "", "cells": [{"id": c["id"], "text": c["share"]} for c in cols]}
            continue
        cells = [{"id": tid, "roles": [{"key": r, "text": ROLE_WORDS[r]} for r in roles(name, t)]} for tid, t in tests]
        row = {"name": name, "kind": s.get("kind") or "", "source": s.get("source") or "", "definition": s.get("definition") or "",
               "pricings": ", ".join(s.get("pricings") or []), "cells": cells}  # fmt: skip
        (used if any(c["roles"] for c in cells) else unused).append(row)
    order = {"benefit": 0, "cost": 1}
    used.sort(key=lambda r: order.get(r["kind"], 2))  # stable: the set's order within each kind
    return {"tests": cols, "rows": used, "unused": unused, "program": program}
