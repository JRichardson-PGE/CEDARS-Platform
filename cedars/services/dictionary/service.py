"""The field catalog and what is generated from it. Every function reads the current (newest) catalog version unless given one.

A field keeps one `identity` in every catalog version, whatever it is called: a field a filing specification renames
(changes.rename_fields) keeps its identity and its old name as a former name, and a later catalog that still calls it by the old name
gives it the new one (`load_catalog`)."""

import csv
import io
import uuid
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import Alias, CatalogVersion, Field, FieldRename, Usage

FORMER = "Former name"  # the system of an alias that is a name the field had before a specification renamed it

ATTRIBUTES = [
    "unit",
    "source",
    "source_version",
    "cost_tests",
    "time_basis",
    "aggregation",
    "rounding",
    "years_in_use",
    "audience",
    "required_by",
]
RETIREMENT = ["state", "retired_from", "retirement_reason", "review_usage", "review_audience", "review_regulatory"]
JSON_TYPES = {"text": "string", "number": "number", "boolean": "boolean", "year_quarter": "string"}


def current() -> CatalogVersion | None:
    return CatalogVersion.objects.first()


@transaction.atomic
def load_catalog(actor, payload: dict, *, label: str | None = None, notes: str = "", platform: bool = False) -> CatalogVersion:
    """A new catalog version from an exported catalog (scripts/export_recet_dictionary.py writes one from ReCET). With `platform`,
    the platform's own records are added to it (dictionary/platform.py: every column of the tables it keeps), and the label says so
    unless one is given: "ReCET 0.1.89 + platform 0.2.0"."""
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator publishes a dictionary version.")
    asked = label
    label = label or payload.get("source") or "catalog"
    if platform:
        import cedars

        from . import platform as records

        taken = {f["name"].lower() for f in payload["fields"]}
        added = [f for f in records.catalog() if f["name"].lower() not in taken]
        payload = {**payload, "fields": [*payload["fields"], *added]}
        label = asked or f"{label} + platform {cedars.__version__}"
    payload = {**payload, "fields": renamed(payload["fields"])}
    previous = current()
    identities = {f.name.lower(): f.identity for f in previous.fields.all()} if previous else {}
    retired = (
        {f.name.lower(): f for f in previous.fields.filter(state=Field.State.RETIRED).prefetch_related("aliases", "usages")}
        if previous
        else {}
    )
    version = CatalogVersion.objects.create(label=label, notes=notes, loaded_by=actor)
    for f in payload["fields"]:
        obj = Field.objects.create(
            version=version,
            identity=identities.get(f["name"].lower()) or uuid.uuid4(),
            name=f["name"],
            kind=f["kind"],
            group=f.get("group", ""),
            definition=f.get("definition", ""),
            doc_link=f.get("doc_link", ""),
            data_type=f.get("data_type", "text"),
            max_length=f.get("max_length"),
            unit=f.get("unit", ""),
            source=f.get("source", ""),
            source_version=f.get("version", ""),
            cost_tests=f.get("cost_tests", ""),
            time_basis=f.get("time_basis", ""),
            aggregation=f.get("aggregation", ""),
            rounding=f.get("rounding", ""),
            years_in_use=f.get("years_in_use", ""),
            audience=f.get("audience", Field.Audience.PUBLIC),
            required_by=f.get("required_by", ""),
            values=f.get("values") or [],
            # a retirement is not undone by a later export that does not know of it
            **({a: getattr(retired[f["name"].lower()], a) for a in RETIREMENT} if f["name"].lower() in retired else {}),
        )
        Alias.objects.bulk_create(Alias(field=obj, name=a["name"], system=a["system"]) for a in f.get("aliases", []))
        Usage.objects.bulk_create(
            Usage(field=obj, module=u["module"], table=u["table"], position=u["position"], required=u.get("required", False))
            for u in f.get("usage", [])
        )
    given = {f["name"].lower() for f in payload["fields"]}
    kept = [old for key, old in retired.items() if key not in given]
    for old in kept:  # a retired field stays in the catalog even when the export leaves it out
        copy_field(old, version)
    audit.record(
        "dictionary.published",
        obj=version,
        after={"label": label, "fields": len(payload["fields"]) + len(kept), "retired_kept": [f.name for f in kept]},
        actor=actor,
    )
    return version


