"""The permission catalog. A grant gives one of these to a member of one organization.

Module permissions are "<module>.<action>". Seven more are special:
  org.admin     an organization's administrator: manages its members and grants, runs its recertification (the CPUC grants only
                an organization's first admin)
  claims.pii    reaching the PII vault for the organization's claims; any data steward must approve the grant, and the CPUC can require
                a federated session for it (AccessPolicy.pii_requires_federated)
  system.admin  a CPUC system administrator (CPUC organization only)
  allocation.manage  loads, confirms and discards statewide allocation factor versions; only in an organization the CPUC marks as
                managing statewide allocations (Organisation.manages_allocation; Jake 2026-11-02)
  support.answer  the CPUC's support desk (CPUC organization only): reads and answers the requests people send from a page's
                Feedback link, without being a system administrator (never a security concern, which system administrators read)
  work.manage   a backlog manager: writes work items' stories, accepts them into the backlog, orders it, and proposes declining
                one or making one public; only in the CPUC organization or one the CPUC marks as managing the backlog
                (Organisation.manages_backlog: the developer's organization; Jake 2026-11-14)
  work.approve  the CPUC's approval of work items (CPUC organization only): confirms a decline or a public item a backlog manager
                proposed; a person holding both does each in one step
  pii.steward   a data steward (CPUC organization only): approves PII grants, confirms rule changes; never the last one revoked
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
ALLOCATION = "allocation.manage"
SUPPORT = "support.answer"
WORK_MANAGE = "work.manage"
WORK_APPROVE = "work.approve"

SPECIAL = {
    ORG_ADMIN: "Organization administrator",
    PII: "Claims PII (vault)",
    SYSTEM_ADMIN: "CPUC system administrator",
    STEWARD: "Data steward",
    ALLOCATION: "Statewide allocation factors: manage",
    SUPPORT: "Support: answer requests",
    WORK_MANAGE: "Work items: manage the backlog",
    WORK_APPROVE: "Work items: approve",
}

CPUC_ONLY = frozenset({SYSTEM_ADMIN, STEWARD, SUPPORT, WORK_APPROVE})


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


# What each permission lets a person do, in plain words: shown beside its name wherever someone asks for, grants or reads one, so a
# person never has to decode "claims.submit" (UI review 3, U6). `label` names a permission; `describe` says what it is for.
_WHAT = {
    "programs": "the organization's programs",
    "calculations": "cost-effectiveness runs",
    "impact_profiles": "impact profiles",
    "budgets": "budget filings",
    "claims": "claims",
    "monthly": "monthly reports",
    "data": "record-level data and downloads",
    "documents": "documents and EM&V studies",
}
_SUBMIT = {
    "programs": "Add programs and ask for changes to them.",
    "calculations": "Start runs and edit their inputs.",
    "impact_profiles": "Upload load shapes for the packages the organization leads.",
    "budgets": "Upload, check and price budget filings, and file fund shifts.",
    "claims": "Upload, check and price claims.",
    "monthly": "Upload and check monthly reports.",
    "documents": "File documents, add versions and answer comments.",
}
_SPECIAL_WORDS = {
    ORG_ADMIN: "Manage the organization's members and their permissions, decide its access requests and run its recertification.",
    PII: "Reach the personal information (sites and contacts) behind the organization's claims. A CPUC data steward approves it too.",
    SYSTEM_ADMIN: "Set up filings, engine releases, reference data, rules, organizations and the system itself.",
    STEWARD: "Approve access to personal information and confirm changes to the access settings.",
    ALLOCATION: "Load, confirm and discard statewide allocation factors.",
    SUPPORT: "Read and answer the questions, problems, ideas and comments people send from a page's Feedback link.",
    WORK_MANAGE: "Write up work items, accept them into the backlog, order it, and propose declining one or making one public.",
    WORK_APPROVE: "Approve what a backlog manager proposes: declining a work item, or making one public.",
}


def describe(code: str) -> str:
    """One sentence on what the permission lets a person do."""
    if code in _SPECIAL_WORDS:
        return _SPECIAL_WORDS[code]
    module, _, action = code.partition(".")
    what = _WHAT.get(module, module)
    if action == "read":
        return f"See {what} and their history."
    if action == "submit":
        return _SUBMIT.get(module, f"Add and change {what}.")
    if action == "confirm":
        return f"Confirm {what}, which publishes them."
    if action == "approve":
        return f"Decide the CPUC's approvals for {what}: correction, reopen and change requests."
    if action == "admin":
        return f"Administer {what}: settings and set-up."
    return label(code)


def labels(codes) -> str:
    """Several permissions named in words, in catalog order: "Claims: read, Claims: submit"."""
    order = {c: i for i, c in enumerate(all_permissions())}
    return ", ".join(label(c) for c in sorted(set(codes), key=lambda c: order.get(c, len(order))))
