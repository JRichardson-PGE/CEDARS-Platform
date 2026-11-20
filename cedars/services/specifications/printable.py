"""The specification document: one version written out for people to read, print or keep, as an accessible PDF made on request.

It holds everything the version's page shows and what the page links to: the version's status, dates and settings; each table with
its fields (requirement, type, value list, default, example, unit, other accepted names, definition, guidance) and the rules about each
field and table in words with their fixes; the built-in checks; the codes of every value list the fields use; and what changed from the
version it was based on (or the version published before it). `content` gathers it as plain data; `pdf` lays it out.

The PDF is typeset by Typst (the `typst` package: a compiled typesetter with wheels for every platform the image builds for and no
system libraries). It writes a tagged PDF to the PDF/UA-1 standard: headings, tables with header cells repeated on each page, reading
order, the document's title and language, embedded fonts, and bookmarks; it refuses to write a file that would break the standard. The
layout is `typst/specification.typ`; the version's words reach it only as JSON data (`sys.inputs`), never as markup, so nothing a field
or rule says can change the layout.

Value lists are versioned on their own, not frozen in a specification version, so the document lists each list's codes as they stand on
the day it is made, and says so. A list longer than `CODES_SHOWN` is named with its size and the places its codes are (the value list's
page and the fields-and-rules workbook, which holds every code)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from django.urls import reverse
from django.utils import timezone

from . import service
from .service import REQUIREMENT_WORDS

LAYOUT = Path(__file__).parent / "typst" / "specification.typ"
CODES_SHOWN = 400  # a longer list is pointed to, not printed: the impact profile list alone has some five thousand codes
SEVERITY_WORDS = {"reject": "Rejects the record", "warning": "Warning", "either": "Rejects the record or warns, by case"}


# --- the version it is compared with


def prior_of(version):
    """The version a document compares this one with: the published version it was copied from, else the newest version of the same
    specification published before it. None for a first version, and for a legacy CEDARS layout (it changes nothing)."""
    from .models import SpecificationVersion

    if version.historic:
        return None
    if version.based_on_id and version.based_on_id != version.pk:
        return version.based_on
    qs = SpecificationVersion.objects.filter(specification=version.specification, status=SpecificationVersion.Status.PUBLISHED).exclude(
        pk=version.pk
    )
    if version.published_at:
        qs = qs.filter(published_at__lt=version.published_at)
    return qs.order_by("-published_at", "-pk").first()


def _blank(v) -> str:
    return v if v not in ("", None) else "(blank)"


def changes(version, prior=None) -> list[tuple[str, list[str]]]:
    """What changes from `prior` (prior_of when not given) to the version, as [(heading, [sentence, ...]), ...] in the order the
    comparison page uses: tables, fields, rules, built-in checks, settings. Empty when nothing changed or there is nothing before it."""
    prior = prior if prior is not None else prior_of(version)
    if prior is None:
        return []
    c = service.compare(prior, version)
    out: list[tuple[str, list[str]]] = []
    tables = [f"Added the {t.name} table ({t.fields.count()} fields)." for t in c.tables_added]
    tables += [f"Removed the {t.name} table." for t in c.tables_removed]
    fields = [f"{t}: added {f.name}, {REQUIREMENT_WORDS[f.requirement].lower()}, {f.type_words.lower()}." for t, f in c.fields_added]
    for t, old, new, diffs in c.fields_renamed:
        more = "".join(f" {what}: {_blank(a)} to {_blank(b)}." for what, a, b in diffs)
        fields.append(f"{t}: renamed {old} to {new}; the old name is still accepted on upload.{more}")
    fields += [f"{t}: removed {f.name}." for t, f in c.fields_removed]
    for t, name, diffs in c.fields_changed:
        fields.append(f"{t}: {name}. " + " ".join(f"{what}: {_blank(a)} to {_blank(b)}." for what, a, b in diffs))
    rules = [f"Added {r.rule_id} ({r.table} {r.field}, {r.get_severity_display().lower()}): {r.message}" for r in c.rules_added]
    rules += [f"Removed {r.rule_id} ({r.table} {r.field}): {r.message}" for r in c.rules_removed]
    rules += [
        f"Changed {old.rule_id} to {new.rule_id} ({new.table} {new.field}): {old.message} ({old.get_severity_display().lower()}) "
        f"becomes {new.message} ({new.get_severity_display().lower()})"
        for old, new in c.rules_changed
    ]
    if c.rules_renamed:
        rules.append(
            f"{len(c.rules_renamed)} rule{'s' if len(c.rules_renamed) != 1 else ''} unchanged but for a renamed field's name: "
            + ", ".join(f"{new.rule_id} (was {old.rule_id})" for old, new in c.rules_renamed)
            + "."
        )
    checks = [f"Added {x.rule_id}: {x.finds}" for x in c.checks_added]
    checks += [f"Removed {x.rule_id}." for x in c.checks_removed]
    checks += [f"Changed {new.rule_id}: {new.finds}" for _, new in c.checks_changed]
    settings = [f"{what}: {_blank(a)} to {_blank(b)}." for what, a, b in c.settings_changed]
    for heading, items in (("Tables", tables), ("Fields", fields), ("Rules", rules), ("Built-in checks", checks), ("Settings", settings)):
        if items:
            out.append((heading, items))
    return out


# --- value lists


@dataclass
class ListCodes:
    name: str
    label: str
    count: int
    codes: list[dict]  # every code: {code, description, start_year, end_year}
    found: bool  # False: no codes could be read here (the engine that ships the list is not installed)


def list_codes(name: str, on: date | None = None) -> ListCodes:
    """A value list's codes on `on` (today): the version published here in force, else (a list made for a specification) its newest
    published version or, before it is published, its draft's, else the list as the engine ships it."""
    from cedars.services.dictionary import value_lists

    codes = value_lists.codes_in_force(name, on)
    if codes is None:
        codes = value_lists.codes_for_check(name, on)
    if codes is None and value_lists.made_here(name):  # made in a draft and not published yet: the draft's codes
        draft = value_lists.draft_of(value_lists.get_list(name))
        codes = [
            {"code": c.code, "description": c.description, "start_year": c.start_year, "end_year": c.end_year}
            for c in (draft.codes.order_by("pk") if draft else [])
        ]
    found = codes is not None
    if codes is None:
        try:
            codes, found = value_lists.shipped(name), True
        except (value_lists.EngineMissing, value_lists.ValueListError):
            codes = []
    try:
        label = value_lists.label_for(name)
    except Exception:  # the dictionary has no field for it: the list's own name
        label = name
    rows = [
        {"code": c["code"], "description": c.get("description") or "", "start_year": c.get("start_year"), "end_year": c.get("end_year")}
        for c in codes
    ]
    return ListCodes(name=name, label=label, count=len(rows), codes=rows, found=found)


