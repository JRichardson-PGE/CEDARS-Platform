"""The pre-upload checker (BR-03.4): the rule-set step of a module's validation, run on one table before it is filed, by the page
(/rules/check/), the API (POST /api/v1/rules/check), the `check_file` command and, for those who cannot send a file, as a download
(the rule set as JSON with the check specification, `rule_set_json`).

Each filing module registers the function its own validation runs (`register_checker`), so the checker and production validation are
the same code: Claims reads the table as its upload does (canonical names, unknown columns dropped, ClaimID as the record key) and
checks it against the claims rule set with the claim year as the filing year; Budgets checks it year by year, each year as its own
filing year, as a cycle filing is. Both reject a table that lacks the specification's columns before running its rules. What the
checker leaves to the upload are the module's own checks that need the whole filing (other tables, earlier confirmations, eTRM's deemed
values filled into a deemed claim's blanks, the program registry for the PA)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from .models import RuleSet

TABLES = ("Measure", "ProgramCost")


class CheckError(ValueError):
    """The request cannot be checked as asked (an unknown module, a version of another module's rules, a file that cannot be read)."""


@dataclass(frozen=True)
class Checker:
    """A module's rule-set step. `run(rule_set, table, content, filing_year)` -> (findings, notes, records), exactly as the module's
    validation runs it on that table."""

    module: str
    code: str  # the rule set it checks against
    label: str
    run: Callable
    year_help: str = ""
    tables: tuple = TABLES


_checkers: dict[str, Checker] = {}


def register_checker(module: str, code: str, label: str, run: Callable, *, year_help: str = "", tables: tuple = TABLES) -> None:
    """Called from a module's AppConfig.ready (a service never imports a module)."""
    _checkers[module] = Checker(module, code, label, run, year_help, tables)


def checkers() -> list[Checker]:
    return sorted(_checkers.values(), key=lambda c: c.label)


def checker_for(module: str | None = None, code: str | None = None) -> Checker | None:
    if module:
        return _checkers.get(module)
    return next((c for c in _checkers.values() if c.code == code), None)


def versions() -> list[RuleSet]:
    """Every published rule-set version a file can be checked against (the one in force, earlier ones and ones that take effect later)."""
    return list(RuleSet.objects.filter(status=RuleSet.Status.PUBLISHED, code__in=[c.code for c in _checkers.values()]))


@dataclass
class Result:
    checker: Checker
    rule_set: RuleSet
    table: str
    filing_year: int | None
    records: int
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def rejected(self) -> int:
        return len({f.record_id or f.row for f in self.findings if f.severity == "reject"})

    @property
    def warned(self) -> int:
        return len({f.record_id or f.row for f in self.findings if f.severity == "warning"})


def resolve(module: str | None, rule_set: RuleSet | None) -> tuple[Checker, RuleSet]:
    """The module and the version to check against: the version named (it must be published, and the module's rules), else the module's
    version in force today."""
    from . import service

    if rule_set is not None:
        c = checker_for(code=rule_set.code)
        if c is None:
            raise CheckError(f"No module files under the {rule_set.code} rules.")
        if module and module != c.module:
            raise CheckError(f"{rule_set} are the {c.label} rules, not the {module} rules.")
        if rule_set.status != RuleSet.Status.PUBLISHED:
            raise CheckError(f"{rule_set} is a draft; files are checked against published rules only.")
        return c, rule_set
    c = checker_for(module=module)
    if c is None:
        known = ", ".join(sorted(_checkers))
        raise CheckError(f"Choose the module to check for: {known}." if not module else f"No module {module!r}; choose one of {known}.")
    rs = service.in_force(c.code)
    if rs is None:
        raise CheckError(f"No {c.label} rule set is in force yet.")
    return c, rs


def check(module: str | None, table: str, content: bytes, *, rule_set: RuleSet | None = None, filing_year: int | None = None) -> Result:
    """Run a module's rule-set step on one table's CSV. Nothing is kept."""
    c, rs = resolve(module, rule_set)
    if table not in c.tables:
        raise CheckError(f"{c.label} checks the {' or '.join(c.tables)} table, not {table!r}.")
    found, notes, records = c.run(rs, table, content, filing_year)
    return Result(c, rs, table, filing_year, records, found, notes)


YEAR_COLUMN = {"Measure": "ClaimYearQuarter", "ProgramCost": "PrgYear"}


def year_of(values) -> int | None:
    """The one year a table's year column holds (ClaimYearQuarter's first four characters, or PrgYear), or None when it holds several."""
    years = {str(v).strip()[:4] for v in values if str(v).strip()}
    year = next(iter(years)) if len(years) == 1 else ""
    return int(year) if year.isdigit() else None


# --- the downloadable form: the rule set as data with the specification of how each rule is checked

FORMAT, FORMAT_VERSION = "cedars-rule-set", 1

CHECK_SPEC = {
    "reading_the_file": [
        "One table per file: Measure or ProgramCost, its first line the column names. Pipe-, tab- or comma-delimited (the first line "
        "decides), UTF-8 or Windows-1252.",
        "Column names are matched without regard to case, under the data dictionary's names or the CET names it lists (/dictionary/).",
        "Every value is text: surrounding spaces are removed, and an empty value is blank.",
        "Line numbers count the header as line 1, so the first record is line 2.",
    ],
    "columns": "A table that lacks any column in its `tables.<table>.columns` list is rejected on those columns (rule <module>-COL), and "
    "its rules are not run until the columns are there; a column may be blank.",
    "filing_year": {
        "claims": "The claim year: every record is checked with it as FilingYear.",
        "budgets": "Each record's own year (ProgramCost PrgYear, Measure ClaimYearQuarter's first four characters): a cycle filing is "
        "checked year by year, each year as its FilingYear.",
    },
    "years": "Value lists are read for each record's year: Measure ClaimYearQuarter's first four characters, ProgramCost PrgYear.",
    "kinds": {
        "spec": "A field constraint of the specification. `definition.check` says which; every spec rule rejects the record.",
        "rule": "A validation rule (severity reject) or warning rule (severity warning): a test on one field, applied when its "
        "conditions hold.",
    },
    "spec_checks": {
        "not_empty": "The field is filled in. A table without the column fails it on every record.",
        "max_length": "The value has at most `n` characters. Blanks pass.",
        "numeric": "The value is a plain number (no text, units, commas or currency symbols). Blanks pass.",
        "bit": "The value is 0, 1, True or False, in any case. Blanks pass.",
        "year_quarter": "The value is a year and quarter such as 2028Q1 (four digits, Q, 1-4). Blanks pass.",
        "range": "The number is at least `lo` (more than it when `lo_open`) and at most `hi` (less than it when `hi_open`); a "
        "missing bound is no bound. Blanks and non-numbers pass (numeric checks them).",
        "value_list": "The value is a code of value list `list` (in `value_lists`, ignoring case) in force in the record's year: "
        "start_year blank or at most the year, end_year blank or at least it (a retired code keeps its row with its end_year). A record "
        "without a year accepts a code in force in any year. List PrgID is the program registry (/programs/). Blanks pass.",
        "starts_with": "The value starts with a PA code (value list pa), a dash and the filing year, e.g. PGE-2028 (ignoring case). "
        "Blanks pass.",
        "unique": "The combination of `columns` appears on one record only; every record sharing a combination fails.",
    },
    "rule_tests": {
        "form": "`definition` = {field, rule, value, connector, conds}. The rule applies to a record when its conditions hold (`conds`, "
        "each [table, field, rule, value]: all of them when connector is all, any when it is any; no conditions = every record). An "
        "applying record fails when `field rule value` does not hold.",
        "values": '`value` is a number; a quoted text (\'Deemed\'); a list (["a", "b"]); FilingYear; another column of the table; or '
        "a column with arithmetic on a number (EUL_Yrs /2.75). Anything else is text.",
        "comparisons": "equals, does not equal, is less than, is less than or equal to, is greater than (or greater than), is greater "
        "than or equal to (or greater than or equal to), less than: numeric when the value is a number (or a column that holds numbers), "
        "else text compared without regard to case. in / not in: the value, ignoring case, is (is not) in the list. is null / is not "
        "null: blank / filled in. starts with string / ends with string: ignoring case. matches_year_quarter: the field equals the first "
        "four characters of the column named in value. combo_value_list: the combination of the fields joined by + is an impact "
        "profile in the CEDARS impact-profile library (checked on CEDARS only).",
        "blanks": "A comparison with a blank on either side is unknown: as a condition it does not hold, and as the test it fails "
        "(except is null and is not null, which test the blank itself).",
    },
    "findings": "One finding per failing record and rule: line, record_id (Measure CEInputID or ClaimID, ProgramCost PrgID), table, "
    'field, rule_id, severity, message (the rule\'s message, plus ", but is <value>" when the value is filled in) and suggested_fix. '
    "A record is rejected when any finding on it is a reject.",
    "not_in_this_file": "The module's own checks that need the whole filing run on upload: other tables of the filing, earlier "
    "confirmations, eTRM's values for deemed claims, the program registry. The checker on CEDARS (page or API) gives the exact findings.",
}


def rule_set_json(rs: RuleSet, base_url: str = "") -> dict:
    """A rule set as data, with what a system needs to apply it without CEDARS' code: each rule's test, the value lists in force for
    its value-list rules, the specification's columns and how each kind of rule is checked (CHECK_SPEC)."""
    from . import service

    c = checker_for(code=rs.code)
    rules = list(rs.rules.all())
    in_force = service.in_force(rs.code)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "rule_set": {
            "id": rs.pk,
            "code": rs.code,
            "label": rs.label,
            "module": c.module if c else None,
            "effective_from": rs.effective_from.isoformat(),
            "status": rs.status,
            "in_force": in_force is not None and in_force.pk == rs.pk,
            "source": rs.source,
            "published_at": rs.published_at.isoformat() if rs.published_at else None,
            "url": f"{base_url}/rules/{rs.pk}/",
        },
        "made_at": date.today().isoformat(),
        "check_on_cedars": {
            "page": f"{base_url}/rules/check/",
            "api": f"{base_url}/api/v1/rules/check",
        },
        "tables": {
            t: {
                "id_column": "PrgID" if t == "ProgramCost" else ("ClaimID or CEInputID" if rs.code == "claims" else "CEInputID"),
                "columns": service.spec_columns(rs, t),
            }
            for t in TABLES
            if any(r.table == t for r in rules)
        },
        "how_to_check": CHECK_SPEC,
        "value_lists": value_lists_for(rules),
        "rules": [
            {
                "rule_id": r.rule_id,
                "severity": r.severity,
                "table": r.table,
                "field": r.field,
                "kind": r.kind,
                "definition": r.definition,
                "message": r.message,
                "suggested_fix": r.fix,
            }
            for r in rules
        ],
    }


