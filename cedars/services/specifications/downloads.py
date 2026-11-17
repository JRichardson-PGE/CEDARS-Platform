"""What a filer downloads from a specification version: the upload template (a workbook, or a .zip of delimited files), the fields and
rules workbook, the version as JSON with a JSON Schema per table, and the example sets. Each is made from the version on request, so
it can never be out of step with it."""

from __future__ import annotations

import csv
import io
import zipfile

from . import service
from .service import REQUIREMENT_WORDS, type_words

FORMAT_NAME = "cedars-filing-specification"
JSON_TYPES = {"number": "number", "integer": "integer", "flag": ["integer", "boolean", "string"]}
PATTERNS = {
    "year_quarter": r"^\d{4}Q[1-4]$",
    "year_month": r"^\d{6}$",
    "phone": r"^\d*$",
    "date": r"^(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})$",
}


def file_stem(version) -> str:
    """e.g. claims_starting-version: the specification's code and the version's short name."""
    return f"{version.specification.code}_{version.slug}"


def _tables(version, vault: bool | None):
    return [t for t in version.tables.prefetch_related("fields") if vault is None or t.vault == vault]


def _other(f) -> str:
    return ", ".join(f.other_names)


def template_xlsx(version, *, vault: bool = False) -> bytes:
    """The upload template: a Fields sheet (every field's requirement, type, value list, example, other accepted names and definition),
    then a sheet per table with its columns in order, for the claim tables or (vault) the PII tables. The Fields sheet is not read."""
    from cedars.core import workbooks

    tables = _tables(version, vault)
    what = "PII upload template" if vault else "upload template"
    book = workbooks.Book(
        f"{version.specification.name}: {version.name}, {what}",
        subject=f"The tables' columns and the definition of each field ({version.specification.name}, {version.name})",
        description="Fill in each table's sheet and upload the workbook, or save each sheet as a delimited file of the same name in one "
        ".zip. The Fields sheet is not read.",
    )
    about = book.sheet(
        "Fields",
        ["Table", "Field", "Requirement", "Type", "Value list", "Example", "Also accepted as", "Definition"],
        widths={7: 60},
    )
    for t in tables:
        for f in t.fields.all():
            about.append(
                [t.name, f.name, REQUIREMENT_WORDS[f.requirement], type_words(f), f.value_list, f.example, _other(f), f.definition]
            )
    for t in tables:
        book.sheet(t.name[:31], [f.name for f in t.fields.all() if f.requirement != "system"])
    return book.save()


