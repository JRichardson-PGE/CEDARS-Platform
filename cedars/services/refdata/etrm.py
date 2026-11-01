"""eTRM's published permutations as a reference-data pack (BR-05.8): the deemed values of every permutation, by Measure Detail ID, read
from eTRM's permutations downloads (the measure pages' CSV, eTRM's own column labels). The bulk channel will be the eTRM database
replica the impact-profile sync reads (architecture: "the bulk permutation channel stays the database replica"); until then a CPUC
system administrator loads the downloads with `manage.py load_etrm_permutations`, and publishes the pack before filings use it.

The field maps say which claims-template field (the canonical dictionary names) each eTRM column fills: Claims fills a deemed claim's
blank fields from them and flags filed values that differ, and the input editor's measure-row lookup (BR-06.22) uses the same maps.

That lookup narrows a measure package's permutations one offering key at a time (`OFFERING_KEYS`, the Measure Detail ID's own order).
The keys are columns of their own under one index in that order, so each choice's values are read by a loose index scan (`choices`):
one index probe per distinct value, whatever the pack's size. Each package's name, offerings and dates are kept apart (`EtrmPackage`)."""

import csv
import io
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from django.db import connection, models, transaction

from cedars.core import storage
from cedars.services import audit

from . import service
from .models import EtrmPackage, EtrmPermutation, PackVersion

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
# the offering keys that tell a package's permutations apart, in the Measure Detail ID's order: (model field, eTRM column, what the
# pages call it). A Measure Detail ID is <measure version>-<offering>-<application type>-...-<PA type>-<PA>.
OFFERING_KEYS = (
    ("offering", "Offering ID", "Offering"),
    ("meas_app_type", "Measure Application Type", "Measure application type"),
    ("delivery_type", "Delivery Type", "Delivery type"),
    ("sector", "Sector", "Sector"),
    ("bldg_type", "Building Type", "Building type"),
    ("bldg_vint", "Building Vintage", "Building vintage"),
    ("bldg_hvac", "Building HVAC", "Building HVAC"),
    ("bldg_loc", "Building Location", "Climate zone (building location)"),
    ("ntg_id", "Net to Gross Ratio ID", "Net-to-gross ID"),
    ("gsia_id", "GSIA ID", "Installation rate (GSIA) ID"),
    ("pa_type", "Program Administrator Type", "Program administrator type"),
    ("pa", "Program Administrator", "Program administrator"),
)
KEY_FIELDS = tuple(f for f, _c, _l in OFFERING_KEYS)
NAME, OFFERING_DESC, USE_CATEGORY = "Measure Name", "Offering Description", "Use Category"
KEPT = sorted(
    {
        *MEASURE_FIELDS.values(),
        *CLAIM_FIELDS.values(),
        *MEASURE_DESCRIPTIONS.values(),
        *(c for _f, c, _l in OFFERING_KEYS),
        "Restricted Permutation",
        "PA Lead",
        NAME,
        OFFERING_DESC,
    }
)
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


def offering_keys(mdid: str, measure_version_id: str, values: dict) -> dict:
    """{model field: value} of a permutation's offering keys: each from its own column, else (a key whose column the download lacks)
    from its place in the Measure Detail ID where that place is certain: the offering after the measure version, the program
    administrator type and administrator last (the net-to-gross and GSIA IDs may hold dashes themselves)."""
    out = {f: (values.get(c) or "").strip()[: 100 if f == "ntg_id" else 60] for f, c, _l in OFFERING_KEYS}
    rest = mdid.strip()[len(measure_version_id) :].lstrip("-").split("-") if measure_version_id else []
    if len(rest) >= 3:
        out["offering"] = out["offering"] or rest[0][:60]
        out["pa_type"] = out["pa_type"] or rest[-2][:60]
        out["pa"] = out["pa"] or rest[-1][:60]
    return out


def permutation(pack, mdid: str, version: str, start: str, end: str, row: dict) -> EtrmPermutation:
    """One download row as the permutation the pack keeps."""
    mvid = version.lower() or measure_version(mdid)
    values = {c: row[c].strip() for c in KEPT if (row.get(c) or "").strip()}
    return EtrmPermutation(
        pack=pack,
        measure_detail_id=mdid,
        key=mdid.lower(),
        measure_version_id=mvid,
        start_date=_date(start),
        end_date=_date(end),
        values=values,
        **offering_keys(mdid, mvid, values),
    )


