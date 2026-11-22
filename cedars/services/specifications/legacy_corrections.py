"""Specification versions for correcting legacy CEDARS filings: published versions a legacy year's correction is checked and priced
against, defined ahead so a correction is checked like any filing on the platform.

A legacy CEDARS year is imported as published versions that nothing checks (historic.py records the layout it came in). When a program
administrator corrects one of the recent years, the correction is a new version filed on the platform, so it needs a published version
to be checked against and priced with. Each version here:

  - covers the legacy years it corrects (`legacy_first_year`..`legacy_last_year`) and is never offered for a new filing set: it is
    not a specification's current version, nor a choice when a filing set is set up or moved;
  - names the calculation that prices it (`calculation`): the CET replica of the release legacy CEDARS priced those years with, in the
    current engine release, so a corrected filing is priced the way the rest of it was;
  - holds the claims tables a legacy year can carry (Claim, Measure, ProgramCost, Withdrawal), in the platform's names: a field that
    the year's legacy download did not hold is optional, since the records being corrected never had it. Legacy years hold no
    site, contact or participant tables to correct, and no PII.

The documents are generated from the stored starting version and the legacy crosswalk (`documents`), stored in legacy_corrections/*.json
(`manage.py specification_legacy_corrections --write`) and loaded by migration 0010. A test holds the stored documents to the generator.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from django.utils.text import slugify

from cedars.services.dictionary import legacy as crosswalk

from . import service

FOLDER = Path(__file__).resolve().parent / "legacy_corrections"
KEPT_TABLES = {"claims": ("Claim", "Measure", "ProgramCost", "Withdrawal")}
# The legacy download layout each specification's correctable years came in, and its table names in the crosswalk.
LEGACY_LAYOUT = {"claims": "claims_b"}
LAYOUT_TABLES = {"Claim": ("Claim", "Key"), "Measure": ("Measure", "Key")}
NOT_IN_LEGACY = (
    "Legacy CEDARS did not hold this field for these years, so the records being corrected do not have it: it may be left blank."
)


@dataclass(frozen=True)
class Correction:
    specification: str  # the specification's code
    first_year: int
    last_year: int
    calculation: str  # the CET replica that prices it, e.g. cet-24.3
    release_label: str  # the CET release in words, e.g. CET 24.3

    @property
    def name(self) -> str:
        return f"Legacy corrections, {self.first_year}-{self.last_year}"


# Jake, 2026-11-21: claims of 2024-2025 were priced in legacy CEDARS with CET 24.3, those of 2026-2027 with CET 25.1; corrections are
# expected only for 2024 on, so earlier years stay read-only.
CORRECTIONS = (
    Correction("claims", 2024, 2025, "cet-24.3", "CET 24.3"),
    Correction("claims", 2026, 2027, "cet-25.1", "CET 25.1"),
)


def _legacy_fields(spec: str, table: str) -> set[str]:
    layout = LEGACY_LAYOUT[spec]
    return {m.canonical for m in crosswalk.columns(layout).values() if m.table in LAYOUT_TABLES.get(table, ())}


def document(c: Correction) -> dict:
    """The version document (service.FORMAT) of one correction version: the stored starting version's tables a legacy year can hold."""
    start = service.stored_starting(c.specification)
    tables = []
    for t in start["tables"]:
        if t["name"] not in KEPT_TABLES[c.specification]:
            continue
        t = copy.deepcopy(t)
        held = _legacy_fields(c.specification, t["name"]) if t["name"] in LAYOUT_TABLES else None
        if held is not None:
            for f in t["fields"]:
                if f["name"] not in held and f["requirement"] == "required":
                    f["requirement"] = "optional"
                    f["guidance"] = " ".join(x for x in (f.get("guidance", ""), NOT_IN_LEGACY) if x)
        tables.append(t)
    years = f"{c.first_year}-{c.last_year}"
    return {
        "format": service.FORMAT,
        "specification": start["specification"],
        "version": {
            "name": c.name,
            "slug": slugify(c.name),
            "summary": f"For correcting the claims of {years} imported from legacy CEDARS, and nothing else: a correction of one of those "
            "years is checked against this version and priced on the "
            f"{c.release_label} replica, the release legacy CEDARS priced them with, so the corrected year is priced the way the rest of "
            "it was. It holds the claim, measure, program cost and withdrawal tables; a field legacy CEDARS did not hold for those years "
            "is optional. It is never used for a new claim year.",
            "source": "Generated from the starting claims version and the legacy CEDARS crosswalk",
            "rule_set_code": start["version"].get("rule_set_code", ""),
            "engine_release": "",
            "cost_test_set": None,
            "file_format": start["version"].get("file_format") or {},
            "legacy_years": [c.first_year, c.last_year],
            "calculation": c.calculation,
        },
        "tables": tables,
        "checks": copy.deepcopy(start.get("checks", [])),
        "examples": [],
    }


def documents() -> dict[str, dict]:
    """{file name: document} of every correction version, as generated now."""
    return {f"{c.specification}-{slugify(c.name)}.json": document(c) for c in CORRECTIONS}


def write() -> list[Path]:
    FOLDER.mkdir(exist_ok=True)
    out = []
    for name, doc in documents().items():
        path = FOLDER / name
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        out.append(path)
    return out


def stored() -> dict[str, dict]:
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(FOLDER.glob("*.json"))}


def load(apps=None) -> list:
    """Load each stored correction version that is not there yet, published (migration 0010; tests that flush the database)."""
    get = apps.get_model if apps is not None else service._live_model
    Version = get("specifications", "SpecificationVersion")
    made = []
    for doc in stored().values():
        code, name = doc["specification"]["code"], doc["version"]["name"]
        if Version.objects.filter(specification__code=code, name=name).exists():
            continue
        made.append(service.load(doc, models=apps, status="published"))
    return made
