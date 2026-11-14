"""Rule sets as data, run by the engine's rule runner (recet.filing_rules.check_frame) so the platform, ReCET runs and the pre-upload
checker apply exactly the same rules. The engine is imported only when rules are run or imported from it (engine/README.md)."""

import io
import re
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import jobs
from .jobs import CheckNotRun, engine_run  # noqa: F401 - the modules' way in
from .models import Rule, RuleSet

MODULES_FOR = {"claims": ["claims", "calculations"], "budget_filing": ["budgets", "calculations"]}
LABELS = {"claims": "Quarterly claims", "budget_filing": "Budget and application filings"}


class EngineMissing(RuntimeError):
    """The pinned ReCET engine is not installed in this environment."""


def _engine():
    try:
        from recet import fieldnames, filing_rules
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise EngineMissing("The ReCET engine is not installed here (engine/README.md), so rules cannot be run.") from e
    return filing_rules, fieldnames


def require_engine() -> None:
    """Raises EngineMissing when the pinned engine is not installed here."""
    _engine()


def suggested_fix(kind: str, field: str, definition: dict, message: str) -> str:
    """A plain-language fix for a finding, from the kind of rule (every finding says how to fix it)."""
    check = definition.get("check", "")
    if kind == "spec":
        return {
            "not_empty": f"Fill in {field}.",
            "max_length": f"Shorten {field} to {definition.get('n')} characters or fewer.",
            "numeric": f"Enter a plain number in {field}: no text, units, commas or currency symbols.",
            "bit": f"Enter 0 or 1 (or True or False) in {field}.",
            "year_quarter": f"Write {field} as a year and quarter, such as 2028Q1.",
            "value_list": f"Use a code from the {definition.get('list')} value list that is current for the claim year.",
            "range": f"Enter a value for {field} within the allowed range ({message.split(' should be ', 1)[-1]}).",
            "starts_with": f"Start {field} with {definition.get('pattern')}.",
            "unique": f"Make each combination of {field} appear only once.",
        }.get(check, f"Correct {field}.")
    return f"Change {field} so that this holds: {message[:1].lower() + message[1:]}."


# --- the tables a rule set's rules may read

_layout_providers: list = []


def register_layout_provider(provider) -> None:
    """Called by the specifications service: `provider(rule_set)` returns {"tables": [...], "keys": {table: [key columns]}} for the
    tables of the specification version that names the rule set (its non-vault tables), or None."""
    if provider not in _layout_providers:
        _layout_providers.append(provider)


def layout_of(rs: "RuleSet | None") -> dict:
    """The tables a rule set's rules may test and their key columns: those of the specification version that names it, else the two
    tables CEDARS' rule files cover."""
    if rs is not None:
        for provider in _layout_providers:
            found = provider(rs)
            if found:
                return {"tables": list(found.get("tables") or TABLES), "keys": dict(found.get("keys") or {})}
    return {"tables": list(TABLES), "keys": {}}


def tables_for(rs: "RuleSet | None") -> list[str]:
    return layout_of(rs)["tables"]


def condition_tables(rs: "RuleSet | None") -> list[str]:
    """What a condition may read: a table of the filing, the program's record or who files."""
    return [*tables_for(rs), PROGRAM, FILER]


# --- rule sets


def in_force(code: str, on: date | None = None) -> RuleSet | None:
    on = on or timezone.localdate()
    return RuleSet.objects.filter(code=code, status=RuleSet.Status.PUBLISHED, effective_from__lte=on).order_by("-effective_from").first()


def _require_admin(actor):
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator manages rule sets.")


@transaction.atomic
def import_from_engine(actor, code: str, as_of: str | None = None) -> RuleSet:
    """A draft rule set from the CEDARS rule files the pinned engine ships (data/cet_rules/<code>_<date>/)."""
    _require_admin(actor)
    filing_rules, _ = _engine()
    ers = filing_rules.rule_set(code, as_of)
    effective = date.fromisoformat(ers.version)
    if RuleSet.objects.filter(code=code, effective_from=effective).exists():
        raise AccessDenied(f"The {code} rules effective {ers.version} are already in the library.")
    rs = RuleSet.objects.create(
        code=code,
        label=LABELS.get(code, code),
        effective_from=effective,
        created_by=actor,
        source=f"CEDARS' rule files of {ers.version} ({', '.join(ers.describe()['files'])})",
    )
    Rule.objects.bulk_create(
        Rule(
            rule_set=rs,
            rule_id=r.id,
            severity=r.severity,
            table=r.table,
            field=r.field,
            message=canonical_text(r.text),
            kind=r.kind,
            definition=r.spec,
            source=r.source,
            modules=MODULES_FOR.get(code, []),
            fix=suggested_fix(r.kind, canonical_field(r.field), r.spec, canonical_text(r.text)),
        )
        for r in ers.rules
    )
    audit.record("rule_set.imported", obj=rs, after={"code": code, "effective_from": ers.version, "rules": len(ers.rules)}, actor=actor)
    return rs


class RuleError(ValueError):
    """A rule the engine could not run, or a change the rule library does not allow; the message says why."""


