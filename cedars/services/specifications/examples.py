"""A version's example files: named sets of example records (`ExampleSet`), checked by the same checks a filing gets, which a version
needs passing before it is published.

CEDARS writes the first set itself (`generate`): one record per table (the PII vault's tables aside: personal information is never in an
example), each field filled with its example value or default where valid, else (a required field) the module's example value or a
value of its type, an optional one left blank; a coded field with a code on its value list accepted in the example year; IDs with the
example PA's prefix, and one value for each key field in every table, so the keys link the tables. A draft copied from a version
with no example set gets one, and a draft's sets follow its fields (`fit`: a new column filled the generated way, a removed one
dropped, a renamed one moved), each time the sets are checked.

Each filing module registers how its checks run on example records (`register_example_checker`): its own checks and the rule set,
as a filing by the example PA in the example year, without the reference data only a real filing has (the program registry, eTRM, the
year so far). The report (`check`) has three parts: what the example fails (field checks, the module's checks and the rules), with the
record and the reason; the conditional rules whose condition no record meets, which the example does not demonstrate; and the built-in
checks that need real reference data, which an example cannot pass and does not count against it.

Staff edit a set in its workbook (or .zip of delimited files) and load it back (`read_file`, `replace_rows`); a file with anything
that looks like personal information is refused (the publication scan's patterns)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.utils import timezone

from cedars.services import audit

from . import service
from .models import ExampleSet

EXAMPLE_PA = "PGE"
GENERATED = "generated"
GENERATED_ABOUT = (
    "One record per table, written by CEDARS from the version's fields: each field's example value or a value of its type, codes from "
    "its value list, and IDs that link the tables."
)
MAX_ROWS = 200  # an example shows the layout: a few records per table, not a filing
TYPE_VALUES = {"number": "1", "integer": "1", "flag": "0"}


class ExampleError(ValueError):
    """An example file or change that cannot be taken; the message says why."""


def example_year() -> int:
    return timezone.localdate().year


# --- the modules' checks


@dataclass(frozen=True)
class Checker:
    """How a module checks example records: `run(version, rows, year, pa)` gives findings as dicts (row, record_id, table, field,
    rule_id, severity, message, fix); `reference` names the module's built-in checks that need real reference data; `values` are
    example values its own checks accept ({field: value}), taken where the version gives the field no example or default."""

    run: object
    reference: frozenset[str]
    values: dict


_checkers: dict[str, Checker] = {}


def register_example_checker(module: str, run, *, reference=(), values=None) -> None:
    """Called by a filing module in its AppConfig.ready(): how its checks run on a version's example records, which of its built-in
    checks need reference data an example does not have, and example values its checks accept for the fields they read."""
    _checkers[module] = Checker(run, frozenset(reference), dict(values or {}))


# --- generating


def _codes(name: str, version, year: int) -> list[str]:
    """The codes of value list `name` accepted in `year`, as an example may use them: a list made in this draft from its draft, any
    other from the version in force here or the list as it comes."""
    from cedars.services.dictionary import value_lists
    from cedars.services.dictionary.models import ValueList

    vl = ValueList.objects.filter(name__iexact=name).first()
    rows = None
    if vl is not None:
        v = value_lists.in_force(vl) or value_lists.latest_published(vl)
        if v is None and vl.made_for:
            v = value_lists.draft_of(vl)
        if v is not None:
            rows = [{"code": c.code, "start_year": c.start_year, "end_year": c.end_year} for c in v.codes.order_by("pk")]
    if rows is None:
        try:
            rows = value_lists.shipped(name)
        except (value_lists.EngineMissing, value_lists.ValueListError):
            rows = []
    return [r["code"] for r in rows if value_lists.accepts([r], r["code"], year)]


def _id(name: str, year: int, n: int) -> str:
    stem = name[:-2] if name.lower().endswith("id") and len(name) > 2 else name
    return f"{EXAMPLE_PA}-{year}-{stem}-{n:04d}"


def value_for(f, version, year: int, n: int = 1, *, fill: bool = False) -> str:
    """A valid value for field `f` (a FieldLayout or SpecField) in record `n` of an example for `year`. An optional field is blank
    unless it has an example or default, or `fill` (a field that links tables) asks for a value."""
    if f.requirement == "system":
        return ""  # CEDARS fills it in
    codes = _codes(f.value_list, version, year) if f.value_list and f.value_list.lower() != "prgid" else None
    checker = _checkers.get(version.specification.module)
    hint = checker.values.get(f.name, "") if checker is not None else ""
    for given in (f.example, f.default, hint if f.required or fill else ""):
        if given and not service.value_problem(f, given) and (codes is None or given.lower() in {c.lower() for c in codes}):
            return given
    if f.name.upper() == "PA":
        return EXAMPLE_PA
    if not f.required and not fill:
        return ""  # an optional field is left blank unless the version gives it an example or a default
    if f.value_list and f.value_list.lower() == "prgid" or f.name == "PrgID":
        value = f"{EXAMPLE_PA}_Example_Program"  # the program registry is reference data
    elif codes:
        value = codes[0]
    elif f.type in ("text", "number", "integer") and f.name.lower().endswith("year"):
        value = str(year)
    elif f.type in TYPE_VALUES:
        value = TYPE_VALUES[f.type]
    elif f.type == "date":
        value = f"{year}-01-15"
    elif f.type == "year_quarter":
        value = f"{year}Q1"
    elif f.type == "year_month":
        value = f"{year}01"
    elif f.type == "phone":
        value = "4155550100"
    elif f.type == "email":
        value = "someone@example.org"
    elif f.name.lower().endswith("id"):
        value = _id(f.name, year, n)
    else:
        value = "Example"
    if f.max_length and len(value) > f.max_length:
        value = value[: f.max_length]
    return value


def _tables(version):
    return [t for t in service.layout(version).tables if not t.vault]


def generate_rows(version, year: int | None = None) -> dict[str, list[dict]]:
    """One example record per table (not the vault's), the same value for a field of the same name in every table."""
    year = year or example_year()
    tables = _tables(version)
    links = {k for t in tables for k in t.keys}  # a key of any table links the tables: the same value in each
    shared: dict[str, str] = {}
    out = {}
    for t in tables:
        row = {}
        for f in t.fields:
            if f.requirement == "system":
                row[f.name] = ""
            elif f.name in links:
                if f.name not in shared:
                    shared[f.name] = value_for(f, version, year, fill=True)
                row[f.name] = shared[f.name]
            else:
                row[f.name] = value_for(f, version, year)
        out[t.name] = [row]
    return out


@transaction.atomic
def generate(actor, version, reason: str = "") -> ExampleSet:
    """Write (or write again) the set CEDARS generates for a draft, and check it."""
    from .editor import _draft, _require

    _require(actor)
    v = _draft(version)
    rows = generate_rows(v)
    e = v.examples.filter(slug=GENERATED).first()
    if e is None:
        position = (max((x.position for x in v.examples.all()), default=-1)) + 1
        e = ExampleSet.objects.create(
            version=v,
            position=position,
            name="Generated by CEDARS",
            slug=GENERATED,
            about=GENERATED_ABOUT,
            rows=rows,
            generated=True,
        )
    else:
        e.rows, e.report, e.checked = rows, {}, ""
        e.save(update_fields=["rows", "report", "checked"])
    audit.record("specification.example_generated", obj=v, after={"example": e.slug, "tables": len(rows)}, reason=reason, actor=actor)
    check_all(v)
    e.refresh_from_db()
    return e


# --- following the draft's fields


def fit(version, rows: dict[str, list[dict]], year: int | None = None) -> dict[str, list[dict]]:
    """`rows` laid out on the version's tables: a column under another accepted name (a renamed field's old one) moved to the field, a
    column the table no longer has dropped, a new field filled the generated way. A table the version no longer has is dropped."""
    year = year or example_year()
    out = {}
    for t in _tables(version):
        if t.name not in rows:
            continue
        fitted = []
        for n, r in enumerate(rows[t.name], start=1):
            row = {}
            for k, val in r.items():
                name = t.canonical(k)
                if name is not None and name not in row:
                    row[name] = val
            for f in t.fields:
                if f.name not in row:
                    row[f.name] = value_for(f, version, year, n)
            fitted.append({f.name: row[f.name] for f in t.fields})
        out[t.name] = fitted
    return out


# --- checking


def state(version) -> str:
    """What a check depends on: the version as a document (its fields, checks and examples) and its rule set's rules."""
    doc = service.as_document(version)
    rules = (
        sorted(version.rule_set.rules.values_list("rule_id", "severity", "table", "field", "definition"), key=lambda r: r[0])
        if version.rule_set_id
        else []
    )
    return hashlib.sha256(json.dumps([doc, rules], sort_keys=True, default=str).encode()).hexdigest()


def made_lists(version, year: int) -> dict[str, set[str]]:
    """{list name, lower case: its codes accepted in `year`, lower case} for each value list made in a draft and not yet published: the
    filing modules' own check (`value_lists.codes_for_check`) does not have it yet, so the example's and the impact test's checks do."""
    from cedars.services.dictionary import value_lists
    from cedars.services.dictionary.models import ValueList

    return {
        vl.name.lower(): {c.lower() for c in _codes(vl.name, version, year)}
        for vl in ValueList.objects.exclude(made_for="")
        if value_lists.latest_published(vl) is None
    }


def made_list_findings(version, rows, year: int, rule_id: str = "FIELD") -> list[dict]:
    """A value not a code of the unpublished value list (made in a draft) its field names, accepted in `year`."""
    made = made_lists(version, year)
    if not made:
        return []
    out = []
    for t in _tables(version):
        coded = [f for f in t.fields if f.value_list and f.value_list.lower() in made]
        for i, r in enumerate(rows.get(t.name, []), start=2):
            rid = " / ".join(str(r.get(k, "")) for k in t.keys)
            for f in coded:
                value = str(r.get(f.name, "") or "").strip()
                if value and value.lower() not in made[f.value_list.lower()]:
                    out.append(_finding(i, rid, t.name, f.name, rule_id, "reject",
                                        f"{f.name} {value} is not a code of the new {f.value_list} value list accepted for {year}",
                                        f"Use a code from {f.value_list}.", value))  # fmt: skip
    return out


def _field_findings(version, rows, year: int) -> list[dict]:
    """The fields' own checks: a required field filled, each value valid for its type, and on a value list made in the draft (the
    engine's and the platform's lists are the rules' and the modules' to check)."""
    out = []
    for t in _tables(version):
        for i, r in enumerate(rows.get(t.name, []), start=2):
            rid = " / ".join(str(r.get(k, "")) for k in t.keys)
            for f in t.fields:
                value = str(r.get(f.name, "") or "").strip()
                if f.requirement == "system":
                    continue
                if not value:
                    if f.required:
                        out.append(_finding(i, rid, t.name, f.name, "FIELD", "reject", f"{f.name} is blank", f"Fill in {f.name}."))
                    continue
                problem = service.value_problem(f, value)
                if problem:
                    out.append(_finding(i, rid, t.name, f.name, "FIELD", "reject", f"{f.name} {value}: {problem}", "", value))
    return out + made_list_findings(version, rows, year)


def _finding(row, record_id, table, field, rule_id, severity, message, fix="", value="") -> dict:
    return {
        "row": row,
        "record_id": record_id,
        "table": table,
        "field": field,
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
        "fix": fix,
        "value": value,
    }


def _as_dict(f) -> dict:
    return f if isinstance(f, dict) else {k: getattr(f, k, "") for k in ("row", "record_id", "table", "field", "rule_id", "severity",
                                                                          "message", "fix", "value")}  # fmt: skip


def _reference_rules(version) -> set[str]:
    """The rule set's rules that need reference data an example lacks: the program registry (value list PrgID) and the
    impact-profile library (profile combinations)."""
    if not version.rule_set_id:
        return set()
    out = set()
    for r in version.rule_set.rules.all():
        d = r.definition or {}
        if (d.get("check") == "value_list" and str(d.get("list", "")).lower() == "prgid") or d.get("rule") == "combo_value_list":
            out.add(r.rule_id)
    return out


def _not_shown(version, rows, year: int) -> list[dict]:
    """The rule set's conditional rules whose condition no example record meets."""
    if not version.rule_set_id:
        return []
    from cedars.services.rules import service as rules

    out = []
    frames = {}
    for table, recs in rows.items():
        if table == "Measure":
            recs = [{**x, "CEInputID": x.get("CEInputID") or x.get("ClaimID", "")} for x in recs]
        frames[table] = rules.read_table(_csv(recs)) if recs else None
    lay = rules.layout_of(version.rule_set)
    context = rules.CheckContext(
        tables={t: df for t, df in frames.items() if df is not None},
        keys=lay["keys"],
        filer={"PA": EXAMPLE_PA, "PAType": "IOU"},
        programs=[],  # the program registry is reference data an example lacks
        upload_date=date(year, 12, 31),
    )
    for r in version.rule_set.rules.filter(kind="rule").order_by("table", "field", "rule_id"):
        if not (r.definition or {}).get("conds"):
            continue
        df = frames.get(r.table)
        held = rules.condition_met(r, df, filing_year=year, context=context) if df is not None else False
        if not held:
            out.append({"rule_id": r.rule_id, "table": r.table, "field": r.field, "severity": r.severity, "message": r.message})
    return out


def _csv(rows: list[dict]) -> bytes:
    cols = list(dict.fromkeys(k for r in rows for k in r))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="|", lineterminator="\n")
    w.writerow(cols)
    w.writerows([[r.get(c, "") for c in cols] for r in rows])
    return buf.getvalue().encode()