def count_package(packages: dict, perm) -> None:
    """Add a permutation to its package's summary (name, use category, offerings, count, dates) in `packages`."""
    pkg = packages.get(perm.measure_version_id)
    v = perm.values
    if pkg is None:
        mvid = perm.measure_version_id
        head = perm.measure_detail_id[: len(mvid)]
        label = head if head.lower() == mvid else mvid.upper()
        pkg = packages[mvid] = EtrmPackage(
            pack_id=perm.pack_id,
            measure_version_id=perm.measure_version_id,
            label=label[:60],
            name=v.get(NAME, "")[:255],
            use_category=v.get(USE_CATEGORY, "")[:60],
            offerings={},
            start_date=perm.start_date,
            end_date=perm.end_date,
        )
        pkg.open_ended = perm.end_date is None
    pkg.permutations += 1
    if perm.offering and perm.offering not in pkg.offerings:
        pkg.offerings[perm.offering] = v.get(OFFERING_DESC, "")
    if perm.start_date and (pkg.start_date is None or perm.start_date < pkg.start_date):
        pkg.start_date = perm.start_date
    if perm.end_date is None:
        pkg.open_ended = True
    elif pkg.end_date is None or perm.end_date > pkg.end_date:
        pkg.end_date = perm.end_date
    if pkg.open_ended:
        pkg.end_date = None


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
    packages: dict[str, EtrmPackage] = {}
    batch, rows, duplicates = [], 0, 0
    for p in paths:
        for r in read(p):
            mdid = r[ID].strip()
            key = mdid.lower()
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            perm = permutation(pack, mdid, (r.get(VERSION) or "").strip(), r.get(START) or "", r.get(END) or "", r)
            batch.append(perm)
            count_package(packages, perm)
            if len(batch) >= 5000:
                EtrmPermutation.objects.bulk_create(batch)
                rows += len(batch)
                batch = []
    EtrmPermutation.objects.bulk_create(batch)
    rows += len(batch)
    if not rows:
        raise EtrmError("The downloads hold no permutations.")
    EtrmPackage.objects.bulk_create(packages.values(), batch_size=2000)
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


# --- the input editor's lookup (BR-06.22): a package, then its offering one key at a time

MAX_CHOICES = 5000  # a guard: eTRM's offering keys have tens of values


def packages(pack: PackVersion, search: str = "", year: int | None = None, limit: int = 300):
    """The pack's measure packages whose ID or name holds `search` (any case), in effect at some time in `year` when one is given."""
    qs = EtrmPackage.objects.filter(pack=pack)
    s = search.strip()
    if s:
        qs = qs.filter(models.Q(label__icontains=s) | models.Q(name__icontains=s))
    if year:
        qs = qs.filter(models.Q(start_date__isnull=True) | models.Q(start_date__lte=date(year, 12, 31)))
        qs = qs.filter(models.Q(end_date__isnull=True) | models.Q(end_date__gte=date(year, 1, 1)))
    return list(qs.order_by("label")[:limit])


def package(pack: PackVersion, measure_version_id: str) -> EtrmPackage | None:
    return EtrmPackage.objects.filter(pack=pack, measure_version_id=measure_version_id.strip().lower()).first()


def choices(pack: PackVersion, measure_version_id: str, field: str, given: dict) -> list[str]:
    """The distinct values of offering key `field` among the package's permutations with the `given` values of every key before it, in
    order: a loose index scan of refdata_perm_offering (one probe per value), so its cost follows the number of values, not of rows.
    At most MAX_CHOICES values."""
    i = KEY_FIELDS.index(field)
    before = KEY_FIELDS[:i]
    if any(f not in given for f in before):
        raise ValueError(f"choices for {field} need a value for every key before it")
    table = EtrmPermutation._meta.db_table
    where = " AND ".join(["pack_id = %s", "measure_version_id = %s", *(f"{f} = %s" for f in before)])
    params = [pack.pk, measure_version_id.strip().lower(), *(given[f] for f in before)]
    sql = f"""
        WITH RECURSIVE t(v) AS (
            (SELECT {field} FROM {table} WHERE {where} ORDER BY {field} LIMIT 1)
            UNION ALL
            SELECT (SELECT {field} FROM {table} WHERE {where} AND {field} > t.v ORDER BY {field} LIMIT 1) FROM t WHERE t.v IS NOT NULL
        )
        SELECT v FROM t WHERE v IS NOT NULL LIMIT %s"""
    with connection.cursor() as cur:
        cur.execute(sql, [*params, *params, MAX_CHOICES])
        return [r[0] for r in cur.fetchall()]


