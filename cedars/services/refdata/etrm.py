"""eTRM's published permutations as a reference-data pack (BR-05.8): the deemed values of every permutation, by Measure Detail ID, read
from eTRM's permutations downloads (the measure pages' CSV, eTRM's own column labels). The bulk channel will be the eTRM database
replica the impact-profile sync reads (architecture: "the bulk permutation channel stays the database replica"); until then a CPUC
system administrator loads the downloads with `manage.py load_etrm_permutations`, and publishes the pack before filings use it.

The field maps say which claims-template field (the canonical dictionary names) each eTRM column fills: Claims fills a deemed claim's
blank fields from them and flags filed values that differ, and the input editor's measure-row lookup (BR-06.22) can use the same maps."""

import csv
import io
import re
from datetime import date
from pathlib import Path

from django.db import transaction

from cedars.core import storage
from cedars.services import audit

from . import service
from .models import EtrmPermutation, PackVersion

KIND = PackVersion.Kind.ETRM_PERMUTATIONS
ID, VERSION, START, END = "Measure Detail ID", "Measure Version ID", "Effective Start Date", "End Date"

# claims-template field -> eTRM column; checked against what was filed, and filled in when blank
MEASURE_FIELDS = {
    "Sector": "Sector",
    "DeliveryType": "Delivery Type",
    "BldgType": "Building Type",
    "MeasAppType": "Measure Application Type",
    "NormUnit": "Normalized Unit",
    "TechGroup": "Technology Group",
    "TechType": "Technology Type",
    "UseCategory": "Use Category",
    "UseSubCategory": "Use Subcategory",
    "MeasImpactType": "Measure Impact Type",
    "AC_Elec_Impact_Profile": "Electric Impact Profile ID",
    "AC_Gas_Impact_Profile": "Gas Impact Profile ID",
    "UnitkW1stBaseline": "First Baseline - Peak Electric Demand Reduction",
    "UnitkWh1stBaseline": "First Baseline - Electric Savings",
    "UnitTherm1stBaseline": "First Baseline - Gas Savings",
    "UnitkW2ndBaseline": "Second Baseline - Peak Electric Demand Reduction",
    "UnitkWh2ndBaseline": "Second Baseline - Electric Savings",
    "UnitTherm2ndBaseline": "Second Baseline - Gas Savings",
    "UnitkWhIOUWater1stBaseline": "First Baseline - IOU Embedded Water Energy Savings",
    "UnitkWhIOUWater2ndBaseline": "Second Baseline - IOU Embedded Water Energy Savings",
    "UnitMeaCost1stBaseline": "Measure Total Cost 1st Baseline",
    "UnitMeaCost2ndBaseline": "Measure Total Cost 2nd Baseline",
    "NTG_ID": "Net to Gross Ratio ID",
    "NTGRkW": "NTG kW",
    "NTGRkWh": "NTG kWh",
    "NTGRTherm": "NTG Therms",
    "NTGRCost": "NTGR Cost",
    "EUL_ID": "Effective Useful Life ID",
    "EUL_Yrs": "EUL Years",
    "RUL_ID": "Remaining Useful Life ID",
    "RUL_Yrs": "RUL Years",
    "GSIA_ID": "GSIA ID",
    "InstallationRatekW": "GSIA Value",
    "InstallationRatekWh": "GSIA Value",
    "InstallationRateTherm": "GSIA Value",
    "UnitGasInfraBens": "Unit Gas Infrastructure Benefits",
    "UnitRefrigCosts": "Unit Refrigerant Costs",
    "UnitRefrigBens": "Unit Refrigerant Benefits",
    "UnitMiscCosts": "Unit Miscellaneous Costs",
    "UnitMiscBens": "Unit Miscellaneous Benefits",
    "Upstream_Flag": "Upstream Flag (True / False)",
    "CombustionType": "Combustion Type",
    "MeasInflation": "Measure Inflation",
}
CLAIM_FIELDS = {
    "BldgLoc": "Building Location",
    "BldgVint": "Building Vintage",
    "BldgHVAC": "Building HVAC",
    "MeasTechID": "Measure Technology ID",
    "DEER_MeasureID": "DEER Measure ID",
    "IETableName": "IE Table Name",
    "MeasImpactCalcType": "Measure Impact Calculation Type",
    "ETP_Flag": "ETP Flag",
}
# descriptions: filled in when blank, never compared
MEASURE_DESCRIPTIONS = {
    "MeasDescription": "Measure Case Description",
    "PreDesc": "Existing Description",
    "StdDesc": "Standard Description",
    "SourceDesc": "Source Description",
    "Version": "Version",
    "MiscCostsDesc": "Miscellaneous Cost Description",
    "MiscBensDesc": "Miscellaneous Benefits Description",
}
# fields a PA may file a value of its own for (Jake, 2026-10-30: NTG can vary in some situations): a difference is a warning, not a reject
WARN_ONLY = frozenset({"NTG_ID", "NTGRkW", "NTGRkWh", "NTGRTherm", "NTGRCost"})
# eTRM's word for "any value is valid": the PA files the actual value, so it is neither filled in nor compared
ANY = "any"
KEPT = sorted({*MEASURE_FIELDS.values(), *CLAIM_FIELDS.values(), *MEASURE_DESCRIPTIONS.values(), "Restricted Permutation", "PA Lead"})
MVID = re.compile(r"^([A-Za-z0-9]+-[0-9]+)")


