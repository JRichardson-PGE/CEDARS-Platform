"""Editing a draft in a workbook: download the draft (Settings, Tables, Fields and Rules sheets), change it in Excel and load it back.

The load never changes the draft until it is confirmed. `preview` reads the workbook into the changes it makes (`read`) and tries each
one in a transaction it then rolls back, so every difference and every change the editor would refuse is shown first, with its sheet
and row. `load` makes the same changes, with one reason, through the editor's own functions, so each is checked and audited as if
made on its page: settings, tables' other names and guidance, fields removed, renamed (the Rename to column: the same field under a
new name, its rules following it), changed and added, the fields' order (the rows' order), then rules removed, changed and added
(a rule's test as the JSON the engine reads)."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from cedars.services.accounts.service import AccessDenied

from . import editor
from .editor import EditError
from .models import SpecField, SpecificationVersion
from .service import REQUIREMENT_WORDS

ABOUT, SETTINGS, TABLES, FIELDS, RULES = "About", "Settings", "Tables", "Fields", "Rules"
SETTING_ROWS = ["Name", "Summary", "Engine release", "Cost-test set"]
TABLE_COLUMNS = ["Table", "Also accepted as", "Guidance", "Key", "Kept in the PII vault"]
FIELD_COLUMNS = [
    "Table", "Field", "Rename to", "Requirement", "Type", "Most characters", "Decimals", "Value list", "Default", "Example", "Unit",
    "Also accepted as", "Definition", "Guidance", "Locked",
]  # fmt: skip
RULE_COLUMNS = ["Rule", "Severity", "Table", "Field", "Kind", "Test", "Rule in words", "Suggested fix"]
# the columns read, by attribute (the others are there to read)
FIELD_ATTRS = {
    "Requirement": "requirement",
    "Type": "type",
    "Most characters": "max_length",
    "Decimals": "decimals",
    "Value list": "value_list",
    "Default": "default",
    "Example": "example",
    "Unit": "unit",
    "Also accepted as": "other_names",
    "Definition": "definition",
    "Guidance": "guidance",
}
SEVERITIES = {"reject": "reject", "rejects the record": "reject", "warning": "warning"}
MAX_BYTES = 5 * 1024 * 1024
CHECKING = "Checking the workbook before it is loaded"


class LoadError(EditError):
    """A workbook that cannot be read as this draft's: the message says where and why."""


@dataclass
class Change:
    """One difference the workbook makes: where it is (sheet and row), what it does in words, and how it is made."""

    sheet: str
    row: int
    words: str
    make: Callable = field(repr=False)

    @property
    def where(self) -> str:
        return f"{self.sheet}, row {self.row}" if self.row else self.sheet


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def plan_of(changes: list[Change]) -> str:
    """A fingerprint of the changes as shown, so a confirmation makes only the changes it was shown."""
    return sha256("\n".join(f"{c.where}|{c.words}" for c in changes).encode())


def file_name(v) -> str:
    return f"{v.specification.code}_{v.slug}_draft.xlsx"


# --- the workbook


def _requirement_words(code: str) -> str:
    return "Filled in by CEDARS" if code == "system" else REQUIREMENT_WORDS[code]


