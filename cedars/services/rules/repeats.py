"""One finding per problem: the findings a rule set gives that only repeat another finding on the same record are left out, so a filer
reads each problem once. Which records are rejected never changes: a finding is left out only beside one at least as severe.

* A field's own constraint first. Where a field constraint of the specification (not empty, a number, a range, a value list, a length)
  fails on a record, the rule tests of the same field on that record are left out: a blank RUL_Yrs is "RUL_Yrs should not be empty",
  not that and three rules that test RUL_Yrs and fail on the blank.
* The two halves of a pair. CEDARS states some rules both ways ("UnitTherm2ndBaseline should equal 0 when RUL_Yrs equals 0" and
  "RUL_Yrs should be different from 0 when UnitTherm2ndBaseline (or another 2nd-baseline field) is different from 0"): each condition
  of one is the other's test turned round. On a record where one half fails, the other half's finding is left out when each of its
  conditions that holds there is the turned-round test of a half that fails (`pairs`). Of two halves that fail on a record, the one
  with fewer conditions is kept (the rule ID breaks a tie).
* A module's own check first. A module that checks something itself (a budget filing's numbers, PrgIDs and one ProgramCost row per
  program and year) keeps its finding, whose fix says what to do on CEDARS, and the rule set's finding of the same thing on the same
  record is left out (`without_module_repeats`).

The rule set itself is unchanged (CEDARS' rules as published); only what is listed is."""

from __future__ import annotations

import re

NEGATION = {
    "equals": "does not equal",
    "does not equal": "equals",
    "is less than": "is greater than or equal to",
    "less than": "is greater than or equal to",
    "is greater than or equal to": "is less than",
    "greater than or equal to": "is less than",
    "is greater than": "is less than or equal to",
    "greater than": "is less than or equal to",
    "is less than or equal to": "is greater than",
    "in": "not in",
    "not in": "in",
    "is null": "is not null",
    "is not null": "is null",
}
_WHOLE = re.compile(r"^([-+]?\d+)\.0*$")


def _value(v) -> str:
    t = str(v if v is not None else "").strip()
    if len(t) >= 2 and t[0] == t[-1] == "'":
        t = t[1:-1]
    m = _WHOLE.match(t)
    return (m.group(1) if m else t).lower()


def _test(field: str, rule: str, value) -> tuple[str, str, str]:
    from .service import canonical_field

    return canonical_field(field).lower(), str(rule).strip().lower(), _value(value)


def _turned(field: str, rule: str, value) -> tuple[str, str, str] | None:
    neg = NEGATION.get(str(rule).strip().lower())
    return None if neg is None else _test(field, neg, value)


def _plain_conds(rule) -> list[tuple] | None:
    """A rule's conditions as (field, rule, value) when every one is a plain condition on the rule's own record, else None."""
    d = rule.definition or {}
    out = []
    for c in d.get("conds") or []:
        if isinstance(c, dict):
            return None
        tb, f, r, v = (list(c) + ["", "", "", ""])[:4]
        if str(tb or "").strip() and str(tb).strip().lower() != rule.table.lower():
            return None
        out.append((f, r, v))
    return out


def pairs(rules) -> dict[str, list[tuple[int, str]]]:
    """{rule ID B: [(index of B's condition, rule ID A), ...]}: B's condition at that index, turned round, is A's test, and one of A's
    conditions, turned round, is B's test. Only rules whose conditions are all plain conditions on their own record, joined by "any"
    (or only one), take part."""
    plain = {}
    for r in rules:
        if r.kind != "rule":
            continue
        d = r.definition or {}
        conds = _plain_conds(r)
        if not conds or (len(conds) > 1 and str(d.get("connector") or "all").lower() != "any"):
            continue
        plain[r.rule_id] = (r, _test(r.field, d.get("rule", ""), d.get("value", "")), [_turned(*c) for c in conds], conds)
    out: dict[str, list[tuple[int, str]]] = {}
    for b_id, (_b, b_test, b_turned, _bc) in plain.items():
        for a_id, (_a, a_test, a_turned, _ac) in plain.items():
            if a_id == b_id or b_test not in a_turned:
                continue
            for i, t in enumerate(b_turned):
                if t == a_test:
                    out.setdefault(b_id, []).append((i, a_id))
    return out


def _rank(rule) -> tuple:
    return (len(_plain_conds(rule) or []), rule.rule_id)