def value_lists_of(version, on: date | None = None) -> list[ListCodes]:
    """Each value list a field of the version names, once, in the order the fields first name them."""
    names = []
    for t in version.tables.prefetch_related("fields"):
        for f in t.fields.all():
            if f.value_list and f.value_list not in names:
                names.append(f.value_list)
    return [list_codes(n, on) for n in names]


# --- the document as data


def _years(c: dict) -> str:
    a, b = c.get("start_year"), c.get("end_year")
    if a is None and b is None:
        return "Any year"
    if b is None:
        return f"{a} on"
    if a is None:
        return f"Through {b}"
    return f"{a} to {b}"


def _status_line(v) -> str:
    parts = [v.get_status_display()]
    if v.status == "published" and v.published_at:
        parts[0] = f"Published {timezone.localtime(v.published_at).date().isoformat()}"
    if v.effective_on:
        parts.append(f"takes effect {v.effective_on.isoformat()}")
    if v.status == "withdrawn" and v.withdrawn_at:
        parts.append(f"withdrawn {timezone.localtime(v.withdrawn_at).date().isoformat()}: {v.withdrawn_reason}")
    if v.status == "proposed":
        parts.append("proposed for comment; not in use, and it may still change before it is published")
    if v.status == "draft":
        parts.append("a draft; not in use, and it will change before it is published")
    if v.historic:
        parts.append("read only: no filing is set up with it or checked against it")
    return "; ".join(parts) + "."