def draft_xlsx(v: SpecificationVersion) -> bytes:
    """The draft as a workbook to edit and load back on its Workbook page."""
    from cedars.core import workbooks

    lock = editor.locks(v)
    book = workbooks.Book(
        f"{v.specification.name}: {v.name} (draft), to edit",
        subject=f"The draft {v.name} of {v.specification.name}: its settings, tables, fields and rules",
        description="Change the Settings, Tables, Fields and Rules sheets and load the workbook back on the draft's Workbook page. "
        "Every difference is shown before it is made.",
    )
    book.sheet(
        ABOUT,
        ["About this workbook", "Value"],
        [
            ["Specification", v.specification.code],
            ["Draft", str(v.pk)],
            ["Downloaded", timezone.localtime().strftime("%Y-%m-%d %H:%M")],
            ["Settings", "Change the values. A blank engine release is the default release; a blank cost-test set is the filing set's."],
            ["Tables", "Change a table's other accepted names (separated by commas) and its guidance. Tables are not added or removed."],
            ["Fields", "One row per field, in the template's order. Change any column but Table, Field and Locked; put a new name in "
             "Rename to to rename a field; add a row to add a field (a data dictionary field needs no definition); delete a row "
             "to remove a field. A locked field keeps its name and type, and stays required if it is."],
            ["Rules", "Change a rule's severity, field, test (the JSON the engine reads), wording or suggested fix; add a row with no "
             "Rule ID for a new rule; delete a row to remove a rule. A changed test gives the rule a new ID."],
            ["Requirement", "Required, Optional or Filled in by CEDARS (only a field that already is)."],
            ["Type", ", ".join(SpecField.Type.labels)],
        ],
        widths={1: 100},
    )  # fmt: skip
    cost_label = f"{v.cost_test_set.label} {v.cost_test_set.version}".strip() if v.cost_test_set_id else ""
    book.sheet(SETTINGS, ["Setting", "Value"], [["Name", v.name], ["Summary", v.summary], ["Engine release", v.engine_release],
                                                ["Cost-test set", cost_label]], widths={1: 80})  # fmt: skip
    tables = list(v.tables.prefetch_related("fields"))
    book.sheet(
        TABLES,
        TABLE_COLUMNS,
        [[t.name, ", ".join(t.other_names), t.guidance, ", ".join(t.keys), "yes" if t.vault else ""] for t in tables],
        widths={2: 60},
    )
    fields = book.sheet(FIELDS, FIELD_COLUMNS, widths={12: 60, 13: 40})
    for t in tables:
        for f in t.fields.all():
            fields.append(
                [t.name, f.name, "", _requirement_words(f.requirement), f.get_type_display(), f.max_length, f.decimals, f.value_list,
                 f.default, f.example, f.unit, ", ".join(f.other_names), f.definition, f.guidance,
                 lock.get((t.name, f.name), "")]
            )  # fmt: skip
    rules = list(v.rule_set.rules.order_by("table", "field", "rule_id")) if v.rule_set_id else []
    book.sheet(
        RULES,
        RULE_COLUMNS,
        [[r.rule_id, r.get_severity_display(), r.table, r.field, r.kind, json.dumps(r.definition, sort_keys=True), r.message, r.fix]
         for r in rules],
        widths={5: 60, 6: 60, 7: 60},
    )  # fmt: skip
    return book.save()


# --- reading it back


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _sheets(content: bytes) -> dict[str, list[tuple[int, dict[str, str]]]]:
    """{sheet: [(row number, {column: text})]} for every sheet, its first row the header; blank rows are skipped."""
    from openpyxl import load_workbook

    if len(content) > MAX_BYTES:
        raise LoadError("That workbook is larger than a specification draft's: load the workbook the Workbook page gives.")
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - openpyxl raises several kinds for a file that is not a workbook
        raise LoadError("That file is not an Excel workbook (.xlsx).") from e
    out = {}
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        header = [_cell(h) for h in next(it, [])]
        rows = []
        for n, r in enumerate(it, start=2):
            if not r or all(v in (None, "") for v in r):
                continue
            rows.append((n, {h: _cell(v) for h, v in zip(header, r, strict=False) if h}))
        out[ws.title] = (header, rows)
    return out


def _need(sheets, name: str, columns: list[str]):
    if name not in sheets:
        raise LoadError(f"The workbook has no {name} sheet: load the workbook the Workbook page gives, with its sheets as they are.")
    header, rows = sheets[name]
    missing = [c for c in columns if c not in header]
    if missing:
        raise LoadError(f"The {name} sheet has no {', '.join(missing)} column{'s' if len(missing) > 1 else ''}: keep its columns.")
    return rows


def _names(text: str) -> list[str]:
    return [n.strip() for n in (text or "").split(",") if n.strip()]


