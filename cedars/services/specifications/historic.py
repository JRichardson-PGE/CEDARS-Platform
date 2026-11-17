"""Legacy CEDARS' published layouts as historic specification versions: read-only records of the record-level downloads the years
imported from legacy CEDARS came in (dictionary.legacy: six layouts, the header rows as CEDARS publishes them). Each version holds one
table per download file, each column a field under CEDARS' own label, with the platform's name it is loaded as. They are never set up
on a filing set, never checked against and never edited: legacy years were filed and checked in legacy CEDARS (status `historic`,
frozen by the version trigger, migration 0009). The page of each lists the imported years in its layout, and an imported filing's page
links its layout.

The documents are generated from the crosswalk (`documents`) and stored in historic/<slug>.json (`manage.py specification_historic
--write`); migration 0009 loads them. A test holds the stored documents to the crosswalk."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from django.utils.text import slugify

from cedars.services.dictionary import legacy as crosswalk

from . import service

FOLDER = Path(__file__).resolve().parent / "historic"


@dataclass(frozen=True)
class Layout:
    specification: str  # the specification's code
    name: str  # the version's name
    years: str
    tables: tuple[tuple[str, str, str], ...]  # (layout, table name, what the file is)
    about: str


LAYOUTS = (
    Layout(
        "claims",
        "Legacy CEDARS claims, 2016-2022",
        "2016-2022",
        (("claims_a", "Claims", "The claims download: one row per measure record, with its program, claim and CEDARS' CET outputs."),),
        "The layout of legacy CEDARS' record-level claims download for claim years 2016 to 2022.",
    ),
    Layout(
        "claims",
        "Legacy CEDARS claims, 2023-2026",
        "2023-2026",
        (
            (
                "claims_b",
                "Claims",
                "The claims download: one row per measure record, with its program, claim, link IDs, dates, flags "
                "and CEDARS' CET outputs. Also the layout of the statewide claims CEDARS publishes unallocated.",
            ),
            ("participants", "Participants", "The participant data, a second file in the 2026 download only."),
        ),
        "The layout of legacy CEDARS' record-level claims download for claim years 2023 to 2026.",
    ),  # fmt: skip
    Layout(
        "budget_filing",
        "Legacy CEDARS budget filings, 2017-2031",
        "2017-2031",
        (
            (
                "filings",
                "Filings",
                "The budget filings download: one row per measure record, with its program and CEDARS' CET outputs. "
                "Also the layout of the archived 2028-2031 snapshot.",
            ),
        ),
        "The layout of legacy CEDARS' record-level budget filings download for the filings of 2017 to 2031.",
    ),  # fmt: skip
    Layout(
        "budget_filing",
        "Legacy CEDARS 2024-2027 application",
        "2024-2027",
        (("filings_2024_2027", "Filings", "The archived download of the 2024-2027 business plan application."),),
        "The layout of legacy CEDARS' archived download of the 2024-2027 business plan application.",
    ),
    Layout(
        "budget_filing",
        "Legacy CEDARS 2026-2027 true-up",
        "2026-2027",
        (("filings_2026_2027_tual", "Filings", "The archived download of the 2026-2027 true-up advice letter (TUAL)."),),
        "The layout of legacy CEDARS' archived download of the 2026-2027 true-up advice letter (TUAL).",
    ),
)
NUMERIC = crosswalk.NUMERIC_MEASURE | crosswalk.NUMERIC_LEGACY
WHERE = {
    "Key": "identifies the record or its filing",
    "Measure": "the Measure table",
    "Claim": "the Claim table",
    "Participant": "the Participant table",
    "Program": "the program's record in the registry",
    "Output": "CEDARS' CET outputs, kept as filed",
    "Legacy": "kept as filed, with no field of the platform's own",
}
KEYS = {"ClaimID", "CEInputID", "ParticipantID"}


def _field(label: str, m) -> dict:
    where = WHERE.get(m.table, m.table)
    return service.field_doc(
        label,
        requirement="optional",
        type="number" if m.canonical in NUMERIC else "text",
        guidance=f"Loaded as {m.canonical} ({where}).",
    )


def document(layout: Layout) -> dict:
    """The version document of a legacy layout (service.FORMAT), its fields in the download's column order."""
    tables = []
    for key, table, about in layout.tables:
        cols = crosswalk.columns(key)
        tables.append(
            {
                "name": table,
                "required": table != "Participants",
                "keys": [label for label, m in cols.items() if m.canonical in KEYS][:1],
                "guidance": about,
                "fields": [_field(label, m) for label, m in cols.items()],
            }
        )
    spec = service.stored_starting(layout.specification)["specification"]
    return {
        "format": service.FORMAT,
        "specification": spec,
        "version": {
            "name": layout.name,
            "summary": f"{layout.about} Recorded from the download's header as CEDARS publishes it, for the years imported from it: "
            "those years were filed and checked in legacy CEDARS, so no filing is checked against this version and it is never "
            "changed.",
            "source": "Legacy CEDARS' record-level downloads (cedars.cpuc.ca.gov/reports/record-level/)",
            "file_format": {"kind": "csv", "delimiter": ",", "encoding": "utf-8", "files": "one comma-delimited file per table, in a .zip"},
            "legacy_layouts": [key for key, _, _ in layout.tables],
            "years": layout.years,
        },
        "tables": tables,
        "checks": [],
        "examples": [],
    }


def documents() -> dict[str, dict]:
    """{file name: document} of every legacy layout, as generated from the crosswalk now."""
    return {f"{slugify(lay.name)}.json": document(lay) for lay in LAYOUTS}


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
    """Load each stored layout that is not there yet as a historic version (migration 0009; tests that flush the database)."""
    get = apps.get_model if apps is not None else service._live_model
    Version = get("specifications", "SpecificationVersion")
    made = []
    for doc in stored().values():
        code, name = doc["specification"]["code"], doc["version"]["name"]
        if Version.objects.filter(specification__code=code, name=name).exists():
            continue
        made.append(service.load(doc, models=apps, status="historic"))
    return made


# --- reading them


def versions(specification=None):
    from .models import SpecificationVersion

    qs = SpecificationVersion.objects.filter(status=SpecificationVersion.Status.HISTORIC).select_related("specification")
    if specification is not None:
        qs = qs.filter(specification=specification)
    return qs.order_by("specification__position", "name")


def for_layouts(layouts) -> list:
    """The historic versions holding any of the given legacy layouts, by name."""
    wanted = set(layouts)
    return [v for v in versions() if wanted & set(v.legacy_layouts or [])]


def years_of(version) -> str:
    """The years a historic version's layout was published for (from its document)."""
    for lay in LAYOUTS:
        if lay.name == version.name and lay.specification == version.specification.code:
            return lay.years
    return ""