TABLES = ["Measure", "ProgramCost"]  # a rule set no specification version names (CEDARS' rule files cover these two)
# what a condition may read besides the tables of the filing: the program's record in the registry, and who files
PROGRAM = "Program"
FILER = "Filer"
PROGRAM_FIELDS = {
    "PrgID": "The program's ID",
    "PA": "The code of the PA that administers it",
    "Statewide": "1 for a statewide program, else 0",
    "Name": "The program's name",
    "Status": "Its status (new, active, closed ...)",
    "Segment": "Its segment",
    "PrimarySector": "Its primary sector",
    "Category": "Its category",
    "Implementer": "Who implements it",
    "StartYear": "Its first year",
    "EndYear": "Its last year",
}
FILER_FIELDS = {"PA": "The code of the PA that files", "PAType": "The kind of PA that files: IOU, REN or CCA"}
GROUPS = ("1", "2")  # the groups a condition may join (one level: a group holds plain conditions)
# what a validation or warning rule may test (recet.filing_rules._compare), as offered on the rule form
OPERATORS = [
    ("equals", "equals"),
    ("does not equal", "does not equal"),
    ("is less than", "is less than"),
    ("is less than or equal to", "is less than or equal to"),
    ("is greater than", "is greater than"),
    ("is greater than or equal to", "is greater than or equal to"),
    ("in", "is one of"),
    ("not in", "is not one of"),
    ("is not null", "is not blank"),
    ("is null", "is blank"),
    ("starts with string", "starts with"),
    ("ends with string", "ends with"),
    ("matches_year_quarter", "has the year of"),
    ("contains", "contains"),
    ("does not contain", "does not contain"),
    ("is true", "is true (1, true or yes)"),
    ("is not true", "is not true (blank, 0 or false)"),
    ("is before", "is a date before"),
    ("is on or before", "is a date on or before"),
    ("is after", "is a date after"),
    ("is on or after", "is a date on or after"),
    ("within years of upload", "is a date within this many years of the upload"),
    ("in effect on", "is a code in effect on the date in field"),
]
DATE_HELP = "A date as YYYY-MM-DD, a date field's name, or UploadDate for the day of the upload."
# the engine's other spellings, which CEDARS' own rule files use; accepted on a rule copied from them, not offered on the form
ALSO_ACCEPTED = {"greater than": "is greater than", "greater than or equal to": "is greater than or equal to", "less than": "is less than"}
NO_VALUE = {"is not null", "is null", "is true", "is not true"}
TEST_ONLY = {"within years of upload", "in effect on"}  # a rule's own test, never a condition
LIST_VALUE = {"in", "not in"}
# the field constraints a rule may be (recet.filing_rules._apply_spec), with the parameters each takes
CHECKS = [
    ("not_empty", "is filled in"),
    ("numeric", "is a number"),
    ("bit", "is 0, 1, True or False"),
    ("year_quarter", "is a year and quarter such as 2028Q1"),
    ("max_length", "is at most so many characters"),
    ("value_list", "is a code of a value list"),
    ("range", "is within a range"),
]
# CEDARS' specification also has two-column keys and UploadPA's prefix; kept on copied rules, not offered for new ones
ALSO_CHECKED = {"unique": "is unique", "starts_with": "starts with the PA and the filing year"}
WORDING = ["message", "fix", "difference", "modules"]
TEST = ["severity", "table", "field", "kind", "definition"]


def _value_text(rule: str, value) -> str:
    """A rule's value as the engine reads it: a list for "is one of" ("a, b" or ['a', 'b'] both become ['a', 'b'])."""
    v = str(value if value is not None else "").strip()
    if rule in NO_VALUE:
        return ""
    if rule in LIST_VALUE and not v.startswith("["):
        return repr([x.strip() for x in v.split(",") if x.strip()])
    return v


def rule_definition(
    field: str, rule: str, value="", conditions=(), connector: str = "all", table: str = "", groups: dict | None = None, value_list=""
) -> dict:
    """A validation or warning rule's definition as the engine reads it (the rule files' Field, Rule, Value and up to five Where
    conditions). `conditions` is a list of (field, rule, value), or (table, field, rule, value, group): the table a condition reads
    (blank: the record's own; another table of the filing, Program or Filer) and the group it joins ("" or one of GROUPS). The test
    applies to a record only where the conditions hold (all, or any); a group's conditions are joined by its own connector
    (`groups`: {group: "all" | "any"}) and the group counts as one condition, placed where its first condition is."""
    rule = (rule or "").strip().lower()
    conds: list = []
    placed: dict[str, dict] = {}
    for c in conditions:
        if len(c) == 3:
            tb, f, r, v, g = table, *c, ""
        else:
            tb, f, r, v, g = (list(c) + ["", "", "", "", ""])[:5]
        if not (f or "").strip():
            continue
        r = (r or "").strip().lower()
        cond = [(tb or "").strip() or table, f.strip(), r, _value_text(r, v)]  # CEDARS' files name the table on each condition
        g = str(g or "").strip()
        if not g:
            conds.append(cond)
            continue
        if g not in placed:
            placed[g] = {"connector": "any" if (groups or {}).get(g) == "any" else "all", "conds": []}
            conds.append(placed[g])
        placed[g]["conds"].append(cond)
    out = {
        "field": (field or "").strip(),
        "rule": rule,
        "value": _value_text(rule, value),
        "connector": "any" if connector == "any" else "all",
        "conds": conds,
    }
    if rule == "in effect on" and (value_list or "").strip():
        out["list"] = value_list.strip()
    return out


def check_definition(check: str, **params) -> dict:
    """A field constraint's definition as the engine reads it."""
    d: dict = {"check": check}
    if check == "max_length":
        try:
            n = int(params.get("n"))
        except (TypeError, ValueError) as e:
            raise RuleError("Give the largest number of characters.") from e
        if n < 1:
            raise RuleError("The largest number of characters is at least 1.")
        d["n"] = n
    elif check == "value_list":
        d["list"] = (params.get("list") or "").strip()
    elif check == "range":

        def num(x):
            if x in (None, ""):
                return None
            try:
                return float(x)
            except (TypeError, ValueError) as e:
                raise RuleError(f"{x!r} is not a number.") from e

        lo, hi = num(params.get("lo")), num(params.get("hi"))
        if lo is None and hi is None:
            raise RuleError("Give the lowest value, the highest or both.")
        if lo is not None and hi is not None and hi < lo:
            raise RuleError("The highest value is below the lowest.")
        d |= {"lo": lo, "hi": hi, "lo_open": bool(params.get("lo_open")), "hi_open": bool(params.get("hi_open"))}
    return d