def _requirement(text: str, where: str) -> str:
    t = (text or "").strip().lower()
    for code, words in (("required", "required"), ("optional", "optional"), ("system", "filled in by cedars")):
        if t in (code, words) or (code == "system" and t.startswith("filled in by cedars")):
            return code
    raise LoadError(f"{where}: {text!r} is not a requirement. Write Required or Optional.")


def _type(text: str, where: str) -> str:
    t = (text or "").strip().lower()
    for code, label in SpecField.Type.choices:
        if t in (code, label.lower()):
            return code
    raise LoadError(f"{where}: {text!r} is not a field type. Choose one of: {', '.join(SpecField.Type.labels)}.")


def _whole(text: str, where: str, what: str) -> int | None:
    if not (text or "").strip():
        return None
    try:
        n = int(float(text))
    except ValueError:
        raise LoadError(f"{where}: {what} is a whole number, not {text!r}.") from None
    if n < 0:
        raise LoadError(f"{where}: {what} is not below 0.")
    return n


def _row_attrs(row: dict, where: str, current: SpecField | None) -> dict:
    """A Fields row's attributes, as the editor takes them (blank type or requirement on a new row: left to the dictionary or default)."""
    out = {}
    for col, attr in FIELD_ATTRS.items():
        text = row.get(col, "")
        if attr == "requirement":
            if text or current is not None:
                out[attr] = _requirement(text, where)
        elif attr == "type":
            if text or current is not None:
                out[attr] = _type(text, where)
        elif attr == "max_length":
            out[attr] = _whole(text, where, "Most characters")
        elif attr == "decimals":
            out[attr] = _whole(text, where, "Decimals")
        elif attr == "other_names":
            out[attr] = _names(text)
        else:
            out[attr] = text
    return out


def _show(attr: str, value) -> str:
    if attr == "requirement":
        return _requirement_words(value)
    if attr == "type":
        return SpecField.Type(value).label
    if attr == "other_names":
        return ", ".join(value) or "none"
    if value in (None, ""):
        return "blank"
    text = str(value)
    return f"{text[:117]}..." if len(text) > 120 else text


def _field_changes(f: SpecField, attrs: dict) -> dict:
    """The attributes of `attrs` that differ from the field's (a type that takes no length or decimals compares them as blank)."""
    out = {}
    for attr, value in attrs.items():
        now = getattr(f, attr)
        if attr == "other_names":
            if [n.lower() for n in value] != [n.lower() for n in now]:
                out[attr] = value
        elif attr in ("max_length", "decimals"):
            kind = attrs.get("type", f.type)
            if (attr == "max_length" and kind not in editor.TEXT_TYPES) or (attr == "decimals" and kind != "number"):
                continue
            if value != now:
                out[attr] = value
        elif isinstance(now, str) or now is None:
            if (value or "") != (now or ""):
                out[attr] = value
        elif value != now:
            out[attr] = value
    return out


def _cost_test_set(text: str):
    text = (text or "").strip()
    if not text:
        return None
    for p in editor.cost_test_sets():
        if f"{p.label} {p.version}".strip().lower() == text.lower() or p.label.lower() == text.lower():
            return p
    raise LoadError(f"{SETTINGS}: there is no published cost-test set called {text}.")


def _test(text: str, where: str) -> dict:
    try:
        d = json.loads(text)
    except ValueError:
        raise LoadError(f"{where}: the test is not JSON. Copy it from another rule and change it.") from None
    if not isinstance(d, dict):
        raise LoadError(f"{where}: the test is a JSON object, such as the one a rule downloads with.")
    return d