def template_zip(version, *, vault: bool = False) -> bytes:
    """The same template as a .zip of pipe-delimited files, one per table, each with its header line only."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for t in _tables(version, vault):
            z.writestr(f"{t.name}.csv", "|".join(f.name for f in t.fields.all() if f.requirement != "system") + "\n")
    return out.getvalue()


def fields_xlsx(version) -> bytes:
    """Every table, field, rule and built-in check of the version, and its settings, for people who work from spreadsheets."""
    from cedars.core import workbooks

    book = workbooks.Book(
        f"{version.specification.name}: {version.name}, fields and rules",
        subject=f"Every table, field, rule and built-in check of {version.specification.name}, {version.name}",
    )
    book.sheet(
        "Tables",
        ["Table", "Also accepted as", "Every upload holds it", "Key", "A later upload replaces", "Kept in the PII vault",
         "Refused without the columns", "Guidance"],
        [
            [t.name, ", ".join(t.other_names), "yes" if t.required else "", ", ".join(t.keys), t.get_replace_display(),
             "yes" if t.vault else "", ", ".join(t.refuse_without), t.guidance]
            for t in _tables(version, None)
        ],
        widths={7: 60},
    )  # fmt: skip
    fields = book.sheet(
        "Fields",
        ["Table", "Field", "Requirement", "Type", "Value list", "Default", "Example", "Unit", "Also accepted as", "Definition", "Guidance"],
        widths={9: 60, 10: 40},
    )
    for t in _tables(version, None):
        for f in t.fields.all():
            fields.append(
                [t.name, f.name, REQUIREMENT_WORDS[f.requirement], type_words(f), f.value_list, f.default, f.example, f.unit, _other(f),
                 f.definition, f.guidance]
            )  # fmt: skip
    book.sheet(
        "Rules",
        ["Rule", "Severity", "Table", "Field", "Rule in words", "Suggested fix"],
        [[r.rule_id, r.get_severity_display(), r.table, r.field, r.message, r.fix] for r in service.rules(version)],
        widths={4: 60, 5: 60},
    )
    book.sheet(
        "Built-in checks",
        ["Rule", "Checked by", "Severity", "Tables", "Fields", "What it finds", "How to fix it"],
        [[c.rule_id, c.group, c.severity, c.tables, c.fields, c.finds, c.fix] for c in version.checks.all()],
        widths={5: 60, 6: 60},
    )
    book.sheet("Settings", ["Setting", "Value"], service.settings_rows(version), widths={1: 80})
    return book.save()


def json_schema(version, table) -> dict:
    """A JSON Schema for one record of the table: an object keyed by the fields' names."""
    props, required = {}, []
    for f in table.fields.all():
        types = JSON_TYPES.get(f.type, "string")
        p = {"type": ([types] if isinstance(types, str) else list(types)) + ["null"], "description": f.definition or type_words(f)}
        if f.max_length and f.type in ("text", "phone", "email"):
            p["maxLength"] = f.max_length
        if f.type in PATTERNS:
            p["pattern"] = PATTERNS[f.type]
        if f.type == "email":
            p["format"] = "email"
        if f.value_list:
            p["x-value-list"] = f.value_list
        if f.other_names:
            p["x-also-accepted-as"] = list(f.other_names)
        if f.unit:
            p["x-unit"] = f.unit
        if f.requirement == "system":
            p["readOnly"] = True
        props[f.name] = p
        if f.requirement == "required":
            required.append(f.name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{version.specification.name}, {version.name}: {table.name} record",
        "description": table.guidance or f"One record of the {table.name} table",
        "type": "object",
        "properties": props,
        "required": required,
    }


def document(version, base: str = "") -> dict:
    """The whole version as data: the specification document (service.as_document) with its rules, the value lists' pages and each
    table's schema address."""
    doc = service.as_document(version)
    doc["format_name"] = FORMAT_NAME
    doc["status"] = version.status
    doc["published_at"] = version.published_at.isoformat() if version.published_at else None
    doc["fingerprint"] = version.fingerprint
    rs = version.rule_set
    doc["rule_set"] = (
        {
            "code": rs.code,
            "effective_from": rs.effective_from.isoformat(),
            "rules": [
                {"rule_id": r.rule_id, "severity": r.severity, "table": r.table, "field": r.field, "message": r.message, "fix": r.fix}
                for r in service.rules(version)
            ],
        }
        if rs
        else None
    )
    if base:
        prefix = f"{base}/api/v1/specifications/{version.specification.code}/{version.slug}"
        for t in doc["tables"]:
            t["schema"] = f"{prefix}/schema/{t['name']}"
    return doc


def example_xlsx(version, example) -> bytes:
    from cedars.core import workbooks

    book = workbooks.Book(f"{version.specification.name}: {version.name}, example {example.name}", subject=example.about)
    for t in _tables(version, None):
        rows = example.rows.get(t.name)
        if rows is None:
            continue
        cols = [f.name for f in t.fields.all() if f.requirement != "system"]
        book.sheet(t.name[:31], cols, [[r.get(c, "") for c in cols] for r in rows])
    return book.save()


def example_zip(version, example) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for t in _tables(version, None):
            rows = example.rows.get(t.name)
            if rows is None:
                continue
            cols = [f.name for f in t.fields.all() if f.requirement != "system"]
            buf = io.StringIO()
            w = csv.writer(buf, delimiter="|", lineterminator="\n")
            w.writerow(cols)
            w.writerows([[r.get(c, "") for c in cols] for r in rows])
            z.writestr(f"{t.name}.csv", buf.getvalue())
    return out.getvalue()