def _spec_text(field: str, d: dict) -> str:
    """A field constraint's wording, as the engine words the ones it reads from CEDARS' specification."""
    c = d["check"]
    if c == "max_length":
        return f"{field} should be at most {d['n']} characters"
    if c == "numeric":
        return f"{field} should be a number"
    if c == "bit":
        return f"{field} should be 0, 1, True or False"
    if c == "year_quarter":
        return f"{field} should be a year and quarter such as 2028Q1"
    if c == "not_empty":
        return f"{field} should not be empty"
    if c == "value_list":
        return f"{field} should be in the {d['list']} value list"
    if c == "unique":
        return f"{field} should be unique"
    if c == "starts_with":
        return f"{field} should start with {d.get('pattern', '')}"
    if c == "range":
        fmt = lambda x: "" if x is None else f"{x:g}"  # noqa: E731
        if d["hi"] is None and not d["lo_open"]:
            return f"{field} should be greater than or equal to {fmt(d['lo'])}"
        return f"{field} should be in {'(' if d['lo_open'] else '['}{fmt(d['lo'])}, {fmt(d['hi'])}{')' if d['hi_open'] else ']'}"
    return f"{field} should hold"


def _id_and_text(kind: str, severity: str, table: str, field: str, d: dict) -> tuple[str, str]:
    """The rule's ID (the engine's content hash over the same parts as CEDARS' rules, so the same rule has the same ID wherever it
    is) and its plain wording."""
    filing_rules, _ = _engine()
    if kind == "spec":
        params = sorted(f"{k}={v}" for k, v in d.items() if k != "check")
        return filing_rules._rule_id("S", table, field, d["check"], *params), _spec_text(field, d)
    rid = filing_rules.rule_id(severity, table, field, d["rule"], d["value"], d["connector"], d["conds"])
    return rid, filing_rules.describe(d, table)


def known_field(name: str) -> bool:
    from cedars.services.dictionary import service as dictionary

    return dictionary.current() is None or dictionary.resolve(name) is not None


def _own(cond_table: str, table: str) -> bool:
    return not (cond_table or "").strip() or cond_table.strip().lower() == table.lower()


def validate(kind: str, severity: str, table: str, field: str, d: dict, rs: "RuleSet | None" = None) -> None:
    """Refuse a rule the engine cannot run: an unknown table, field, operator, value list or value. The rule is run once on a blank
    record (with blank records of the tables its conditions read), so a rule saved here is one the checker runs. `rs`: the rule set it
    is for, whose specification version decides the tables."""
    filing_rules, fieldnames = _engine()
    if severity not in Rule.Severity.values:
        raise RuleError("Choose whether the rule rejects the record or gives a warning.")
    lay = layout_of(rs)
    tables = lay["tables"]
    if table not in tables:
        raise RuleError(f"Choose the table: {', '.join(tables)}.")
    if not field:
        raise RuleError("Give the field the rule tests.")
    conds = [] if kind == "spec" else filing_rules.conditions(d)
    if kind == "rule":
        for c in d.get("conds") or []:
            if isinstance(c, dict):
                if not c.get("conds"):
                    raise RuleError("A group of conditions is empty.")
                if any(isinstance(x, dict) for x in c["conds"]):
                    raise RuleError("Groups of conditions hold plain conditions only.")
        where = {t.lower(): t for t in condition_tables(rs)}
        for c in conds:
            if not _own(c[0], table) and c[0].strip().lower() not in where:
                raise RuleError(f"A condition reads {c[0]}, which is not a table of the filing, {PROGRAM} or {FILER}.")
            if c[0].strip().lower() == PROGRAM.lower() and c[1] not in PROGRAM_FIELDS:
                raise RuleError(f"{c[1]} is not a field of the program record ({', '.join(PROGRAM_FIELDS)}).")
            if c[0].strip().lower() == FILER.lower() and c[1] not in FILER_FIELDS:
                raise RuleError(f"{c[1]} is not something known about who files ({', '.join(FILER_FIELDS)}).")
            if not _own(c[0], table) and c[0].strip().lower() not in (PROGRAM.lower(), FILER.lower()):
                keys = {k.lower(): v for k, v in lay["keys"].items()}
                if not keys.get(c[0].strip().lower()) and not keys.get(table.lower()):
                    raise RuleError(f"No key links {table} to {c[0]}, so a condition cannot read it.")
            if c[2] in TEST_ONLY:
                raise RuleError(f'"{dict(OPERATORS)[c[2]]}" is a test, not a condition.')
    named = [field] + [c[1] for c in conds if c[0].strip().lower() not in (PROGRAM.lower(), FILER.lower())]
    fields = [part.strip() for f in named for part in f.split("+") if part.strip()]  # a key or combination names several
    unknown = [f for f in fields if not known_field(f)]
    if unknown:
        raise RuleError(f"{', '.join(unknown)} is not a field in the data dictionary (any of its names may be used).")
    if kind == "spec":
        if d["check"] not in dict(CHECKS) | ALSO_CHECKED:
            raise RuleError("Choose what the field must be.")
        if d["check"] == "value_list":
            from cedars.services.dictionary import value_lists

            name = filing_rules.VALUE_LIST_FILES.get(d["list"].lower(), d["list"].lower())
            if name != "prgid" and name not in value_lists.engine_lists():  # PrgID: the program registry
                raise RuleError(f"There is no value list called {d['list'] or '(blank)'}.")
    else:
        ops = dict(OPERATORS) | {k: dict(OPERATORS)[v] for k, v in ALSO_ACCEPTED.items()}
        if d["rule"] == "combo_value_list":  # CEDARS' profile-combination rule, checked against the library
            ops["combo_value_list"] = "is a combination in the impact-profile library"
        for r in [d["rule"]] + [c[2] for c in conds]:
            if r not in ops:
                raise RuleError(f"{r or '(blank)'} is not a test the checker runs.")
        for r, v in [(d["rule"], d["value"])] + [(c[2], c[3]) for c in conds]:
            if r not in NO_VALUE and not v:
                raise RuleError(f'Give the value for "{ops[r]}".')
        if d["rule"] == "within years of upload":
            try:
                if float(d["value"]) < 0 or float(d["value"]) != int(float(d["value"])):
                    raise ValueError
            except ValueError:
                raise RuleError("Give the number of years as a whole number.") from None
        if d["rule"] == "in effect on" and not known_field(str(d["value"]).strip()):
            raise RuleError("Give the date field the code must be in effect on.")
    import pandas as pd

    def columns_of(tb: str) -> set[str]:
        own = _own(tb, table)
        out = {fieldnames.recet_name(f) for f in ([field] if own else [])}
        out |= {fieldnames.recet_name(c[1]) for c in conds if (own and _own(c[0], table)) or (not own and c[0].strip() == tb)}
        if kind == "rule" and own:
            for v in [d["value"]] + [c[3] for c in conds if _own(c[0], table)]:
                head = str(v).split()[0] if str(v).split() else ""
                if head and head.isidentifier() and known_field(head) and head.lower() not in ("filingyear", "uploaddate"):
                    out.add(fieldnames.recet_name(head))
        return out

    others = {c[0].strip() for c in conds if not _own(c[0], table) and c[0].strip().lower() not in (PROGRAM.lower(), FILER.lower())}
    keys = lay["keys"]
    program = {"PrgID"} if any(c[0].strip().lower() == PROGRAM.lower() for c in conds) else set()
    frames = {table: pd.DataFrame({c: [""] for c in columns_of(table) | set(keys.get(table, [])) | program})}
    for tb in others:
        name = next((t for t in tables if t.lower() == tb.lower()), tb)
        frames[name] = pd.DataFrame({c: [""] for c in columns_of(tb) | set(keys.get(name, []))})
    ctx = filing_rules.Context(
        filing_year=2028,
        upload_date=date(2028, 1, 1),
        filer={k: "" for k in FILER_FIELDS},
        programs=pd.DataFrame({k: [] for k in PROGRAM_FIELDS}),
        tables=frames,
        keys=keys,
    )
    probe = filing_rules.RuleSet("check", "0000-00-00", None, [filing_rules.Rule("X", severity, table, field, "", kind, "", d)])
    try:
        _results, notes = filing_rules.check_frame(probe, table, frames[table], ctx)
    except Exception as e:  # noqa: BLE001 - whatever the engine says is the reason the rule cannot be saved
        raise RuleError(f"The checker cannot run this rule: {e}") from e
    blocked = [n for n in notes if "no key links" in n or ("the filing has no " in n and n.rstrip(" ()").endswith("table"))]
    if blocked:
        raise RuleError(f"The checker cannot run this rule: {blocked[0].split(': ', 1)[-1].removesuffix(' ()')}.")