def renamed(fields: list[dict]) -> list[dict]:
    """Catalog fields (as exported) with the specifications' renames applied: a field still under a name a specification has renamed
    takes the new name, with the old one among its other names as a former name (in rename order, so a field renamed twice takes its
    newest name). A catalog that already has the new name is left as it is."""
    renames = list(FieldRename.objects.all())
    if not renames:
        return fields
    names = {f["name"].lower() for f in fields}
    out = []
    for f in fields:
        for r in renames:
            if f["name"].lower() == r.old_name.lower() and r.new_name.lower() not in names:
                aliases = list(f.get("aliases", []))
                if not any(a["name"].lower() == f["name"].lower() for a in aliases):
                    aliases.append({"name": f["name"], "system": FORMER})
                names.add(r.new_name.lower())
                f = {**f, "name": r.new_name, "aliases": aliases}
        out.append(f)
    return out


def copy_field(old: Field, version: CatalogVersion, **changes) -> Field:
    """The field, its other names and its usages, copied into another catalog version with any changes."""
    values = {f.name: getattr(old, f.name) for f in Field._meta.concrete_fields if f.name not in ("id", "version")}
    obj = Field.objects.create(version=version, **{**values, **changes})
    Alias.objects.bulk_create(Alias(field=obj, name=a.name, system=a.system) for a in old.aliases.all())
    Usage.objects.bulk_create(
        Usage(field=obj, module=u.module, table=u.table, position=u.position, required=u.required) for u in old.usages.all()
    )
    return obj


def resolve(name: str, version: CatalogVersion | None = None) -> Field | None:
    """The field a column name means: its canonical name or any alias, ignoring case and surrounding spaces."""
    version = version or current()
    if version is None:
        return None
    n = str(name).strip()
    return Field.objects.filter(version=version, name__iexact=n).first() or (
        Field.objects.filter(version=version, aliases__name__iexact=n).first()
    )


def table_fields(
    module: str, table: str, version: CatalogVersion | None = None, *, retired: bool = False, on=None
) -> list[tuple[Field, Usage]]:
    """A module table's fields in column order. A field whose retirement is in effect (on `on`, default today) is left out unless
    `retired` is true: the templates and schemas no longer ask for it."""
    version = version or current()
    usages = Usage.objects.filter(field__version=version, module=module, table=table).select_related("field").order_by("position")
    day = on or timezone.localdate()
    return [(u.field, u) for u in usages if retired or not u.field.retired_on(day)]


@dataclass
class HeaderMap:
    """How an uploaded file's columns line up with a module's table: `columns` maps each header to its canonical name; `renamed` are
    headers given under another name (CET's), `unknown` are not in the catalog (ignored, not rejected), `not_used` are catalog fields
    this table does not use (ignored too), `missing` are required fields the file lacks."""

    columns: dict[str, str] = field(default_factory=dict)
    renamed: dict[str, str] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    not_used: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def map_headers(headers: list[str], module: str, table: str, version: CatalogVersion | None = None) -> HeaderMap:
    version = version or current()
    every = table_fields(module, table, version, retired=True)
    day = timezone.localdate()
    wanted = {f.name: u for f, u in every if not f.retired_on(day)}
    gone = {f.name for f, _ in every if f.retired_on(day)}
    lookup = {}
    for f in Field.objects.filter(version=version).prefetch_related("aliases"):
        lookup[f.name.lower()] = f.name
        for a in f.aliases.all():
            lookup.setdefault(a.name.lower(), f.name)
    out = HeaderMap()
    for h in headers:
        canonical = lookup.get(str(h).strip().lower())
        if canonical is None:
            out.unknown.append(h)
        elif canonical in gone:
            out.retired.append(h)
        elif canonical not in wanted:
            out.not_used.append(h)
        else:
            out.columns[h] = canonical
            if str(h).strip() != canonical:
                out.renamed[h] = canonical
    present = set(out.columns.values())
    out.missing = [name for name, u in wanted.items() if u.required and name not in present]
    return out


def template_csv(module: str, table: str, version: CatalogVersion | None = None) -> str:
    """An empty upload file: the table's header row in column order (pipe-delimited, as CET's files are)."""
    return "|".join(f.name for f, _ in table_fields(module, table, version)) + "\n"


def _aliases(f) -> str:
    return ", ".join(a.name for a in f.aliases.all())