class EtrmError(ValueError):
    pass


def measure_version(measure_detail_id: str) -> str:
    """The measure version a Measure Detail ID belongs to (its first two parts, "SWHC052-02"), in lower case."""
    m = MVID.match(measure_detail_id.strip())
    return m.group(1).lower() if m else ""


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip()[:10]) if text.strip() else None
    except ValueError:
        return None


def read(path: Path):
    """The rows of one eTRM permutations download ({column label: text}), streamed. The download starts with an "Exported from" line;
    the header is the first line naming the Measure Detail ID column."""
    with open(path, "rb") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        head = [text.readline() for _ in range(5)]
        start = next((i for i, line in enumerate(head) if ID in line), None)
        if start is None:
            raise EtrmError(f"{path.name} is not an eTRM permutations download: no {ID} column in its first lines")
        reader = csv.DictReader(_chain(head[start:], text))
        if VERSION not in (reader.fieldnames or []):
            raise EtrmError(f"{path.name} has no {VERSION} column")
        for row in reader:
            if (row.get(ID) or "").strip():
                yield row


def _chain(first, rest):
    yield from (line for line in first if line)
    yield from rest


@transaction.atomic
def load(actor, paths: list[Path], *, label: str, version: str, valid_from: date | None = None, source: str = "") -> PackVersion:
    """Store the downloads as a draft pack of published permutations and index every row by its Measure Detail ID. A Measure Detail ID
    that appears twice keeps its first row; the pack's notes say how many were dropped."""
    service._require(actor, KIND)
    paths = [Path(p) for p in paths]
    store = storage.store("refdata")
    folder = f"etrm_permutations/{re.sub(r'[^A-Za-z0-9._-]+', '_', f'{label}-{version}')}/"
    manifest = {}
    for p in paths:
        manifest.update(service.manifest_for(p))
        store.put_file(folder + p.name, p)
    pack = service.register_pack(
        actor,
        kind=KIND,
        label=label,
        version=version,
        valid_from=valid_from,
        manifest=manifest,
        storage_uri=store.uri(folder),
        source=source or "eTRM permutations downloads",
    )
    seen: set[str] = set()
    batch, rows, duplicates = [], 0, 0
    for p in paths:
        for r in read(p):
            mdid = r[ID].strip()
            key = mdid.lower()
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            batch.append(
                EtrmPermutation(
                    pack=pack,
                    measure_detail_id=mdid,
                    key=key,
                    measure_version_id=(r.get(VERSION) or "").strip().lower() or measure_version(mdid),
                    start_date=_date(r.get(START) or ""),
                    end_date=_date(r.get(END) or ""),
                    values={c: r[c].strip() for c in KEPT if (r.get(c) or "").strip()},
                )
            )
            if len(batch) >= 5000:
                EtrmPermutation.objects.bulk_create(batch)
                rows += len(batch)
                batch = []
    EtrmPermutation.objects.bulk_create(batch)
    rows += len(batch)
    if not rows:
        raise EtrmError("The downloads hold no permutations.")
    pack.notes = f"{rows:,} permutations" + (f"; {duplicates:,} repeated Measure Detail IDs dropped" if duplicates else "")
    pack.save(update_fields=["notes"])
    audit.record("pack.etrm_indexed", obj=pack, after={"permutations": rows, "duplicates": duplicates}, actor=actor)
    return pack


def lookup(pack: PackVersion, ids) -> dict[str, EtrmPermutation]:
    """{lower-case Measure Detail ID: permutation} for those of `ids` the pack has."""
    keys = sorted({i.strip().lower() for i in ids if i and i.strip()})
    out = {}
    for n in range(0, len(keys), 5000):
        out.update({p.key: p for p in EtrmPermutation.objects.filter(pack=pack, key__in=keys[n : n + 5000])})
    return out


def versions(pack: PackVersion, measure_versions) -> set[str]:
    """Those of the (lower-case) measure versions the pack has permutations of."""
    wanted = sorted({v for v in measure_versions if v})
    return set(
        EtrmPermutation.objects.filter(pack=pack, measure_version_id__in=wanted).values_list("measure_version_id", flat=True).distinct()
    )