def _require_draft(rs: RuleSet) -> None:
    if RuleSet.objects.get(pk=rs.pk).status != RuleSet.Status.DRAFT:
        raise AccessDenied("A published rule set never changes; start a new version of it.")


@transaction.atomic
def new_version(actor, rs: RuleSet, effective_from: date, reason: str) -> RuleSet:
    """A draft copy of a rule set, to take effect on a later date: change its rules, then publish it."""
    _require_admin(actor)
    if not (reason or "").strip():
        raise RuleError("Say why the rules change: the reason is kept with the version.")
    if effective_from is None:
        raise RuleError("Give the date the new version takes effect.")
    newest = RuleSet.objects.filter(code=rs.code, status=RuleSet.Status.PUBLISHED).order_by("-effective_from").first()
    if newest and effective_from <= newest.effective_from:
        raise RuleError(f"The newest published version takes effect {newest.effective_from:%Y-%m-%d}; a new one takes effect after it.")
    if RuleSet.objects.filter(code=rs.code, effective_from=effective_from).exists():
        raise RuleError(f"There is already a version of these rules effective {effective_from:%Y-%m-%d}.")
    new = RuleSet.objects.create(
        code=rs.code,
        label=rs.label,
        effective_from=effective_from,
        created_by=actor,
        based_on=rs,
        reason=reason.strip(),
        source=f"Copied from the version effective {rs.effective_from:%Y-%m-%d}",
    )
    copied = [
        Rule(
            rule_set=new,
            **{f: getattr(r, f) for f in ("rule_id", "source", *WORDING, *TEST)},
        )
        for r in rs.rules.all()
    ]
    Rule.objects.bulk_create(copied)
    audit.record(
        "rule_set.version_started",
        obj=new,
        after={"code": rs.code, "effective_from": effective_from.isoformat(), "based_on": rs.pk, "rules": len(copied)},
        reason=reason,
        actor=actor,
    )
    return new


@transaction.atomic
def add_rule(
    actor,
    rs: RuleSet,
    *,
    kind: str,
    severity: str,
    table: str,
    field: str,
    definition: dict,
    message: str = "",
    fix: str = "",
    modules: list[str] | None = None,
    reason: str = "",
) -> Rule:
    """A new rule in a draft. Its ID is the content hash of its test; the wording is written from the test unless given."""
    _require_admin(actor)
    _require_draft(rs)
    field = (field or "").strip()
    if kind == "rule":
        definition = {**definition, "field": field}
    validate(kind, severity, table, field, definition, rs)
    rid, text = _id_and_text(kind, severity, table, field, definition)
    if rs.rules.filter(rule_id=rid).exists():
        raise RuleError(f"This set already has that rule ({rid}).")
    message = (message or "").strip() or canonical_text(text)
    r = Rule.objects.create(
        rule_set=rs,
        rule_id=rid,
        severity=severity,
        table=table,
        field=field,
        kind=kind,
        definition=definition,
        message=message,
        fix=(fix or "").strip() or suggested_fix(kind, canonical_field(field), definition, message),
        modules=modules if modules is not None else MODULES_FOR.get(rs.code, []),
        source="Added by an administrator",
    )
    audit.record("rule.added", obj=r, after=audit.snapshot(r, ["rule_id", *TEST, *WORDING]), reason=reason, actor=actor)
    return r