def without_repeats(rs, table: str, df, failing: list[tuple], *, filing_year=None, context=None) -> tuple[list[tuple], int]:
    """`failing` ([(rule ID, rows, values)], as rules.check has it) less the rows that only repeat another finding on the same record,
    and how many findings were left out."""
    from .service import EngineMissing, canonical_field

    by_id = {r.rule_id: r for r in rs.rules.filter(table=table)}
    rows_of = {rid: set(rows) for rid, rows, _v in failing}
    severity = {"reject": 2, "warning": 1}

    # a field's own constraint first
    constrained: dict[str, set[int]] = {}
    for rid, rows in rows_of.items():
        r = by_id.get(rid)
        if r is not None and r.kind == "spec" and "+" not in (r.field or ""):
            constrained.setdefault(canonical_field(r.field).lower(), set()).update(rows)
    drop: dict[str, set[int]] = {}
    for rid, rows in rows_of.items():
        r = by_id.get(rid)
        if r is None or r.kind != "rule":
            continue
        hit = rows & constrained.get(canonical_field(r.field).lower(), set())
        if hit:
            drop.setdefault(rid, set()).update(hit)

    # the two halves of a pair
    found_pairs = {b: p for b, p in pairs(by_id.values()).items() if b in rows_of}
    if found_pairs:
        try:
            holds = _condition_rows(by_id, found_pairs, table, df, filing_year, context)
        except (EngineMissing, ImportError):
            holds = None
        if holds is not None:
            for b_id, links in found_pairs.items():
                b = by_id[b_id]
                for row in rows_of[b_id] - drop.get(b_id, set()):
                    held = [i for i in range(len(_plain_conds(b) or [])) if row in holds[(b_id, i)]]
                    explained = all(
                        any(
                            a_id in rows_of
                            and row in rows_of[a_id] - drop.get(a_id, set())
                            and severity.get(by_id[a_id].severity, 0) >= severity.get(b.severity, 0)
                            and _rank(by_id[a_id]) < _rank(b)
                            for j, a_id in links
                            if j == i
                        )
                        for i in held
                    )
                    if held and explained:
                        drop.setdefault(b_id, set()).add(row)

    if not drop:
        return failing, 0
    out, left_out = [], 0
    for rid, rows, values in failing:
        gone = drop.get(rid, set())
        if not gone:
            out.append((rid, rows, values))
            continue
        kept = [(row, values[n] if n < len(values) else "") for n, row in enumerate(rows) if row not in gone]
        left_out += len(rows) - len(kept)
        if kept:
            out.append((rid, [k[0] for k in kept], [k[1] for k in kept]))
    return out, left_out


def _condition_rows(by_id, found_pairs, table, df, filing_year, context) -> dict[tuple[str, int], set[int]]:
    """{(rule ID, index of its condition): the rows where that condition holds}, for the rules that have a pair."""
    import numpy as np

    from .service import CheckContext, _engine, library_available

    filing_rules, _ = _engine()
    ctx = (context or CheckContext()).engine(filing_rules, filing_year, library_available())
    out = {}
    for b_id in found_pairs:
        b = by_id[b_id]
        for i, c in enumerate((b.definition or {}).get("conds") or []):
            held = filing_rules.applies({"conds": [c], "connector": "all"}, table, df, ctx)
            out[(b_id, i)] = {int(x) for x in np.flatnonzero(held)}
    return out


def without_module_repeats(rs, module_findings: list, rule_findings: list, same: dict[str, str]) -> tuple[list, int]:
    """`rule_findings` less those a module's own check already gives on the same record: `same` maps a module check's rule ID to the
    field constraint of the rule set it repeats (numeric, value_list, unique). A field constraint matches on the record's table and
    line and the same field (a unique key, on any of its columns). Returns the rule findings kept and how many were left out."""
    from .service import canonical_field

    if rs is None or not module_findings or not rule_findings:
        return rule_findings, 0
    checks = {r.rule_id: (r.definition or {}) for r in rs.rules.filter(kind="spec")}
    marked: dict[tuple[str, int], set[tuple[str, str]]] = {}
    for p in module_findings:
        kind = same.get(p.rule_id)
        if kind:
            marked.setdefault((p.table, p.row), set()).add((kind, canonical_field(p.field).lower()))
    if not marked:
        return rule_findings, 0
    kept, left_out = [], 0
    for f in rule_findings:
        d = checks.get(f.rule_id)
        at = marked.get((f.table, f.row))
        if d and at:
            kind = d.get("check", "")
            fields = {canonical_field(c).lower() for c in d.get("columns") or [f.field]}
            if any(k == kind and fld in fields for k, fld in at):
                left_out += 1
                continue
        kept.append(f)
    return kept, left_out


NOTE = (
    "Findings that only repeated another finding on the same record were left out: a rule testing a field that already fails its own "
    "constraint, the other half of a rule stated both ways, or the rule set repeating one of the module's own checks. Fixing the "
    "finding shown clears them."
)