def check(version, example: ExampleSet, year: int | None = None) -> dict:
    """Check one example set as a filing by the example PA in `year` and store its report on it: {failing, not_shown, reference,
    notes, year}. `failing` blocks publishing when it holds a reject."""
    from cedars.services.rules import coded

    year = year or example_year()
    rows = fit(version, example.rows or {}, year)
    checker = _checkers.get(version.specification.module)
    findings = _field_findings(version, rows, year)
    notes = []
    reference_ids: frozenset[str] = frozenset()
    if checker is not None:
        reference_ids = checker.reference
        try:
            findings += [_as_dict(f) for f in checker.run(version, rows, year, EXAMPLE_PA)]
        except Exception as e:  # noqa: BLE001 - what the module could not do is the reason the example is not checked
            notes.append(f"The {version.specification.name} checks could not run on the example: {e}")
    try:
        not_shown = _not_shown(version, rows, year)
    except Exception as e:  # noqa: BLE001
        not_shown = []
        notes.append(f"The conditional rules could not be tried on the example: {e}")
    skip = _reference_rules(version) | set(reference_ids)
    failing = [f for f in findings if f["rule_id"] not in skip]
    met = {f["rule_id"] for f in findings if f["rule_id"] in skip}
    known = coded.by_id()
    reference = [
        {"rule_id": rid, "finds": known[rid].finds if rid in known else "A rule that reads the program registry or the profile library",
         "met": rid in met}
        for rid in sorted(set(reference_ids) | (met - set(reference_ids)))
    ]  # fmt: skip
    seen, unique = set(), []
    for f in sorted(failing, key=lambda f: (f["severity"] != "reject", f["table"], f["row"], f["field"], f["rule_id"])):
        k = (f["table"], f["row"], f["field"], f["rule_id"], f["message"])
        if k not in seen:
            seen.add(k)
            unique.append(f)
    report = {"year": year, "failing": unique, "not_shown": not_shown, "reference": reference, "notes": notes}
    ExampleSet.objects.filter(pk=example.pk).update(rows=rows, report=report)
    example.rows, example.report = rows, report
    return report