@transaction.atomic
def edit_rule(
    actor,
    rule: Rule,
    *,
    message: str | None = None,
    fix: str | None = None,
    difference: str | None = None,
    modules: list[str] | None = None,
    severity: str | None = None,
    table: str | None = None,
    field: str | None = None,
    kind: str | None = None,
    definition: dict | None = None,
    reason: str = "",
) -> Rule:
    """Change a draft rule: its wording and module notes, or its test (severity, table, field, definition). A changed test is a
    different rule, so it takes the new test's ID and records the one it replaces; its wording is rewritten from the test unless given."""
    _require_admin(actor)
    _require_draft(rule.rule_set)
    rule = Rule.objects.select_for_update().get(pk=rule.pk)
    fields = ["rule_id", "replaces", *TEST, *WORDING]
    before = audit.snapshot(rule, fields)
    test = {
        "severity": severity if severity is not None else rule.severity,
        "table": table if table is not None else rule.table,
        "field": (field if field is not None else rule.field).strip(),
        "kind": kind if kind is not None else rule.kind,
        "definition": definition if definition is not None else rule.definition,
    }
    if test["kind"] == "rule":
        test["definition"] = {**test["definition"], "field": test["field"]}
    if any(test[k] != getattr(rule, k) for k in TEST):
        validate(test["kind"], test["severity"], test["table"], test["field"], test["definition"], rule.rule_set)
        rid, text = _id_and_text(test["kind"], test["severity"], test["table"], test["field"], test["definition"])
        if rid != rule.rule_id and Rule.objects.filter(rule_set_id=rule.rule_set_id, rule_id=rid).exists():
            raise RuleError(f"This set already has that rule ({rid}).")
        if rid != rule.rule_id:
            rule.replaces = rule.replaces or rule.rule_id
            rule.rule_id = rid
        for k, v in test.items():
            setattr(rule, k, v)
        if message is None:
            message = canonical_text(text)
        if fix is None:
            fix = suggested_fix(rule.kind, canonical_field(rule.field), rule.definition, message)
        rule.source = rule.source if "administrator" in rule.source else f"{rule.source} (changed by an administrator)".strip()
    if message is not None and not message.strip():  # a blank message or fix is written from the test
        message = canonical_text(_id_and_text(rule.kind, rule.severity, rule.table, rule.field, rule.definition)[1])
    if fix is not None and not fix.strip():
        fix = suggested_fix(rule.kind, canonical_field(rule.field), rule.definition, message if message is not None else rule.message)
    for name, value in (("message", message), ("fix", fix), ("difference", difference), ("modules", modules)):
        if value is not None:
            setattr(rule, name, value.strip() if isinstance(value, str) else value)
    after = audit.snapshot(rule, fields)
    if after == before:
        return rule
    rule.save()
    audit.record("rule.edited", obj=rule, before=before, after=after, reason=reason, actor=actor)
    return rule


def _same_name(value, old: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    v = value.strip()
    if v.lower() == old.lower():
        return True
    try:
        return canonical_field(v).lower() == old.lower()  # a rule may name the field by its CET name
    except EngineMissing:
        return False


def renamed_test(rule_table: str, field: str, definition: dict, table: str, old: str, new: str) -> tuple[str, dict]:
    """A rule's field and definition with a field of `table` renamed from `old` to `new`: the rule's own field (each part of a key) and,
    in its definition, the field it tests (`field`), a field it is compared with (`value`), the columns of a key (`columns`) and the
    fields of its conditions on that table (`conds`: [table, field, operator, value]). A rule of another table changes only in its
    conditions on `table`. Nothing else is touched: a value list or an operator can share a field's name."""
    own = rule_table == table

    def name(value):
        return new if _same_name(value, old) else value

    d = dict(definition or {})
    if own:
        field = " + ".join(name(p.strip()) for p in field.split("+")) if field else field
        for k in ("field", "value"):
            if k in d:
                d[k] = name(d[k])
        if isinstance(d.get("columns"), list):
            d["columns"] = [name(c) for c in d["columns"]]

    def cond(c):
        if isinstance(c, dict):
            return {**c, "conds": [cond(x) for x in c.get("conds") or []]}
        if isinstance(c, list | tuple) and len(c) >= 4 and (c[0] == table or (own and not str(c[0]).strip())):
            return [c[0], name(c[1]), c[2], name(c[3]), *c[4:]]
        return c

    if isinstance(d.get("conds"), list):
        d["conds"] = [cond(c) for c in d["conds"]]
    return field, d


def renamed_words(text: str, old: str, new: str) -> str:
    return re.sub(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])", new, text or "")


@transaction.atomic
def rename_field(actor, rs: RuleSet, table: str, old: str, new: str, reason: str = "") -> int:
    """Rewrite a draft rule set's rules for a field renamed from `old` to `new` in `table` (`renamed_test`), their wording with them.
    A rewritten rule gets the ID of its new test and records the one it replaces, as any changed rule does; a comparison of versions
    shows it as the same rule under the field's new name. The number of rules rewritten."""
    _require_admin(actor)
    _require_draft(rs)
    n = 0
    for r in list(rs.rules.all()):
        field, definition = renamed_test(r.table, r.field, r.definition, table, old, new)
        if (field, definition) == (r.field, r.definition):
            continue
        edit_rule(actor, r, field=field, definition=definition, message=renamed_words(r.message, old, new),
                  fix=renamed_words(r.fix, old, new), reason=reason or f"{table}.{old} is renamed {new}")  # fmt: skip
        n += 1
    return n