def template_xlsx(module: str, table: str, version: CatalogVersion | None = None) -> bytes:
    """A workbook with the table's columns on its first sheet and a Fields sheet defining each one."""
    from cedars.core import workbooks

    version = version or current()
    rows = table_fields(module, table, version)
    book = workbooks.Book(f"CEDARS {module} {table} upload template", subject=f"Field catalog {version.label}")
    book.sheet(table[:31], [f.name for f, _ in rows])
    book.sheet(
        "Fields",
        ["Field", "Required", "Type", "Unit", "Definition", "Also accepted as", "Catalog"],
        [[f.name, "yes" if u.required else "", f.data_type, f.unit, f.definition, _aliases(f), version.label] for f, u in rows],
        widths={4: 60},
    )
    return book.save()


@dataclass(frozen=True)
class TemplateField:
    """One column of a filing template's table, as its Fields sheet describes it."""

    name: str
    required: bool = False
    type: str = ""
    unit: str = ""
    definition: str = ""
    aliases: str = ""


def catalog_fields(module: str, table: str, version: CatalogVersion | None = None) -> list[TemplateField]:
    """A module table's columns from the field catalog in force, for a filing template (none when no catalog is loaded)."""
    version = version or current()
    if version is None:
        return []
    return [
        TemplateField(f.name, u.required, f.data_type, f.unit, f.definition, _aliases(f)) for f, u in table_fields(module, table, version)
    ]


def filing_template_xlsx(title: str, tables: list[tuple[str, list[TemplateField]]], *, notes: str = "") -> bytes:
    """A filing's upload template (navigation review D2, as the claims template is): a Fields sheet first, with every column of every
    table, whether it is required, its type, unit and definition, then one sheet per table with its columns in order. An upload of the
    workbook as it stands reads only the tables' sheets; the Fields sheet is ignored."""
    from cedars.core import workbooks

    version = current()
    catalog = f"Field catalog {version.label}" if version else "No field catalog loaded"
    book = workbooks.Book(title, subject=f"The tables' columns and the definition of each field ({catalog})", description=notes)
    about = book.sheet("Fields", ["Table", "Field", "Required", "Type", "Unit", "Definition", "Also accepted as"], widths={5: 60})
    for sheet, fields in tables:
        for f in fields:
            about.append([sheet, f.name, "yes" if f.required else "", f.type, f.unit, f.definition, f.aliases])
        book.sheet(sheet[:31], [f.name for f in fields])
    return book.save()


def json_schema(module: str, table: str, version: CatalogVersion | None = None) -> dict:
    """A JSON Schema for one row of the table, for the API (a row is an object keyed by canonical names)."""
    version = version or current()
    props, required = {}, []
    for f, u in table_fields(module, table, version):
        p = {"type": [JSON_TYPES.get(f.data_type, "string"), "null"], "description": f.definition}
        if f.max_length:
            p["maxLength"] = f.max_length
        if f.data_type == "year_quarter":
            p["pattern"] = r"^\d{4}Q[1-4]$"
        if f.unit:
            p["x-unit"] = f.unit
        props[f.name] = p
        if u.required:
            required.append(f.name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{module} {table} row",
        "description": f"Generated from the CEDARS field catalog {version.label}",
        "type": "object",
        "properties": props,
        "required": required,
    }


def catalog_csv(version: CatalogVersion | None = None) -> str:
    version = version or current()
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(
        ["name", "kind", "group", "definition", "data_type", "max_length", *ATTRIBUTES, "also_known_as", "documentation", "state"]
        + ["retired_from", "values"]
    )
    for f in Field.objects.filter(version=version).prefetch_related("aliases").order_by("kind", "group", "name"):
        w.writerow(
            [
                f.name,
                f.kind,
                f.group,
                f.definition,
                f.data_type,
                f.max_length or "",
                *[getattr(f, a) for a in ATTRIBUTES],
                "; ".join(f"{a.name} ({a.system})" for a in f.aliases.all()),
                f.doc_link,
                f.state,
                f.retired_from or "",
                "; ".join(f"{code} = {text}" if text != code else code for code, text in f.values or []),
            ]
        )
    return out.getvalue()


def crosswalk_csv(version: CatalogVersion | None = None) -> str:
    """Every other name and the canonical name it maps to: the catalog's aliases, then legacy CEDARS' download labels."""
    from . import legacy

    version = version or current()
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["other_name", "used_in", "canonical_name"])
    for a in Alias.objects.filter(field__version=version).select_related("field").order_by("name"):
        w.writerow([a.name, a.system, a.field.name])
    w.writerows(legacy.crosswalk_rows())
    return out.getvalue()