def _table_facts(v, t) -> str:
    if v.historic:
        words = ["Every download in this layout holds this file." if t.required else "Only some downloads in this layout hold this file."]
    else:
        words = ["Every upload holds this table." if t.required else "An upload may leave this table out."]
    if t.keys:
        words.append(f"A record is identified by {' and '.join(t.keys)}.")
    if t.replace == "claim":
        words.append("A claim's rows in a later upload replace all its earlier rows.")
    if t.vault:
        words.append("Kept in the PII vault and uploaded on its own page; its fields and rules are public, its data is not.")
    if t.other_names:
        words.append(f"Also accepted as {', '.join(t.other_names)}.")
    if t.refuse_without:
        words.append(f"An upload without {', '.join(t.refuse_without)} is refused at once.")
    if t.guidance:
        words.append(t.guidance)
    return " ".join(words)


def _rule(r) -> dict:
    return {"id": r.rule_id, "severity": r.get_severity_display(), "field": r.field, "message": r.message, "fix": r.fix}


def content(version, *, base: str = "", on: date | None = None) -> dict:
    """The version as the document's data (strings, lists and dicts only). `base` is the site's address, for the links back."""
    v = version
    on = on or timezone.localdate()
    by_field, by_table = service.rules_by_field(v)
    lists = value_lists_of(v, on)
    tables = []
    for t in v.tables.prefetch_related("fields"):
        fields, rules = [], []
        for f in t.fields.all():
            notes = []
            if f.renamed_from:
                notes.append(f"Renamed from {f.renamed_from} in this version.")
            if f.default:
                notes.append(f"Blank means {f.default}.")
            if f.unit:
                notes.append(f"Unit: {f.unit}.")
            if f.example:
                notes.append(f"Example: {f.example}.")
            if f.other_names:
                notes.append(f"Also accepted as {', '.join(f.other_names)}.")
            if f.guidance:
                notes.append(f.guidance)
            fields.append(
                {
                    "name": f.name,
                    "requirement": f.get_requirement_display(),
                    "type": f.type_words,
                    "value_list": f.value_list,
                    "definition": f.definition,
                    "notes": " ".join(notes),
                }
            )
            rules += [_rule(r) for r in by_field.get((t.name, f.name), [])]
        rules += [_rule(r) for r in by_table.get(t.name, [])]
        tables.append({"name": t.name, "facts": _table_facts(v, t), "fields": fields, "rules": rules})
    labels = service.check_group_labels()
    groups: dict[str, list] = {}
    for c in v.checks.all():
        groups.setdefault(c.group, []).append(
            {
                "id": c.rule_id,
                "severity": SEVERITY_WORDS.get(c.severity, c.severity),
                "where": " ".join(x for x in (c.tables, c.fields) if x),
                "finds": c.finds,
                "fix": c.fix,
            }
        )
    prior = prior_of(v)
    page = f"{base}{reverse('specifications:version', args=[v.specification.code, v.slug])}" if base else ""
    return {
        "title": f"{v.specification.name}: {v.name}",
        "specification": v.specification.name,
        "about": v.specification.about,
        "version": v.name,
        "status": _status_line(v),
        "summary": v.summary,
        "made_on": on.isoformat(),
        "page": page,
        "historic": v.historic,
        "settings": [[a, b or "-"] for a, b in service.settings_rows(v)],
        "tables": tables,
        "checks": [{"label": labels.get(g, g), "items": items} for g, items in groups.items()],
        "lists": [
            {
                "name": x.name,
                "label": x.label,
                "count": x.count,
                "found": x.found,
                "shown": x.count <= CODES_SHOWN,
                "page": f"{base}{reverse('dictionary:list', args=[x.name])}" if base else "",
                "codes": [[c["code"], c["description"], _years(c)] for c in x.codes] if x.count <= CODES_SHOWN else [],
            }
            for x in lists
        ],
        "prior": str(prior.name) if prior else "",
        "changes": [{"heading": h, "items": items} for h, items in changes(v, prior)] if prior else [],
    }


def pdf(version, *, base: str = "", on: date | None = None) -> bytes:
    """The version as a tagged PDF/UA-1 document."""
    import json

    import typst

    data = json.dumps(content(version, base=base, on=on), ensure_ascii=False)
    return typst.compile(
        str(LAYOUT),
        sys_inputs={"data": data},
        pdf_standards=["ua-1"],
        ignore_system_fonts=True,  # the fonts that ship inside Typst, the same on every machine
    )