@transaction.atomic
def remove_rule(actor, rule: Rule, reason: str = "") -> None:
    """Take a rule out of a draft (it stays in every published version that has it)."""
    _require_admin(actor)
    _require_draft(rule.rule_set)
    audit.record("rule.removed", obj=rule.rule_set, before=audit.snapshot(rule, ["rule_id", *TEST, *WORDING]), reason=reason, actor=actor)
    rule.delete()


@transaction.atomic
def discard_draft(actor, rs: RuleSet, reason: str = "") -> None:
    _require_admin(actor)
    _require_draft(rs)
    audit.record(
        "rule_set.discarded",
        obj=rs,
        before={"code": rs.code, "effective_from": rs.effective_from.isoformat(), "rules": rs.rules.count()},
        reason=reason,
        actor=actor,
    )
    rs.delete()


@transaction.atomic
def publish(actor, rs: RuleSet, *, with_specification: bool = False) -> RuleSet:
    """Publish a draft rule set. The rules of a draft filing specification version are published with that version
    (specifications.editor.publish passes `with_specification`), never on their own."""
    _require_admin(actor)
    rs = RuleSet.objects.select_for_update().get(pk=rs.pk)
    if rs.status != RuleSet.Status.DRAFT:
        raise AccessDenied("That rule set is already published.")
    if not with_specification and rs.specification_versions.filter(status="draft").exists():
        raise AccessDenied("These rules belong to a draft filing specification version: they are published with it, from its page.")
    rs.status, rs.published_by, rs.published_at = RuleSet.Status.PUBLISHED, actor, timezone.now()
    rs.save()
    audit.record("rule_set.published", obj=rs, after={"rules": rs.rules.count()}, actor=actor)
    return rs


def differences(a: RuleSet, b: RuleSet) -> dict[str, list[Rule]]:
    """Rules in one set and not the other (by stable ID), rules of `b` whose test changed from one of `a`'s (an administrator's edit
    records the ID it replaces), and rules in both whose module notes record a deliberate difference."""
    ids_a, ids_b = set(a.rules.values_list("rule_id", flat=True)), set(b.rules.values_list("rule_id", flat=True))
    changed = list(b.rules.filter(rule_id__in=ids_b - ids_a, replaces__in=ids_a - ids_b))
    replaced = {r.replaces for r in changed}
    return {
        "only_a": list(a.rules.filter(rule_id__in=ids_a - ids_b).exclude(rule_id__in=replaced)),
        "only_b": list(b.rules.filter(rule_id__in=ids_b - ids_a).exclude(pk__in=[r.pk for r in changed])),
        "changed": [(a.rules.get(rule_id=r.replaces), r) for r in changed],
        "noted": list(a.rules.filter(rule_id__in=ids_a & ids_b).exclude(difference="")),
    }


# --- running rules


@dataclass
class Finding:
    """One problem in one record, in the format every module reports: row, record, field, rule, severity, message, fix."""

    row: int  # the file's line number (header = line 1)
    record_id: str
    table: str
    field: str
    rule_id: str
    severity: str
    message: str
    fix: str
    value: str = ""


@dataclass
class CheckContext:
    """What the wider rule language reads beyond the table checked: the filing's other tables (raw text, by name), their key columns,
    who files ({PA, PAType}), the program records ({PrgID: {field: value}}, PROGRAM_FIELDS) and the day of the upload. Each is
    optional: a rule that reads one not given is noted as not checked, never failed."""

    tables: dict | None = None
    keys: dict | None = None
    filer: dict | None = None
    programs: list | None = None
    upload_date: date | None = None

    def engine(self, filing_rules, filing_year, library):
        import pandas as pd

        programs = None
        if self.programs is not None:
            programs = pd.DataFrame(self.programs, columns=list(PROGRAM_FIELDS)).astype("string").fillna("")
        return filing_rules.Context(
            filing_year=filing_year,
            library=library,
            upload_date=self.upload_date,
            filer=self.filer,
            programs=programs,
            tables=self.tables,
            keys=self.keys,
        )


def filer_record(organisation) -> dict:
    """Who files, as a condition on Filer reads it."""
    if organisation is None:
        return {}
    return {"PA": organisation.code, "PAType": getattr(organisation, "pa_type", "") or ""}


def program_records(prg_ids, on: date | None = None) -> list[dict]:
    """The registry's records of the programs named (PROGRAM_FIELDS, each program's version on `on`, today by default), as a condition
    on Program reads them. A program not in the registry has no record (its fields read as blank)."""
    from cedars.services.registry import service as registry
    from cedars.services.registry.models import Program

    day = on or timezone.localdate()
    out = []
    ids = {str(p).strip() for p in prg_ids if str(p or "").strip()}
    for p in Program.objects.filter(prg_id__in=ids).select_related("administrator"):
        v = registry.version_on(p, day) or registry.current_version(p)
        out.append(
            {
                "PrgID": p.prg_id,
                "PA": getattr(p.administrator, "code", "") if p.administrator_id else "",
                "Statewide": "1" if p.statewide else "0",
                "Name": v.name if v else "",
                "Status": v.status if v else "",
                "Segment": v.segment if v else "",
                "PrimarySector": v.primary_sector if v else "",
                "Category": v.category if v else "",
                "Implementer": v.implementer if v else "",
                "StartYear": str(v.start_year or "") if v else "",
                "EndYear": str(v.end_year or "") if v else "",
            }
        )
    return out


def reads(rs: RuleSet, what: str = "") -> bool:
    """Whether a rule set has a rule with a condition on `what` (PROGRAM, FILER or a table's name) that is not the rule's own table,
    so a caller fetches it only then; with no `what`, on anything beyond the rule's own record."""
    filing_rules, _ = _engine()
    for r in rs.rules.filter(kind="rule"):
        for c in filing_rules.conditions(r.definition or {}):
            tb = str(c[0]).strip().lower()
            if tb and tb != r.table.lower() and (not what or tb == what.lower()):
                return True
    return False


