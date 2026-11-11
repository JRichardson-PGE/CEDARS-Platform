"""What each program definition field means, how to fill it in and its valid values, for the program fields guide
(/programs/fields/), the change form and the program page.

The guidance and the valid values are data (`program_fields.yaml` beside this file). Each field's definition comes from the data
dictionary where the dictionary has the field (ReCET's Prg_ columns), so the two never say different things; the file's own
definition is used only for a field the dictionary lacks, or before a catalog is loaded. A value is never deleted: it is retired, stays
on the versions that carry it, and is offered on the change form only to a program that already has it.

The fields with a list of values (VALUE_LISTS) are value lists a CPUC system administrator keeps on the data dictionary's Value lists
pages, in versions with effective dates. The file's values are each list's first version; once a version is published there, the
version in force is the list (`values`): a code's description is the value's label, its `meaning` the value's meaning, and a code with
an end year is retired from 1 January of the year after."""

from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path

import yaml
from django.db.models import Count
from django.utils import timezone

PATH = Path(__file__).with_name("program_fields.yaml")
ENTRY_KEYS = {"label", "dictionary", "definition", "cedars", "hint", "guidance", "values", "values_note", "set_by"}
SET_BY = {
    "": "",
    "import": "CEDARS' Programs list, when the registry imports it",
    "added": "whoever adds the program (its PA's administrators, or CPUC system administrators), once, when it is added",
}
VALUE_LISTS = {  # field: the value list that holds its values (dictionary/value_lists.py, registered in RegistryConfig.ready)
    "status": "program_status",
    "segment": "program_segment",
    "primary_sector": "program_primary_sector",
    "implementer": "program_implementer",
    "category": "program_category",
}
COUNTED = {  # the fields whose values on record the guide counts (text fields of ProgramVersion with a list of values)
    "status",
    "segment",
    "primary_sector",
    "implementer",
    "category",
}


@dataclass(frozen=True)
class Value:
    code: str
    label: str
    meaning: str = ""
    retired_from: date | None = None

    def offered_on(self, day: date) -> bool:
        return self.retired_from is None or self.retired_from > day


@dataclass(frozen=True)
class Entry:
    name: str
    label: str
    definition: str
    dictionary: tuple[str, ...] = ()
    cedars: str = ""
    hint: str = ""
    guidance: tuple[str, ...] = ()
    values: tuple[Value, ...] = ()
    values_note: str = ""
    set_by: str = ""

    @property
    def anchor(self) -> str:
        return f"field-{self.name}"

    def value(self, code: str) -> Value | None:
        return next((v for v in self.values if v.code == code), None)


def _text(value) -> str:
    return " ".join(str(value or "").split())


@cache
def entries() -> tuple[Entry, ...]:
    """Every field of the program record, in the order the program page shows them."""
    raw = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
    out = []
    for name, e in raw.items():
        unknown = set(e) - ENTRY_KEYS
        if unknown:
            raise ValueError(f"{PATH.name}: {name} has unknown keys {', '.join(sorted(unknown))}")
        if e.get("set_by", "") not in SET_BY:
            raise ValueError(f"{PATH.name}: {name} set_by is one of {', '.join(k for k in SET_BY if k)}")
        values = []
        for v in e.get("values") or []:
            retired = v.get("retired_from")
            values.append(
                Value(
                    code=str(v["code"]),
                    label=str(v.get("label") or v["code"]),
                    meaning=_text(v.get("meaning")),
                    retired_from=date.fromisoformat(str(retired)) if retired else None,
                )
            )
        if len({v.code.lower() for v in values}) != len(values):
            raise ValueError(f"{PATH.name}: {name} lists a value twice")
        out.append(
            Entry(
                name=name,
                label=e["label"],
                definition=_text(e.get("definition")),
                dictionary=tuple(e.get("dictionary") or ()),
                cedars=_text(e.get("cedars")),
                hint=_text(e.get("hint")),
                guidance=tuple(_text(p) for p in e.get("guidance") or ()),
                values=tuple(values),
                values_note=_text(e.get("values_note")),
                set_by=e.get("set_by", ""),
            )
        )
    return tuple(out)


def entry(name: str) -> Entry | None:
    return next((e for e in entries() if e.name == name), None)


