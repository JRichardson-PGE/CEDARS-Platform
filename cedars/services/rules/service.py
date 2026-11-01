"""Rule sets as data, run by the engine's rule runner (recet.filing_rules.check_frame) so the platform, ReCET runs and the pre-upload
checker apply exactly the same rules. The engine is imported only when rules are run or imported from it (engine/README.md)."""

import io
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

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
    """A plain-language fix for a finding, from the kind of rule (BR-03.2: every finding says how to fix it)."""
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


TABLES = ["Measure", "ProgramCost"]
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
]
# the engine's other spellings, which CEDARS' own rule files use; accepted on a rule copied from them, not offered on the form
ALSO_ACCEPTED = {"greater than": "is greater than", "greater than or equal to": "is greater than or equal to", "less than": "is less than"}
NO_VALUE = {"is not null", "is null"}
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


def rule_definition(field: str, rule: str, value="", conditions=(), connector: str = "all", table: str = "") -> dict:
    """A validation or warning rule's definition as the engine reads it (the rule files' Field, Rule, Value and up to five Where
    conditions): `conditions` is a list of (field, rule, value); the test applies to a record only where they hold (all, or any)."""
    rule = (rule or "").strip().lower()
    conds = []
    for f, r, v in conditions:
        if (f or "").strip():
            r = (r or "").strip().lower()
            conds.append([table, f.strip(), r, _value_text(r, v)])  # CEDARS' files name the table on each condition
    return {
        "field": (field or "").strip(),
        "rule": rule,
        "value": _value_text(rule, value),
        "connector": "any" if connector == "any" else "all",
        "conds": conds,
    }


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
    conds = [tuple(c) for c in d["conds"]]
    rid = filing_rules._rule_id(
        "V" if severity == "reject" else "W",
        table,
        field,
        d["rule"],
        d["value"],
        d["connector"] if len(conds) > 1 else "",
        *(x for c in sorted(conds) for x in c),
    )
    return rid, filing_rules._describe(d)


def _known_field(name: str) -> bool:
    from cedars.services.dictionary import service as dictionary

    return dictionary.current() is None or dictionary.resolve(name) is not None


def validate(kind: str, severity: str, table: str, field: str, d: dict) -> None:
    """Refuse a rule the engine cannot run: an unknown table, field, operator, value list or value. The rule is run once on a blank
    record, so a rule saved here is one the checker runs."""
    filing_rules, fieldnames = _engine()
    if severity not in Rule.Severity.values:
        raise RuleError("Choose whether the rule rejects the record or gives a warning.")
    if table not in TABLES:
        raise RuleError(f"Choose the table: {', '.join(TABLES)}.")
    if not field:
        raise RuleError("Give the field the rule tests.")
    named = [field] if kind == "spec" else [field] + [c[1] for c in d["conds"]]
    fields = [part.strip() for f in named for part in f.split("+") if part.strip()]  # a key or combination names several
    unknown = [f for f in fields if not _known_field(f)]
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
        for r in [d["rule"]] + [c[2] for c in d["conds"]]:
            if r not in ops:
                raise RuleError(f"{r or '(blank)'} is not a test the checker runs.")
        for r, v in [(d["rule"], d["value"])] + [(c[2], c[3]) for c in d["conds"]]:
            if r not in NO_VALUE and not v:
                raise RuleError(f'Give the value for "{ops[r]}".')
    columns = {fieldnames.recet_name(f) for f in fields}
    if kind == "rule":
        for v in [d["value"]] + [c[3] for c in d["conds"]]:
            head = str(v).split()[0] if str(v).split() else ""
            if head and head.isidentifier() and _known_field(head) and head.lower() not in ("filingyear",):
                columns.add(fieldnames.recet_name(head))
    import pandas as pd

    blank = pd.DataFrame({c: [""] for c in columns})
    probe = filing_rules.RuleSet("check", "0000-00-00", None, [filing_rules.Rule("X", severity, table, field, "", kind, "", d)])
    try:
        filing_rules.check_frame(probe, table, blank, filing_rules.Context(filing_year=2028))
    except Exception as e:  # noqa: BLE001 - whatever the engine says is the reason the rule cannot be saved
        raise RuleError(f"The checker cannot run this rule: {e}") from e


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
    validate(kind, severity, table, field, definition)
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
        validate(test["kind"], test["severity"], test["table"], test["field"], test["definition"])
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
def publish(actor, rs: RuleSet) -> RuleSet:
    _require_admin(actor)
    rs = RuleSet.objects.select_for_update().get(pk=rs.pk)
    if rs.status != RuleSet.Status.DRAFT:
        raise AccessDenied("That rule set is already published.")
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


def _engine_rule_set(rs: RuleSet):
    filing_rules, _ = _engine()
    rules = [filing_rules.Rule(r.rule_id, r.severity, r.table, r.field, r.message, r.kind, r.source, r.definition) for r in rs.rules.all()]
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
    "the impact-profile library is not in the engine's reference folder on this server (RECET_DATA for the web service); the "
    "profile-combination rules are checked when the job is priced"
)


def canonical_text(text: str) -> str:
    """A rule's wording with the data dictionary's field names (decision L): the first mention of a CET name becomes
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


def check(rs: RuleSet, table: str, df, *, filing_year: int | None = None, id_column: str = "CEInputID") -> tuple[list[Finding], list[str]]:
    """Run a rule set's rules for `table` on a raw-text frame: every failing (record, rule) as a Finding, rejects first, and notes on rules
    that could not be checked. Findings name fields and wording with the data dictionary's names."""
    from cedars.services.dictionary import value_lists

    filing_rules, _ = _engine()
    value_lists.apply_to_engine()  # the value lists in force here, as administrators keep them (BR-12.6)
    ctx = filing_rules.Context(filing_year=filing_year, library=library_available())
    results, notes = filing_rules.check_frame(_engine_rule_set(rs), table, df, ctx)
    notes = [n.replace(_NO_LIBRARY, _NO_LIBRARY_HERE) for n in notes]
    fixes = dict(rs.rules.values_list("rule_id", "fix"))
    ids = df[id_column].astype(str).to_numpy() if id_column in df.columns else [""] * len(df)
    findings = []
    for res in sorted(results, key=lambda r: r.rule.severity != "reject"):
        actual = res.actual
        for i in res.fail.nonzero()[0]:
            value = "" if actual is None else str(actual.iloc[i]) if hasattr(actual, "iloc") else ""
            findings.append(
                Finding(
                    int(i) + 2,
                    ids[i],
                    table,
                    canonical_field(res.rule.field),
                    res.rule.id,
                    res.rule.severity,
                    canonical_text(res.rule.text) + (f", but is {value}" if value and value != "<NA>" else ""),
                    fixes.get(res.rule.id, ""),
                    "" if value == "<NA>" else value,
                )
            )
    return findings, notes


def findings_csv(findings: list[Finding]) -> str:
    import csv

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["line", "record_id", "table", "field", "rule_id", "severity", "message", "suggested_fix"])
    for f in findings:
        w.writerow([f.row, f.record_id, f.table, f.field, f.rule_id, f.severity, f.message, f.fix])
    return out.getvalue()
