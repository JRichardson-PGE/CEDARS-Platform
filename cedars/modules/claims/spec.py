"""The claims template: CEDARS' 2026 Claims specification (cedars.cpuc.ca.gov/claims/specification/, six tables) plus the fuel-substitution
infrastructure cost table, which joins it as an ordinary non-PII claims table (BR-05.13), and a Withdrawal table, which names the claims
an upload withdraws and why (BR-05.2: a withdrawn claim keeps its records with a withdrawn status, rather than being zeroed out), and
a ClaimSite table, which lists the sites of a claim that covers more than one (population NMEC, BR-05.5).
`spec/claims_2026.csv` is that specification as data: each field under its canonical name (ReCET's renamed dictionary, decision L:
AC_Climate_Zone for E3ClimateZone and so on), the name CEDARS gives it (accepted on upload), its type, whether it is required and
CEDARS' description.

The PII tables (PII_Contact, PII_Site) are not part of it: they never travel with the claim records (BR-10.1, BR-10.4)."""

import csv
from dataclasses import dataclass
from functools import cache
from pathlib import Path

SPEC = Path(__file__).parent / "spec" / "claims_2026.csv"
SPEC_LABEL = "CEDARS Claims specification 2026, with the fuel-substitution infrastructure cost and withdrawal tables"

# In upload order; each table's primary key. ContactClaim, InfrastructureCost and ClaimSite rows replace all earlier rows of their claim.
TABLES = ("Claim", "Measure", "ProgramCost", "Site", "ContactClaim", "Participant", "InfrastructureCost", "ClaimSite", "Withdrawal")
KEYS = {
    "Claim": ("ClaimID",),
    "Measure": ("ClaimID",),
    "ProgramCost": ("PrgID", "PrgYear"),
    "Site": ("SiteID",),
    "ContactClaim": ("ClaimID", "ContactID"),
    "Participant": ("PrgID", "PrgYear", "ParticipantType", "SiteID"),
    "InfrastructureCost": ("ClaimID",),
    "ClaimSite": ("ClaimID", "SiteID"),
    "Withdrawal": ("ClaimID",),
}
# Tables whose rows for a claim are a set: a later upload's rows for a ClaimID replace every earlier row for it.
BY_CLAIM = ("ContactClaim", "InfrastructureCost", "ClaimSite")
TABLE_NAMES = {
    **{t.lower(): t for t in TABLES},
    "fuelsubstitutioninfrastructurecost": "InfrastructureCost",
    "fuelsubinfrastructurecost": "InfrastructureCost",
    "infracost": "InfrastructureCost",
    "claimsites": "ClaimSite",
    "withdrawals": "Withdrawal",
    "withdrawnclaims": "Withdrawal",
}
PII_TABLES = ("pii_contact", "pii_site", "piicontact", "piisite")
# Columns CEDARS fills in, not the PA: an uploaded value is replaced.
SYSTEM_FIELDS = {"Withdrawal": ("WithdrawnIn",)}
# Other names a column is given: CET's own job files call a claim CEInputID.
ALIASES = {"Measure": {"ceinputid": "ClaimID"}}


@dataclass(frozen=True)
class Field:
    table: str
    name: str
    cedars_name: str
    type: str  # text, text6, text255, number, integer, bit, date
    required: bool
    description: str


@cache
def fields() -> dict[str, list[Field]]:
    out: dict[str, list[Field]] = {t: [] for t in TABLES}
    with SPEC.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["table"]].append(Field(r["table"], r["field"], r["cedars_name"], r["type"], r["required"] == "yes", r["description"]))
    return out


def columns(table: str) -> list[str]:
    return [f.name for f in fields()[table]]


def upload_columns(table: str) -> list[str]:
    """The columns a PA fills in: the template's, less the ones CEDARS fills in."""
    return [c for c in columns(table) if c not in SYSTEM_FIELDS.get(table, ())]


def required(table: str) -> list[str]:
    return [f.name for f in fields()[table] if f.required]


def field(table: str, name: str) -> Field | None:
    return next((f for f in fields()[table] if f.name == name), None)


@cache
def _lookup(table: str) -> dict[str, str]:
    out = {}
    for f in fields()[table]:
        out[f.name.lower()] = f.name
        out[f.cedars_name.lower()] = f.name
    out.update(ALIASES.get(table, {}))
    return out


def canonical(table: str, header: str) -> str | None:
    """The canonical name of an uploaded column, or None when the table has no such field (ignored, not rejected: BR-02.1)."""
    return _lookup(table).get(str(header).strip().lower())


def table_name(name: str) -> str | None:
    return TABLE_NAMES.get(name.strip().lower().replace("_", "").replace(" ", "").replace("-", ""))


def is_pii_table(name: str) -> bool:
    return name.strip().lower().replace(" ", "") in PII_TABLES


TYPE_LABELS = {
    "text": "Text",
    "text6": "Text (up to 6 characters)",
    "text255": "Text (up to 255 characters)",
    "number": "Number",
    "integer": "Whole number",
    "bit": "0 or 1",
    "date": "Date (YYYY-MM-DD)",
}


def template_xlsx() -> bytes:
    """The claims template: a Fields sheet with every field's type, whether it is required, the name CEDARS gives it and its
    description, then a sheet per table with its columns in order (an upload as it stands: the Fields sheet is ignored)."""
    from cedars.core import workbooks

    book = workbooks.Book("CEDARS claims upload template", subject="The claims tables' columns and the definition of each field")
    about = book.sheet("Fields", ["Table", "Field", "CEDARS name", "Type", "Required", "Description"], widths={5: 60})
    for name in TABLES:
        for f in fields()[name]:
            cedars = f.cedars_name if f.cedars_name != f.name else ""
            about.append([name, f.name, cedars, TYPE_LABELS[f.type], "yes" if f.required else "", f.description])
        book.sheet(name, upload_columns(name))
    return book.save()