def read(v: SpecificationVersion, content: bytes) -> list[Change]:
    """The changes a workbook makes to draft `v`, in the order they are made. Nothing is changed."""
    from cedars.services.rules import service as rules_service

    if v.status != SpecificationVersion.Status.DRAFT:
        raise LoadError("A published version never changes: start a new version from it.")
    sheets = _sheets(content)
    about = {r.get("About this workbook", ""): r.get("Value", "") for _n, r in _need(sheets, ABOUT, ["About this workbook", "Value"])}
    if about.get("Specification") != v.specification.code or about.get("Draft") != str(v.pk):
        raise LoadError(f"This workbook is not of the draft {v.name}: download the draft's own workbook and change that.")
    changes: list[Change] = []

    # settings
    settings = {r.get("Setting", ""): r.get("Value", "") for _n, r in _need(sheets, SETTINGS, ["Setting", "Value"])}
    missing = [s for s in SETTING_ROWS if s not in settings]
    if missing:
        raise LoadError(f"The {SETTINGS} sheet has no {', '.join(missing)} row: keep its rows.")
    new_settings = {
        "name": settings["Name"].strip(),
        "summary": settings["Summary"].strip(),
        "engine_release": settings["Engine release"].strip(),
        "cost_test_set": _cost_test_set(settings["Cost-test set"]),
    }
    now = {"name": v.name, "summary": v.summary, "engine_release": v.engine_release, "cost_test_set": v.cost_test_set}
    differ = [k for k in now if (now[k] or "") != (new_settings[k] or "")]
    if differ:
        words = {"name": "name", "summary": "summary", "engine_release": "engine release", "cost_test_set": "cost-test set"}

        def settings_change(actor, reason, s=new_settings):
            editor.update_settings(actor, SpecificationVersion.objects.get(pk=v.pk), reason=reason, **s)

        shown = "; ".join(f"{words[k]} from {_show(k, now[k])} to {_show(k, new_settings[k])}" for k in differ)
        changes.append(Change(SETTINGS, 0, f"Settings: {shown}.", settings_change))

    # tables
    tables = {t.name.lower(): t for t in v.tables.prefetch_related("fields")}
    for n, r in _need(sheets, TABLES, TABLE_COLUMNS[:3]):
        where = f"{TABLES}, row {n}"
        t = tables.get(r.get("Table", "").lower())
        if t is None:
            raise LoadError(f"{where}: the draft has no {r.get('Table')} table. Tables are not added or removed here.")
        others, guidance = _names(r.get("Also accepted as", "")), r.get("Guidance", "")
        parts = []
        if [x.lower() for x in others] != [x.lower() for x in t.other_names]:
            parts.append(f"other accepted names from {', '.join(t.other_names) or 'none'} to {', '.join(others) or 'none'}")
        if guidance != (t.guidance or ""):
            parts.append(f"guidance to {_show('guidance', guidance)}")
        if parts:

            def table_change(actor, reason, t=t, others=others, guidance=guidance):
                editor.change_table(actor, t, other_names=others, guidance=guidance, reason=reason)

            changes.append(Change(TABLES, n, f"{t.name}: {'; '.join(parts)}.", table_change))

    # fields
    seen: dict[tuple[str, str], int] = {}
    order: dict[str, list[str]] = {name: [] for name in tables}
    removed, renamed, changed, added = [], [], [], []
    renames: list[tuple[str, str, str]] = []
    for n, r in _need(sheets, FIELDS, FIELD_COLUMNS[:14]):
        where = f"{FIELDS}, row {n}"
        t = tables.get(r.get("Table", "").lower())
        if t is None:
            raise LoadError(f"{where}: the draft has no {r.get('Table') or '(blank)'} table.")
        name, new_name = r.get("Field", ""), r.get("Rename to", "")
        if not name:
            raise LoadError(f"{where}: the row has no field name.")
        key = (t.name.lower(), name.lower())
        if key in seen:
            raise LoadError(f"{where}: {t.name}.{name} is on row {seen[key]} too. A field has one row.")
        seen[key] = n
        f = next((x for x in t.fields.all() if x.name.lower() == name.lower()), None)
        attrs = _row_attrs(r, where, f)
        order[t.name.lower()].append(new_name or (f.name if f else name))
        if f is None:
            if new_name:
                raise LoadError(f"{where}: the draft has no {t.name}.{name} to rename. Put a new field's name in Field.")
            given = {k: val for k, val in attrs.items() if val not in ("", None, [])}

            def add(actor, reason, t=t, name=name, given=given):
                editor.add_field(actor, SpecificationVersion.objects.get(pk=v.pk), t.name, name, reason=reason, **given)

            added.append(Change(FIELDS, n, f"{t.name}: add the field {name}.", add))
            continue
        diff = _field_changes(f, attrs)
        if new_name and new_name != f.name:
            renames.append((t.name, f.name, new_name))

            def rename(actor, reason, f=f, new_name=new_name):
                editor.rename_field(actor, f, new_name, reason)

            renamed.append(Change(FIELDS, n, f"{t.name}.{f.name}: rename it {new_name} (its old name is still accepted).", rename))
            if "other_names" in diff:  # the old name stays accepted, as a rename leaves it
                kept = [x for x in diff["other_names"] if x.lower() != new_name.lower()]
                if f.name.lower() not in (x.lower() for x in kept) and new_name.lower() != (f.renamed_from or "").lower():
                    kept.append(f.name)
                diff["other_names"] = kept
        if diff:
            shown = "; ".join(f"{a.replace('_', ' ')} from {_show(a, getattr(f, a))} to {_show(a, val)}" for a, val in diff.items())

            def change(actor, reason, f=f, diff=diff):
                editor.change_field(actor, f, reason=reason, **diff)

            changed.append(Change(FIELDS, n, f"{t.name}.{new_name or f.name}: {shown}.", change))
    for t in tables.values():
        for f in t.fields.all():
            if (t.name.lower(), f.name.lower()) not in seen:

                def remove(actor, reason, f=f):
                    editor.remove_field(actor, f, reason)

                removed.append(Change(FIELDS, 0, f"{t.name}: remove the field {f.name}.", remove))
    changes += removed + renamed + changed + added
    for key, names in order.items():
        t = tables[key]
        renamed_to = {o.lower(): new for tn, o, new in renames if tn == t.name}
        kept = [renamed_to.get(f.name.lower(), f.name) for f in t.fields.all() if (key, f.name.lower()) in seen]
        known = {x.lower() for x in kept}
        now_order = kept + [x for x in names if x.lower() not in known]  # a field added goes at the end
        if [x.lower() for x in names] != [x.lower() for x in now_order]:

            def reorder(actor, reason, t=t, names=names):
                editor.order_fields(actor, t, names, reason)

            changes.append(Change(FIELDS, 0, f"{t.name}: put the fields in the rows' order.", reorder))

    # rules
    rule_rows = _need(sheets, RULES, RULE_COLUMNS[:8]) if RULES in sheets else []
    rs = v.rule_set if v.rule_set_id else None
    current = {r.rule_id: r for r in rs.rules.all()} if rs is not None else {}
    if rule_rows and (rs is None or rs.status != rs.Status.DRAFT):
        raise LoadError(f"{RULES}: the draft has no draft rule set to change.")

    def renamed_rule(table: str, fname: str, definition: dict) -> tuple[str, dict]:
        for tn, old, new in renames:
            fname, definition = rules_service.renamed_test(table, fname, definition, tn, old, new)
        return fname, definition

    listed = set()
    rule_changes, rule_adds = [], []
    for n, r in rule_rows:
        where = f"{RULES}, row {n}"
        rid = r.get("Rule", "")
        severity = SEVERITIES.get(r.get("Severity", "").strip().lower())
        if severity is None:
            raise LoadError(f"{where}: {r.get('Severity')!r} is not a severity. Write Rejects the record or Warning.")
        kind = r.get("Kind", "").strip().lower()
        if kind not in ("rule", "spec"):
            raise LoadError(f"{where}: the kind is rule (a validation or warning rule) or spec (a field constraint).")
        table, fname = r.get("Table", ""), r.get("Field", "")
        definition = _test(r.get("Test", ""), where) if r.get("Test", "") else None
        message, fix = r.get("Rule in words", ""), r.get("Suggested fix", "")
        if rid:
            rule = current.get(rid)
            if rule is None:
                raise LoadError(f"{where}: the draft has no rule {rid}. Leave the Rule column blank for a new rule.")
            if rid in listed:
                raise LoadError(f"{where}: rule {rid} is listed twice.")
            listed.add(rid)
            test = {"severity": severity, "table": table, "field": fname, "kind": kind, "definition": definition or rule.definition}
            kw = {}
            if any(test[k] != getattr(rule, k) for k in test):
                kw["field"], kw["definition"] = renamed_rule(table, fname, test["definition"])
                kw.update(severity=severity, table=table, kind=kind)
            if message != rule.message:
                kw["message"] = message
            if fix != rule.fix:
                kw["fix"] = fix
            if kw:
                what = "its test" if "definition" in kw else "its wording"

                def change_rule(actor, reason, pk=rule.pk, kw=kw):
                    rules_service.edit_rule(actor, rules_service.Rule.objects.get(pk=pk), reason=reason, **kw)

                rule_changes.append(Change(RULES, n, f"Rule {rid} ({rule.table}.{rule.field}): change {what}.", change_rule))
        else:
            if definition is None:
                raise LoadError(f"{where}: a new rule needs its test, the JSON the engine reads.")
            fname2, definition2 = renamed_rule(table, fname, definition)

            given = {"kind": kind, "severity": severity, "table": table, "field": fname2, "definition": definition2, "message": message,
                     "fix": fix}  # fmt: skip

            def add_rule(actor, reason, kw=given):
                rs_now = SpecificationVersion.objects.get(pk=v.pk).rule_set
                rules_service.add_rule(actor, rs_now, reason=reason, **kw)

            words = f"New rule on {table}.{fname or '(the table)'}: {message or 'worded from its test'}."
            rule_adds.append(Change(RULES, n, words, add_rule))
    rule_removes = []
    if RULES in sheets and rs is not None:
        for rid, rule in current.items():
            if rid not in listed:

                def remove_rule(actor, reason, pk=rule.pk):
                    rules_service.remove_rule(actor, rules_service.Rule.objects.get(pk=pk), reason)

                rule_removes.append(Change(RULES, 0, f"Remove rule {rid} ({rule.table}.{rule.field}): {rule.message}", remove_rule))
    return changes + rule_removes + rule_changes + rule_adds


