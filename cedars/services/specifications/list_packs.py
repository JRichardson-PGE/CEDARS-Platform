"""Sets of value lists kept as data (`list_packs/<name>.json`, its codes in `list_packs/<name>/*.csv`) that an administrator adds to a
draft specification version in one step, such as the code lists CEDARS' 2026 claims specification gives Claim and Participant fields.
A set names the specification it is for and, for each list, the field it fills, the list's name and its codes (code, description,
start_year, end_year). Adding it makes each list in the data dictionary as a list made in the draft (published with the version, taking
effect the same day, dropped if the draft is discarded) and points its field at it, each change audited; a field that already names the
list is left as it is. The filing modules then check the field against the list (their *-LIST checks)."""

import csv
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

FOLDER = Path(__file__).parent / "list_packs"


@dataclass(frozen=True)
class PackList:
    table: str
    field: str
    name: str
    file: str

    def content(self, slug: str) -> bytes:
        return (FOLDER / slug / self.file).read_bytes()

    def codes(self, slug: str) -> int:
        with (FOLDER / slug / self.file).open(encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))


@dataclass(frozen=True)
class ListPack:
    slug: str
    name: str
    code: str  # the specification's code
    about: str
    lists: tuple[PackList, ...]


@dataclass(frozen=True)
class ListPackState:
    pack: ListPack
    missing: list  # the pack's lists (PackList) the draft's fields do not name yet
    absent: list  # "Table.Field" the draft does not have, so their lists are not added

    @property
    def added(self) -> bool:
        return not self.missing


@cache
def packs() -> tuple[ListPack, ...]:
    out = []
    for f in sorted(FOLDER.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        lists = tuple(PackList(x["table"], x["field"], x["name"], x["file"]) for x in d["lists"])
        out.append(ListPack(f.stem, d["name"], d["code"], d.get("about", ""), lists))
    return tuple(out)


def pack(slug: str) -> ListPack | None:
    return next((p for p in packs() if p.slug == slug), None)


def state(v, p: ListPack) -> ListPackState:
    """What adding set `p` to draft `v` would change."""
    fields = {(t.name, f.name): f for t in v.tables.prefetch_related("fields") for f in t.fields.all()}
    missing, absent = [], []
    for x in p.lists:
        f = fields.get((x.table, x.field))
        if f is None:
            absent.append(f"{x.table}.{x.field}")
        elif (f.value_list or "").lower() != x.name:
            missing.append(x)
    return ListPackState(p, missing, absent)


def for_version(v) -> list[ListPackState]:
    """The sets for the version's specification, with what adding each would change."""
    return [state(v, p) for p in packs() if p.code == v.specification.code]