def check_all(version) -> None:
    """Check every example set of a draft again, and record the state they were checked in."""
    for e in version.examples.all():
        check(version, e)
    key = state(version)  # after `fit` wrote the rows back
    version.examples.update(checked=key)


def stale(version) -> bool:
    """Whether a set has not been checked since the draft last changed."""
    sets = list(version.examples.all())
    if not sets:
        return False
    key = state(version)
    return any(e.checked != key for e in sets)


def rejects(report: dict) -> list[dict]:
    return [f for f in (report or {}).get("failing", []) if f.get("severity") == "reject"]


# --- editing a set from its file


def read_file(version, filename: str, content: bytes) -> dict[str, list[dict]]:
    """A set's records from its workbook (a sheet per table, header in the first row) or .zip of delimited files (one per table).
    Sheets and files that name no table of the version are skipped; a table it lacks keeps no records."""
    lay = service.layout(version)
    tables: dict[str, list[dict]] = {}
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001 - openpyxl raises several kinds for a file that is not a workbook
            raise ExampleError("That file is not an Excel workbook (.xlsx).") from e
        for ws in wb.worksheets:
            t = lay.table_name(ws.title, vault=False)
            if t is None:
                continue
            it = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(it, [])]
            tables[t] = [
                {h: _cell(v) for h, v in zip(header, r, strict=False) if h} for r in it if r and any(v not in (None, "") for v in r)
            ]
    elif name.endswith(".zip"):
        try:
            z = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as e:
            raise ExampleError("That file is not a .zip.") from e
        for member in z.namelist():
            stem = member.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            t = lay.table_name(stem, vault=False)
            if t is None or member.endswith("/"):
                continue
            text = z.read(member).decode("utf-8-sig")
            first = text.split("\n", 1)[0]
            reader = csv.DictReader(io.StringIO(text), delimiter="|" if "|" in first else ",")
            tables[t] = [{(k or "").strip(): (v or "").strip() for k, v in r.items() if k} for r in reader]
    else:
        raise ExampleError("Load the example as the workbook (.xlsx) or .zip it downloads as.")
    if not tables:
        raise ExampleError("The file has no sheet or file named after a table of the version.")
    for t, rs in tables.items():
        if len(rs) > MAX_ROWS:
            raise ExampleError(f"The {t} table has {len(rs):,} records: an example has at most {MAX_ROWS}.")
    return tables


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def personal_information(rows: dict[str, list[dict]]) -> list[str]:
    """Where `rows` hold a value that looks like personal information, by the publication scan's patterns: ["Table.Field (email
    addresses)", ...]."""
    from cedars.services.snapshots import pii

    hits = []
    for t, rs in rows.items():
        for r in rs:
            for k, v in r.items():
                for p in pii.PATTERNS:
                    if v and pii.matches(p, str(v)):
                        label = f"{t}.{k} ({p.label})"
                        if label not in hits:
                            hits.append(label)
    return hits


@transaction.atomic
def replace_rows(actor, example: ExampleSet, rows: dict[str, list[dict]], reason: str = "") -> ExampleSet:
    """A draft's example set takes the records of its edited file, then is checked again."""
    from .editor import EditError, _draft, _require

    _require(actor)
    v = _draft(example.version)
    if not (reason or "").strip():
        raise EditError("Say why the example changes: the reason is kept with the change.")
    hits = personal_information(rows)
    if hits:
        raise EditError(
            "An example holds no personal information, and this file has what looks like it in "
            + ", ".join(hits[:5])
            + ". Replace those values with made-up ones."
        )
    before = {t: len(rs) for t, rs in (example.rows or {}).items()}
    example.rows = fit(v, rows)
    example.generated = False if example.slug != GENERATED else example.generated
    example.save(update_fields=["rows", "generated"])
    audit.record(
        "specification.example_changed",
        obj=v,
        before={"example": example.slug, "records": before},
        after={"example": example.slug, "records": {t: len(rs) for t, rs in example.rows.items()}},
        reason=reason,
        actor=actor,
    )
    check_all(v)
    example.refresh_from_db()
    return example
