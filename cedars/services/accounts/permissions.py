"""The permission catalog. A grant gives one of these to a member of one organization.

Module permissions are "<module>.<action>". Four more are special:
  org.admin     an organization's administrator: manages its members and grants, runs its recertification (D6: the CPUC grants only
                an organization's first admin)
  claims.pii    reaching the PII vault for the organization's claims; any data steward must approve the grant, and the CPUC can require
                a federated session for it (AccessPolicy.pii_requires_federated, decision F)
  system.admin  a CPUC system administrator (CPUC organization only)
  pii.steward   a data steward (CPUC organization only; decision G): approves PII grants, confirms rule changes; never the last one revoked
"""

MODULES = {
    "programs": "Programs",
    "calculations": "Calculations",
    "impact_profiles": "Impact profiles",
    "budgets": "Budgets",
    "claims": "Claims",
    "monthly": "Monthly reports",
    "data": "Data and downloads",
    "documents": "Documents",
}

ACTIONS = {
    "read": "Read",
    "submit": "Submit",
    "confirm": "Confirm",
    "approve": "Approve",
    "admin": "Administer",
}

ORG_ADMIN = "org.admin"
PII = "claims.pii"
SYSTEM_ADMIN = "system.admin"
STEWARD = "pii.steward"

SPECIAL = {
    ORG_ADMIN: "Organization administrator",
    PII: "Claims PII (vault)",
    SYSTEM_ADMIN: "CPUC system administrator",
    STEWARD: "Data steward",
}

CPUC_ONLY = frozenset({SYSTEM_ADMIN, STEWARD})


def module_permissions() -> list[str]:
    return [f"{m}.{a}" for m in MODULES for a in ACTIONS]


def all_permissions() -> list[str]:
    return module_permissions() + list(SPECIAL)


def is_valid(code: str) -> bool:
    return code in SPECIAL or code in set(module_permissions())


def label(code: str) -> str:
    if code in SPECIAL:
        return SPECIAL[code]
    module, _, action = code.partition(".")
    return f"{MODULES.get(module, module)}: {ACTIONS.get(action, action).lower()}"


def choices() -> list[tuple[str, str]]:
    return [(c, label(c)) for c in all_permissions()]
