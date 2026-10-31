"""The legacy CEDARS crosswalk: the display labels of CEDARS' public record-level downloads (cedars.cpuc.ca.gov/reports/record-level/)
and the canonical names they load under (decision L: ReCET's names are canonical). Phase 7 loads the years legacy CEDARS holds from those
files, so every column of every layout is either mapped to a canonical field or kept under a `Legacy_` name; none is dropped.

`data/legacy_layouts.json` is each layout's header row in order, as CEDARS publishes it (the labels only, no data):

  claims_a                2016-2022 claims (175 columns)
  claims_b                2023-2026 claims (341 columns), also the statewide unallocated files
  participants            2026 claims participant data, the second CSV in the 2026 zips (10 columns)
  filings                 budget filings 2017-2031 (221 columns), also the archived 2028-2031 snapshot
  filings_2024_2027       the archived 2024-2027 business plan application (167 columns)
  filings_2026_2027_tual  the archived 2026-2027 TUAL (169 columns)

`data/legacy_crosswalk.csv` maps each label: legacy_label, layouts (space-separated), table, canonical. Tables:

  Key          identifies the record or its submission: ClaimID (claims) or CEInputID (filings), PA (the submission's organisation),
               Used_Year (the program year)
  Measure      the Measure table's fields (CET's Measure inputs, canonical names)
  Claim        the Claim table's fields (the 2026 Claims specification): link IDs, dates, flags (layout B only)
  Participant  the 2026 Participant table
  Program      the program's attributes on each record (Prg_ names): they become a registry ProgramVersion, not measure columns
  Output       CEDARS' CET outputs, kept as filed (ReCET output names)
  Legacy       columns with no canonical field (Budget, Benefits, Program Group, Measure Table ...): kept with the tables under a
               Legacy_ name, listed in the crosswalk download so they can be found, never offered on upload

Labels that changed between layouts map to one name ("Weighted Direct Install Activity" in A and "Weighted Direct Implementation
Activity" in B are both WtdDIActivity)."""

import csv
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
LAYOUTS = ("claims_a", "claims_b", "participants", "filings", "filings_2024_2027", "filings_2026_2027_tual")
TABLES = ("Key", "Measure", "Claim", "Participant", "Program", "Output", "Legacy")
SYSTEM = "legacy CEDARS downloads"
# The Measure fields loaded as numbers (the rest stay text, as filed); every one is a number in the catalog (a test checks it).
NUMERIC_MEASURE = frozenset(
    {
        "NumUnits",
        "UnitkW1stBaseline",
        "UnitkWh1stBaseline",
        "UnitTherm1stBaseline",
        "UnitkW2ndBaseline",
        "UnitkWh2ndBaseline",
        "UnitTherm2ndBaseline",
        "UnitkWhIOUWater1stBaseline",
        "UnitkWhIOUWater2ndBaseline",
        "UnitMeaCost1stBaseline",
        "UnitMeaCost2ndBaseline",
        "UnitDirectInstallLab",
        "UnitDirectInstallMat",
        "UnitEndUserRebate",
        "UnitIncentiveToOthers",
        "NTGRkW",
        "NTGRkWh",
        "NTGRTherm",
        "NTGRCost",
        "EUL_Yrs",
        "RUL_Yrs",
        "RealizationRatekW",
        "RealizationRatekWh",
        "RealizationRateTherm",
        "InstallationRatekW",
        "InstallationRatekWh",
        "InstallationRateTherm",
        "UnitGasInfraBens",
        "UnitRefrigCosts",
        "UnitRefrigBens",
        "UnitMiscCosts",
        "UnitMiscBens",
        "UnitTaxCredits",
        "MeasInflation",
    }
)


class LayoutError(ValueError):
    """A file's header is not one of the published layouts; the message says how it differs from the nearest one."""


@dataclass(frozen=True)
class Label:
    label: str
    layouts: tuple[str, ...]
    table: str
    canonical: str


@cache
def layouts() -> dict[str, list[str]]:
    return json.loads((DATA / "legacy_layouts.json").read_text(encoding="utf-8"))


@cache
def labels() -> dict[str, Label]:
    with (DATA / "legacy_crosswalk.csv").open(encoding="utf-8", newline="") as f:
        return {
            r["legacy_label"]: Label(r["legacy_label"], tuple(r["layouts"].split()), r["table"], r["canonical"]) for r in csv.DictReader(f)
        }


def layout_of(headers: list[str]) -> str:
    """The layout a file's header row is: the same labels as a published layout (order aside). Anything else is refused, naming the
    labels missing from and added to the nearest layout, so a changed download is noticed rather than loaded half-mapped."""
    got = [str(h).strip().lstrip("﻿") for h in headers]
    for name, cols in layouts().items():
        if set(cols) == set(got) and len(cols) == len(got):
            return name
    nearest = min(layouts(), key=lambda n: len(set(layouts()[n]) ^ set(got)))
    missing = [c for c in layouts()[nearest] if c not in got]
    added = [c for c in got if c not in layouts()[nearest]]
    raise LayoutError(
        f"Not a published legacy CEDARS layout. Nearest: {nearest}; missing {len(missing)} ({', '.join(missing[:5])}), "
        f"added {len(added)} ({', '.join(added[:5])})."
    )


def columns(layout: str) -> dict[str, Label]:
    """{legacy label: its mapping} for every column of a layout, in the layout's order."""
    every = labels()
    return {c: every[c] for c in layouts()[layout]}


def rename(layout: str) -> dict[str, str]:
    """{legacy label: canonical name} for a layout (pandas' / DuckDB's rename map)."""
    return {c: m.canonical for c, m in columns(layout).items()}


def by_table(layout: str, table: str) -> list[str]:
    """The canonical names of a layout's columns that belong to one table, in the layout's order."""
    return [m.canonical for m in columns(layout).values() if m.table == table]


def crosswalk_rows() -> list[tuple[str, str, str]]:
    """(other name, used in, canonical name) rows for the dictionary's crosswalk download."""
    return [(m.label, f"{SYSTEM} ({' '.join(m.layouts)})", m.canonical) for m in labels().values()]