def first_rows(name: str) -> list[dict]:
    """The field's values in the file, as a value list's first version (dictionary/value_lists.py): a value retired from a date is
    accepted to the year before."""
    e = entry(name)
    return [
        {
            "code": v.code,
            "description": v.label,
            "start_year": None,
            "end_year": v.retired_from.year - 1 if v.retired_from else None,
            "extra": {"meaning": v.meaning} if v.meaning else {},
        }
        for v in (e.values if e else ())
    ]


def values(name: str) -> tuple[Value, ...]:
    """The field's valid values: its value list's version in force on the Value lists pages, else the file's."""
    e = entry(name)
    if e is None:
        return ()
    list_name = VALUE_LISTS.get(name)
    if list_name is None:
        return e.values
    from cedars.services.dictionary import value_lists

    codes = value_lists.codes_in_force(list_name)
    if codes is None:
        return e.values
    return tuple(
        Value(
            code=c["code"],
            label=c["description"] or c["code"],
            meaning=_text((c["extra"] or {}).get("meaning")),
            retired_from=date(c["end_year"] + 1, 1, 1) if c["end_year"] is not None else None,
        )
        for c in codes
    )


def offered(name: str, *, current: str = "", day: date | None = None, listed: tuple[Value, ...] | None = None) -> list[Value]:
    """The values the change form offers for a field: those not retired on `day` (today), and the program's current value even when it
    is retired or not in the list at all, so a change to another field never forces a new value on it."""
    if entry(name) is None:
        return []
    listed = values(name) if listed is None else listed
    day = day or timezone.localdate()
    out = [v for v in listed if v.offered_on(day) or v.code == current]
    if current and not any(v.code == current for v in listed):
        out.append(Value(code=current, label=current, meaning="Not in the list of valid values"))
    return out


def choices(name: str, *, current: str = "", day: date | None = None, blank: str = "Not given") -> list[tuple[str, str]]:
    """(code, label) for a select: a blank choice, then the offered values; a retired current value says so."""
    day = day or timezone.localdate()
    listed = values(name)
    known_codes = {v.code for v in listed}
    out = [("", blank)]
    for v in offered(name, current=current, day=day, listed=listed):
        known = v if v.code in known_codes else None
        if known is None:
            out.append((v.code, f"{v.label} (not in the list)"))
        elif not v.offered_on(day):
            out.append((v.code, f"{v.label} (retired)"))
        else:
            out.append((v.code, v.label))
    return out


@dataclass
class Definition:
    text: str
    dictionary_field: str = ""  # the dictionary field it comes from, blank when the guide's own definition is used
    catalog: str = ""  # the catalog version it was read from


def definitions(e: Entry) -> list[Definition]:
    """The field's definition: the data dictionary's for each dictionary field it carries, else the guide's own."""
    from cedars.services.dictionary import service as dictionary

    version = dictionary.current()
    out = []
    if version is not None:
        for name in e.dictionary:
            f = dictionary.resolve(name, version)
            if f is not None and f.definition.strip():
                out.append(Definition(text=_text(f.definition), dictionary_field=f.name, catalog=version.label))
    return out or [Definition(text=e.definition)]


def on_record(name: str) -> dict[str, int]:
    """{value: number of programs whose versions carry it} for a listed field, blank values left out."""
    if name not in COUNTED:
        return {}
    from .models import ProgramVersion

    rows = (
        ProgramVersion.objects.exclude(**{name: ""})
        .values(name)
        .annotate(n=Count("program", distinct=True))
        .order_by(name)
        .values_list(name, "n")
    )
    return dict(rows)


def guide(day: date | None = None) -> list[dict]:
    """Each field with its definitions, guidance and values, for the guide page: each listed value with the number of programs that
    carry it, and the values on record that the list does not have."""
    from . import service

    day = day or timezone.localdate()
    locked = set(service.locked_fields())
    out = []
    for e in entries():
        counts = on_record(e.name)
        vals = values(e.name)
        listed = {v.code for v in vals}
        out.append(
            {
                "entry": e,
                "definitions": definitions(e),
                "list": VALUE_LISTS.get(e.name, ""),
                "values": [(v, v.offered_on(day), counts.get(v.code, 0)) for v in vals],
                "unlisted": sorted((code, n) for code, n in counts.items() if code not in listed),
                "locked": e.name in locked,
                "set_by": SET_BY.get(e.set_by, ""),
                "counted": e.name in COUNTED,
            }
        )
    return out