@dataclass
class Step:
    field: str
    column: str  # eTRM's column
    label: str
    value: str  # "" while open
    options: list[str]  # the values to choose from, given the steps before
    only: bool  # the one value there is, so chosen for the person


@dataclass
class Narrowed:
    steps: list[Step]  # decided, then (when not complete) the one open step last
    permutation: EtrmPermutation | None  # when every key is decided
    matches: int = 0  # permutations with every key decided (more than 1 = the download repeats an offering)

    @property
    def open_step(self) -> Step | None:
        return self.steps[-1] if self.steps and not self.steps[-1].value else None


def narrow(pack: PackVersion, measure_version_id: str, chosen: dict, timings: list | None = None) -> Narrowed:
    """Walk the offering keys in order: a key whose `chosen` value is among its values keeps it, a key with one value takes it, and the
    first key with several values and no valid choice is the open step (the walk stops there). When every key is decided, the
    permutation is the package's one with those keys. `timings`, when given, gets each choice's lookup time in seconds."""
    given: dict[str, str] = {}
    steps = []
    for field, column, label in OFFERING_KEYS:
        t = time.perf_counter()
        opts = choices(pack, measure_version_id, field, given)
        if timings is not None:
            timings.append(time.perf_counter() - t)
        if not opts:
            return Narrowed(steps, None)
        want = (chosen.get(field) or "").strip()
        if len(opts) == 1:
            value, only = opts[0], True
        elif want in opts:
            value, only = want, False
        else:
            steps.append(Step(field, column, label, "", opts, False))
            return Narrowed(steps, None)
        given[field] = value
        steps.append(Step(field, column, label, value, opts, only))
    found = list(
        EtrmPermutation.objects.filter(pack=pack, measure_version_id=measure_version_id.strip().lower(), **given).order_by("key")[:2]
    )
    return Narrowed(steps, found[0] if found else None, len(found))


def flag(text: str) -> str:
    """eTRM's True / False as CET's 1 / 0 (anything else as it is)."""
    t = text.strip().lower()
    return {"true": "1", "false": "0", "yes": "1", "no": "0"}.get(t, text.strip())


def climate_zone(location: str) -> str:
    """eTRM's building location as the avoided-cost climate zone CET takes ("CZ01" -> "1"); "" for a location that is not a zone."""
    m = re.fullmatch(r"(?i)cz0*(\d+)", location.strip())
    return m.group(1) if m else ""


def measure_row(perm: EtrmPermutation, columns) -> tuple[dict, list[tuple[str, str, str]]]:
    """What a permutation fills in a CET Measure row with `columns`: ({column: value}, [(column, eTRM column, why not filled)]). The
    claims maps' Measure fields and descriptions, the climate zone from the building location and MeasureID = the Measure Detail ID.
    eTRM's "Any" (the PA files the actual value) and a blank are left for the person to fill."""
    cols = set(columns)
    v = perm.values
    fill, left = {}, []
    pairs = [*MEASURE_FIELDS.items(), *MEASURE_DESCRIPTIONS.items()]
    for name, col in pairs:
        if name not in cols:
            continue
        text = (v.get(col) or "").strip()
        if not text:
            if name not in MEASURE_DESCRIPTIONS:
                left.append((name, col, "blank in eTRM"))
            continue
        if text.lower() == ANY:
            left.append((name, col, "eTRM allows any value: enter the actual one"))
            continue
        fill[name] = flag(text) if name == "Upstream_Flag" else text
    if "MeasureID" in cols:
        fill["MeasureID"] = perm.measure_detail_id
    if "AC_Climate_Zone" in cols:
        zone = climate_zone(v.get("Building Location", ""))
        if zone:
            fill["AC_Climate_Zone"] = zone
        else:
            left.append(("AC_Climate_Zone", "Building Location", f"{v.get('Building Location') or 'blank'} is not one climate zone"))
    return fill, left


def editor_pack() -> PackVersion | None:
    """The pack the input editor looks permutations up in: the published one in force, else (until one is published) the newest draft;
    drafts are analysis data, and Edit inputs makes analysis jobs."""
    return service.in_force(KIND) or service.in_force(KIND, include_drafts=True)
