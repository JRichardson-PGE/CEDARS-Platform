"""The claims layout as this module's code has it (spec.py, spec/claims_2026.csv) and the PII vault's tables, for the Quarterly claims
specification's starting version (specifications.register_layout). Until filings read their version, a change here changes the
starting document: tests/test_specifications.py fails until it is made again and published as a new version."""

from cedars.services.specifications import service as specifications
from cedars.services.vault import spec as vault_spec

from . import spec

TYPES = {
    "text": ("text", None),
    "text6": ("text", 6),
    "text255": ("text", 255),
    "number": ("number", None),
    "integer": ("integer", None),
    "bit": ("flag", None),
    "date": ("date", None),
}
VAULT_TYPES = {"text": "text", "phone": "phone", "email": "email"}
GUIDANCE = {
    "InfrastructureCost": "The fuel substitution infrastructure cost table: a claim's rows here replace all its earlier rows.",
    "ClaimSite": "The sites of a claim that covers more than one (population NMEC): a claim's rows here replace all its earlier rows.",
    "Withdrawal": "Names the claims an upload withdraws and why. A withdrawn claim keeps its records with a withdrawn status.",
    "ContactClaim": "A claim's rows here replace all its earlier rows.",
}
VAULT_GUIDANCE = (
    "Kept in the PII vault and uploaded on its own page, never with the claim tables (a file of this table in a claims upload refuses "
    "the upload). Findings name the key and the field, never the value."
)


def layout() -> dict:
    tables = []
    for name in spec.TABLES:
        aliases = {}
        for alias, target in spec.ALIASES.get(name, {}).items():
            aliases.setdefault(target, []).append(alias)
        fields = []
        for f in spec.fields()[name]:
            kind, length = TYPES[f.type]
            other = ([f.cedars_name] if f.cedars_name != f.name else []) + aliases.get(f.name, [])
            requirement = "system" if f.name in spec.SYSTEM_FIELDS.get(name, ()) else ("required" if f.required else "optional")
            fields.append(
                specifications.field_doc(
                    f.name, other_names=other, requirement=requirement, type=kind, max_length=length, definition=f.description
                )
            )
        tables.append(
            {
                "name": name,
                "other_names": list(spec.OTHER_TABLE_NAMES.get(name, ())),
                "keys": list(spec.KEYS[name]),
                "replace": "claim" if name in spec.BY_CLAIM else "key",
                "guidance": GUIDANCE.get(name, ""),
                "fields": fields,
            }
        )
    for name, fields in vault_spec.TABLES.items():
        key = vault_spec.KEYS[name]
        tables.append(
            {
                "name": name,
                "keys": [key],
                "vault": True,
                "guidance": VAULT_GUIDANCE,
                "fields": [
                    specifications.field_doc(
                        f.name,
                        requirement="required" if f.name == key else "optional",
                        type=VAULT_TYPES[f.type],
                        max_length=f.length,
                    )
                    for f in fields
                ],
            }
        )
    return {
        "specification": {
            "name": "Quarterly claims",
            "about": "The claims each program administrator files every quarter (and the statewide claims each statewide program's lead "
            "files), compiled into the claim year to date.",
            "position": 2,
        },
        "version": {
            "name": "Starting version",
            "summary": "Generated from what the platform checks today: CEDARS' 2026 Claims specification as the platform carries it, with "
            "the fuel substitution infrastructure cost, claim site and withdrawal tables, and the two PII tables kept in the vault.",
            "source": "The claims module's layout (claims_2026.csv), the PII vault's tables, the claims rule set and the built-in checks",
            "rule_set_code": "claims",
        },
        "tables": tables,
    }