def value_lists_for(rules) -> dict[str, list[dict]]:
    """The codes of every value list the rules read, as the engine holds them with the lists in force here (BR-12.6): {list: [{code,
    description, start_year, end_year}]}. PrgID (the program registry) and lists the engine does not ship are left out."""
    try:
        from recet import editor, filing_rules
    except ImportError:
        return {}
    from cedars.services.dictionary import value_lists

    value_lists.apply_to_engine()
    names = {r.definition.get("list", "") for r in rules if r.kind == "spec" and r.definition.get("check") == "value_list"}
    if any(r.kind == "spec" and r.definition.get("check") == "starts_with" for r in rules):
        names.add("pa")
    out = {}
    for name in sorted(n for n in names if n and n.lower() != "prgid"):
        try:
            df = editor.value_list(filing_rules.VALUE_LIST_FILES.get(name.lower(), name.lower()))
        except (FileNotFoundError, KeyError, ValueError):
            continue
        out[name] = [
            {
                "code": str(row.get("code", "")).strip(),
                "description": "" if _blank(row.get("description")) else str(row.get("description")),
                "start_year": None if _blank(row.get("start_year")) else int(row.get("start_year")),
                "end_year": None if _blank(row.get("end_year")) else int(row.get("end_year")),
            }
            for row in df.to_dict("records")
        ]
    return out


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or str(v).strip() in ("", "<NA>", "nan")