class _DryRun(Exception):
    pass


def preview(actor, v: SpecificationVersion, content: bytes) -> tuple[list[Change], list[str]]:
    """The changes the workbook makes and why any of them would be refused ([] = it loads), found by making each change and rolling
    every one back. Nothing is changed."""
    editor._require(actor)
    changes = read(v, content)
    problems: list[str] = []
    try:
        with transaction.atomic():
            for c in changes:
                try:
                    with transaction.atomic():
                        c.make(actor, CHECKING)
                except (EditError, AccessDenied, ValueError) as e:
                    problems.append(f"{c.where}: {e}")
                except (KeyError, TypeError, IndexError) as e:  # a rule's test without a part the engine reads
                    problems.append(f"{c.where}: the test is not one the engine reads ({e.__class__.__name__}: {e}).")
            raise _DryRun
    except _DryRun:
        pass
    return changes, problems


@transaction.atomic
def load(actor, v: SpecificationVersion, content: bytes, reason: str) -> list[Change]:
    """Make the workbook's changes to the draft, each through the editor and audited with `reason`; all of them or none."""
    from cedars.services import audit

    editor._require(actor)
    if not (reason or "").strip():
        raise EditError("Say why the draft changes: the reason is kept with each change.")
    changes = read(v, content)
    for c in changes:
        try:
            c.make(actor, reason)
        except (EditError, AccessDenied, ValueError) as e:
            raise EditError(f"{c.where}: {e}") from e
        except (KeyError, TypeError, IndexError) as e:
            raise EditError(f"{c.where}: the test is not one the engine reads ({e.__class__.__name__}: {e}).") from e
    v = SpecificationVersion.objects.get(pk=v.pk)
    audit.record("specification.workbook_loaded", obj=v, after={"sha256": sha256(content), "changes": len(changes)}, reason=reason,
                 actor=actor)  # fmt: skip
    return changes
