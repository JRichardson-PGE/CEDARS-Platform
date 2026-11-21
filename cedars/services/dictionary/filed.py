"""The fields the filing specifications define that the engine's catalog does not: every Claim, Site, Participant, infrastructure cost,
claim site, withdrawal and contact field, the PII vault's fields and the monthly report's Commitments fields.

ReCET's catalog holds the fields a CET job takes in and the engine calculates; the filing specifications hold more, and until these were
added the definitions lived only on each specification version's fields. `catalog()` reads them from each specification's current
version (the newest published one in effect; the starting version the platform ships before any is published), so the data dictionary
is the one place every filed field is defined. A field the catalog already has (a Measure or ProgramCost field) keeps the engine's entry;
a field several tables share (SiteID, ClaimID) is one dictionary field, defined by the first table that has it.

Each is kind "filed", its group the specification and table, its type, length and unit from the specification, its other accepted names
as aliases, and its audience confidential when its table is kept in the PII vault. They get no upload-template usages: the
specification's own templates are the templates for these tables."""

from __future__ import annotations

from cedars.services.dictionary.platform import CONFIDENTIAL, PUBLIC, us

# a specification field's type -> the dictionary's (the types the engine has no word for keep the specification's, so a field added to
# a later specification from the dictionary keeps its check: specifications.editor.DICTIONARY_TYPES)
TYPES = {
    "text": "text",
    "number": "number",
    "integer": "integer",
    "flag": "boolean",
    "date": "date",
    "year_quarter": "year_quarter",
    "year_month": "year_month",
    "phone": "phone",
    "email": "email",
}
SYSTEM = "CEDARS"  # the system an other accepted name belongs to


def documents() -> list[dict]:
    """Each specification's current version as a document; the shipped starting version where none is published yet."""
    from cedars.services.specifications import service as specifications

    out = []
    for path in sorted(specifications.STARTING.glob("*.json")):
        code = path.stem
        spec = specifications.get(code)
        version = specifications.current(spec) if spec else None
        out.append(specifications.as_document(version) if version else specifications.stored_starting(code))
    return out


def catalog(taken: set[str] | None = None, docs: list[dict] | None = None) -> list[dict]:
    """The filed fields not in `taken` (lower-case names the catalog already has), as catalog entries (the export's format)."""
    taken = set(taken or ())
    out: dict[str, dict] = {}
    for doc in docs if docs is not None else documents():
        spec = doc.get("specification", {})
        spec_name = spec.get("name") or spec.get("code", "")
        version = (doc.get("version") or {}).get("name", "")
        for table in doc.get("tables", []):
            for f in table.get("fields", []):
                key = f["name"].lower()
                where = f"{spec_name}, table {table['name']}" + (" (required)" if f.get("requirement") == "required" else "")
                if key in taken:
                    continue
                if key in out:
                    out[key]["source"] += f"; {where}"
                    continue
                kind = TYPES.get(f.get("type") or "text", "text")
                definition = " ".join(str(f.get("definition") or "").split())
                if f.get("guidance"):
                    definition = f"{definition} {' '.join(str(f['guidance']).split())}".strip()
                out[key] = {
                    "name": f["name"],
                    "kind": "filed",
                    "group": f"{spec_name}: {table['name']}",
                    "definition": us(definition),
                    "doc_link": "",
                    "data_type": kind,
                    "max_length": f.get("max_length") if kind == "text" else None,
                    "unit": f.get("unit") or "",
                    "source": f"filing specification: {where}",
                    "version": f"{spec_name}: {version}" if version else spec_name,
                    "audience": CONFIDENTIAL if table.get("vault") else PUBLIC,
                    "aliases": [{"name": n, "system": SYSTEM} for n in f.get("other_names") or [] if n.lower() not in taken],
                    "usage": [],
                    "values": [],
                }
    return list(out.values())