def _engine_rule_set(rs: RuleSet, skip=()):
    filing_rules, _ = _engine()
    rules = [
        filing_rules.Rule(r.rule_id, r.severity, r.table, r.field, r.message, r.kind, r.source, r.definition)
        for r in rs.rules.all()
        if r.rule_id not in skip
    ]
    return filing_rules.RuleSet(rs.code, rs.effective_from.isoformat(), None, rules)


def read_table(content: bytes):
    """An uploaded CSV as raw text (pipe- or comma-delimited, UTF-8 or Windows-1252), columns under the canonical names."""
    import pandas as pd

    _, fieldnames = _engine()
    text = content.decode("utf-8-sig", errors="strict") if _is_utf8(content) else content.decode("cp1252")
    header = text.split("\n", 1)[0]
    sep = "|" if "|" in header else ("\t" if "\t" in header else ",")
    df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    return fieldnames.to_recet(df)


def _is_utf8(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# The engine's note when the profile-combination rules cannot run, and what it means on this server.
_NO_LIBRARY = "the CEDARS impact-profile library is not installed"
_NO_LIBRARY_HERE = (
    "the combinations of end use, utility and sector are not checked here; they are checked against the impact-profile library "
    "when the job is priced"
)


def canonical_text(text: str) -> str:
    """A rule's wording with the data dictionary's field names: the first mention of a CET name becomes
    "AC_Climate_Zone (E3ClimateZone)", later mentions the canonical name alone. Idempotent."""
    import re

    _, fieldnames = _engine()
    for cet, ours in fieldnames.LEGACY_TO_RECET.items():
        if cet == ours or cet not in text:
            continue
        pattern = re.compile(rf"(?<![\w(]){re.escape(cet)}\b")
        first = True

        def swap(m, cet=cet, ours=ours):
            nonlocal first
            out = f"{ours} ({cet})" if first else ours
            first = False
            return out

        text = pattern.sub(swap, text)
    return text


def canonical_field(field: str) -> str:
    """A rule's field under the data dictionary's name (a two-column key keeps both names)."""
    _, fieldnames = _engine()
    return " + ".join(fieldnames.LEGACY_TO_RECET.get(part.strip(), part.strip()) for part in field.split("+")) if field else field


def library_available() -> bool:
    """Whether the engine on this server has the impact-profile library the profile-combination rules read."""
    try:
        from recet.pipeline import real_impact_profiles
    except ImportError:
        return False
    return bool(real_impact_profiles.is_available())


def spec_columns(rs: RuleSet, table: str) -> list[str]:
    """The columns a rule set's specification names for a table: every spec rule's field, a key rule's "PrgID + PrgYear" split into
    its columns (it names a two-column primary key, not a column)."""
    names: set[str] = set()
    for field in rs.rules.filter(table=table, kind="spec").values_list("field", flat=True):
        names.update(part.strip() for part in field.split("+") if part.strip())
    return sorted(names)


def _names_in(value) -> set[str]:
    """(UploadDate and FilingYear name no column.)"""
    return {n for n in _names_in_value(value) if n.lower() != "uploaddate"}


def _names_in_value(value) -> set[str]:
    """The column a rule's value names, if it names one: a bare word ("UnitkWh1stBaseline") or arithmetic on one ("EUL_Yrs /2.75");
    not a number, a quoted text, a list or FilingYear. A bare word that is not a column is harmless: it is only compared with the
    columns a file lacks."""
    import re

    v = str(value if value is not None else "").strip()
    if not v or v[0] in "['" or v.lower() == "filingyear":
        return set()
    m = re.match(r"^(\w+)\s*[*/+-]\s*[\d.]+$", v)
    if m:
        return {m.group(1)}
    try:
        float(v)
    except ValueError:
        return {v}
    return set()


def columns_needed(rule: Rule) -> set[str]:
    """Every column of its own table a rule reads: its field (both of a two-column key), a uniqueness rule's columns, its conditions'
    fields on its own table (in groups too) and the columns their values name. A condition on another table, the program or the filer
    reads no column of this one."""
    d = rule.definition or {}
    names = {p.strip() for p in (rule.field or "").split("+") if p.strip()}
    if rule.kind == "spec":
        names |= {str(c) for c in d.get("columns") or []}
        return names
    names |= {p.strip() for p in str(d.get("field", "")).split("+") if p.strip()}
    conds = [c for c in d.get("conds") or [] if not isinstance(c, dict)]
    conds += [x for c in d.get("conds") or [] if isinstance(c, dict) for x in c.get("conds") or []]
    for cond in conds:
        if len(cond) >= 4 and _own(str(cond[0]), rule.table):
            names.add(str(cond[1]).strip())
            names |= _names_in(cond[3])
    names |= _names_in(d.get("value"))
    return names


def needing(rs: RuleSet, table: str, missing) -> set[str]:
    """The IDs of the rule set's rules for `table` that read a column the file lacks (`missing`, by either of its names). The other
    rules can still run: a missing column would read as blank in every record and fail rules that are not the file's fault."""
    _, fieldnames = _engine()
    gone = {n.lower() for c in missing for n in (c, fieldnames.recet_name(c))}
    if not gone:
        return set()
    return {
        r.rule_id
        for r in rs.rules.filter(table=table)
        if any(n.lower() in gone or fieldnames.recet_name(n).lower() in gone for n in columns_needed(r))
    }


def missing_columns_note(table: str, missing: int, skipped: int, spec: str) -> str:
    """What a filing page says when a table lacks some of the specification's columns: the rules that read them were not run, the
    others were, and more findings may come once the columns are added."""
    cols = f"{missing:,} of the {spec}'s column{'s' if missing != 1 else ''}"
    if not skipped:
        return f"The {table} file lacks {cols}; every {table} rule ran, since none reads them."
    return (
        f"Some {table} rules were not run: the file lacks {cols}, and {skipped:,} rule{'s read' if skipped != 1 else ' reads'} "
        f"them. The other {table} rules ran. More findings may appear once the columns are there."
    )


def check(
    rs: RuleSet,
    table: str,
    df,
    *,
    filing_year: int | None = None,
    id_column: str | list[str] = "CEInputID",
    skip=(),
    context: CheckContext | None = None,
) -> tuple[list[Finding], list[str]]:
    """Run a rule set's rules for `table` on a raw-text frame: every failing (record, rule) as a Finding, rejects first, and notes on rules
    that could not be checked. Findings name fields and wording with the data dictionary's names. `skip`: rule IDs not to run (those
    that read a column the file lacks, `needing`). `context`: the filing's other tables, who files, the programs and the upload date,
    which the wider rule language reads. `id_column`: the column (or key columns) that names a record in the findings.

    Inside an engine run (jobs.engine_run) the rules are run by the worker of the run's engine release as a check job: a run that waits
    gets the job's findings (or CheckNotRun, a ValueError, when it could not have them); a run that does not wait gets no findings
    while the job is pending (its `pending` lists it). Otherwise, or when the release's worker cannot run checks, they run here."""
    run = jobs.current()
    if run is not None and run.in_worker:
        out = jobs.check_part(run, rs, table, df, filing_year=filing_year, skip=skip, context=context)
        if out is None:
            return [], []
        failing = [(r["rule_id"], r["rows"], r.get("actual") or []) for r in out.get("results", [])]
        notes = list(out.get("notes", []))
    else:
        from cedars.services.dictionary import value_lists

        filing_rules, _ = _engine()
        value_lists.apply_to_engine()  # the value lists in force here, as administrators keep them
        ctx = (context or CheckContext()).engine(filing_rules, filing_year, library_available())
        results, notes = filing_rules.check_frame(_engine_rule_set(rs, skip), table, df, ctx)
        failing = []
        for res in results:
            rows = [int(i) for i in res.fail.nonzero()[0]]
            actual = res.actual
            failing.append(
                (res.rule.id, rows, ["" if actual is None else str(actual.iloc[i]) if hasattr(actual, "iloc") else "" for i in rows])
            )
        if run is not None and run.note:
            notes = [*notes, run.note]
    notes = [n.replace(_NO_LIBRARY, _NO_LIBRARY_HERE) for n in notes]
    return _findings(rs, table, df, failing, id_column), notes


def _findings(rs: RuleSet, table: str, df, failing, id_column) -> list[Finding]:
    """The failing (rule ID, 0-based rows, the field's value in each) as Findings, rejects first."""
    rules = {r.rule_id: r for r in rs.rules.filter(table=table)}
    id_cols = [id_column] if isinstance(id_column, str) else list(id_column)
    if all(c in df.columns for c in id_cols) and id_cols:
        ids = df[id_cols].astype(str).agg(" / ".join, axis=1).to_numpy()
    else:
        ids = [""] * len(df)
    findings = []
    for rule_id, rows, values in sorted(failing, key=lambda f: getattr(rules.get(f[0]), "severity", "reject") != "reject"):
        r = rules.get(rule_id)
        if r is None:
            continue
        for n, i in enumerate(rows):
            value = str(values[n]) if n < len(values) and values[n] is not None else ""
            value = "" if value == "<NA>" else value
            findings.append(
                Finding(
                    int(i) + 2,
                    ids[i],
                    table,
                    canonical_field(r.field),
                    r.rule_id,
                    r.severity,
                    canonical_text(r.message) + (f", but is {value}" if value else ""),
                    r.fix,
                    value,
                )
            )
    return findings


def condition_met(rule: Rule, df, *, filing_year: int | None = None, context: CheckContext | None = None) -> bool:
    """Whether a conditional rule's condition holds on any record of a raw-text frame (an example file shows the rule only then).
    A condition on a column the frame lacks, or on something the context does not give, never holds; a rule with no condition always
    applies."""
    filing_rules, _ = _engine()
    d = rule.definition or {}
    conds = d.get("conds") or []
    if not conds or df is None or not len(df):
        return bool(not conds and df is not None and len(df))
    ctx = (context or CheckContext()).engine(filing_rules, filing_year, library_available())
    try:
        return bool(filing_rules.applies(d, rule.table, df, ctx).any())
    except (KeyError, filing_rules.NotCheckable):
        return False


def by_rule(problems, limit: int = 20) -> list[dict]:
    """An upload's findings summed by rule, rejects first, then the rules that touch the most: each rule's records (a column rule's
    columns; a record a rule flags twice counts once, UAT 2 U08), its severity and one message for example. The unit agrees with the
    count ("1 record", "9 records")."""
    touched: dict[str, set] = {}
    seen: dict[str, tuple[str, str]] = {}
    for p in problems:
        column = p.rule_id.endswith("-COL")
        touched.setdefault(p.rule_id, set()).add((p.table, p.field) if column else (p.table, p.row, p.record_id))
        seen[p.rule_id] = (p.severity, p.message)
    top = sorted(touched, key=lambda k: (seen[k][0] != "reject", -len(touched[k]), k))[:limit]
    out = []
    for k in top:
        n, noun = len(touched[k]), "column" if k.endswith("-COL") else "record"
        out.append({"rule_id": k, "count": n, "severity": seen[k][0], "example": seen[k][1], "unit": noun if n == 1 else noun + "s"})
    return out


def findings_csv(findings: list[Finding]) -> str:
    import csv

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["line", "record_id", "table", "field", "rule_id", "severity", "message", "suggested_fix"])
    for f in findings:
        w.writerow([f.row, f.record_id, f.table, f.field, f.rule_id, f.severity, f.message, f.fix])
    return out.getvalue()
