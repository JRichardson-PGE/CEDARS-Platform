"""The platform's own data in the data dictionary: every table the platform keeps besides the engine's files.

ReCET's catalog (data/dictionary/recet-<version>.json) holds the fields a CET job takes in and the engine calculates. The platform keeps
more: the program registry, the filing calendar, filings and their versions, budget lines and results, fund shifts, claims, monthly
reports, publication snapshots and holds, documents and EM&V studies, impact profiles, rule sets, release notes and the register of
CPUC-mandated changes. `catalog()` describes those tables' columns from the models themselves, so the dictionary cannot drift from
the code:

- each table in `TABLES` is a model, under the name `<app>.<Model>` (its database table is `<app>_<model>`), each column a field named
  `<app>.<Model>.<column>`, with kind "record" (a platform record), its type and length from the model, and the codes a column may hold
  from its choices;
- its definition is the model field's help text, else the entry in `data/platform_fields.yaml`, else a rule for a common column
  (`_generic`: when a record was made, who confirmed it, the organization it belongs to, ...), and a test fails on a column with none;
- its audience is the table's, and a column naming a person (a user account) is restricted;
- every model of the platform is either in `TABLES` or in `NOT_DESCRIBED` with the reason (the test fails on a new model in neither).

`load_dictionary` adds them to the ReCET catalog it loads (one catalog version: "ReCET 0.1.89 + platform 0.2.0"). The names map to no
legacy CEDARS field: the crosswalk stays legacy names only (Jake, 2026-11-02)."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import yaml
from django.apps import apps
from django.conf import settings
from django.db import models

PUBLIC, RESTRICTED, CONFIDENTIAL = "public", "restricted", "confidential"

# Every table described, with who may see what it holds (Field.Audience). Order: the order the dictionary groups them in.
TABLES: dict[str, str] = {
    # the program registry
    "registry.Program": PUBLIC,
    "registry.ProgramVersion": PUBLIC,
    "registry.FilingCycle": PUBLIC,
    "registry.ReportingPeriod": PUBLIC,
    "registry.AllocationVersion": PUBLIC,
    "registry.AllocationEntry": PUBLIC,
    "registry.AllocationShare": PUBLIC,
    "registry.ProgramChangePolicy": PUBLIC,
    "registry.ProgramChangeRequest": RESTRICTED,
    "registry.GoalSet": PUBLIC,
    "registry.Goal": PUBLIC,
    "registry.GoalCountingRule": PUBLIC,
    # the filing calendar, filings and their workflow
    "submissions.Deadline": PUBLIC,
    "submissions.WorkflowPolicy": PUBLIC,
    "submissions.Submission": PUBLIC,
    "submissions.SubmissionVersion": PUBLIC,
    "submissions.ReopenRequest": RESTRICTED,
    "submissions.Delegation": RESTRICTED,
    "submissions.Comment": RESTRICTED,
    "submissions.Attachment": RESTRICTED,
    # budgets and fund shifts
    "budgets.FilingSetup": PUBLIC,
    "budgets.BudgetLine": PUBLIC,
    "budgets.BudgetResult": PUBLIC,
    "budgets.FundShift": PUBLIC,
    "budgets.FundShiftLine": PUBLIC,
    "budgets.FundShiftAcknowledgement": PUBLIC,
    "budgets.ChangeLetter": PUBLIC,
    # claims
    "claims.ClaimYear": PUBLIC,
    "claims.ClaimProgramLine": PUBLIC,
    "claims.ClaimResult": PUBLIC,
    "claims.ClaimRecord": PUBLIC,
    "claims.Restatement": PUBLIC,
    "claims.RestatementLine": PUBLIC,
    "claims.ChangeLetter": PUBLIC,
    # monthly reports
    "monthly.YearSetup": PUBLIC,
    "monthly.ReportLine": PUBLIC,
    "monthly.MonthlyResult": PUBLIC,
    "monthly.ChangeLetter": PUBLIC,
    # calculations
    "calculations.Run": RESTRICTED,
    "calculations.EngineRelease": PUBLIC,
    "calculations.OutputTemplate": RESTRICTED,
    # publication
    "snapshots.Snapshot": PUBLIC,
    "snapshots.Release": PUBLIC,
    "snapshots.PIIScan": RESTRICTED,
    "snapshots.ScanSettings": RESTRICTED,
    # documents and EM&V studies
    "documents.Document": PUBLIC,
    "documents.DocumentVersion": PUBLIC,
    "documents.DocumentFile": PUBLIC,
    "documents.AttributeList": PUBLIC,
    "documents.AttributeOption": PUBLIC,
    "documents.CommentPeriod": PUBLIC,
    "documents.ResponseRequest": PUBLIC,
    "documents.Comment": PUBLIC,
    "documents.CommentResponse": PUBLIC,
    "documents.WorkOrder": PUBLIC,
    "documents.Study": PUBLIC,
    "documents.StudyProgram": PUBLIC,
    "documents.KeyDate": PUBLIC,
    # impact profiles and load shapes
    "impact_profiles.ImpactProfile": PUBLIC,
    "impact_profiles.ProfileVersion": PUBLIC,
    "impact_profiles.ProfileZone": PUBLIC,
    "impact_profiles.ProfileMapping": PUBLIC,
    "impact_profiles.LoadShape": PUBLIC,
    "impact_profiles.LoadShapeVersion": PUBLIC,
    "impact_profiles.LoadShapeUpload": RESTRICTED,
    "impact_profiles.UploadFinding": RESTRICTED,
    "impact_profiles.Shape": PUBLIC,
    "impact_profiles.MeasurePackage": PUBLIC,
    "impact_profiles.PackageVersion": PUBLIC,
    "impact_profiles.Permutation": PUBLIC,
    "impact_profiles.ExcludedPackage": PUBLIC,
    "impact_profiles.LibraryExport": PUBLIC,
    "impact_profiles.LibrarySettings": PUBLIC,
    # reference data and rules
    "refdata.PackVersion": PUBLIC,
    "refdata.TieOutRequirement": PUBLIC,
    "refdata.TieOut": PUBLIC,
    "refdata.EtrmPackage": PUBLIC,
    "refdata.EtrmPermutation": PUBLIC,
    "rules.RuleSet": PUBLIC,
    "rules.Rule": PUBLIC,
    "specifications.Specification": PUBLIC,
    "specifications.SpecificationVersion": PUBLIC,
    "specifications.SpecTable": PUBLIC,
    "specifications.SpecField": PUBLIC,
    "specifications.CheckSetting": PUBLIC,
    "specifications.ExampleSet": PUBLIC,
    "specifications.ImpactTest": RESTRICTED,  # a draft's test, read by CPUC staff: counts per PA of confirmed filings
    "specifications.SpecComment": RESTRICTED,  # comments on a proposed version: read by anyone signed in, and they name their authors
    # releases, the register of mandated changes, retention
    "releases.ReleaseNote": PUBLIC,
    "releases.Change": PUBLIC,
    "releases.LimitChange": PUBLIC,
    "releases.Mandate": PUBLIC,
    "releases.MandateLink": PUBLIC,
    "releases.SiteNotice": PUBLIC,
    "retention.RetentionPolicy": PUBLIC,
    "retention.LegalHold": RESTRICTED,
    # organizations
    "accounts.Organisation": PUBLIC,
    "accounts.OrganisationCode": PUBLIC,
    # personal information, held only in the vault
    "vault.PIIUpload": CONFIDENTIAL,
    "vault.PIIContact": CONFIDENTIAL,
    "vault.PIISite": CONFIDENTIAL,
    # the audit trail and document accessibility checks
    "audit.AuditEvent": RESTRICTED,
    "accessibility.FileCheck": RESTRICTED,
    "notifications.Subscription": RESTRICTED,
}

# Every other model, and why the dictionary does not describe it: machinery and access control, not data anyone files or reads.
NOT_DESCRIBED: dict[str, str] = {
    "accounts.User": "sign-in accounts (identity and access, not filed data)",
    "accounts.Membership": "access control",
    "accounts.RoleTemplate": "access control",
    "accounts.Grant": "access control",
    "accounts.AccessRequest": "access control",
    "accounts.RecertificationCampaign": "access control",
    "accounts.RecertificationItem": "access control",
    "accounts.AccessPolicy": "access control",
    "accounts.PolicyChange": "access control",
    "accounts.ApiToken": "access control",
    "dictionary.CatalogVersion": "the dictionary itself",
    "dictionary.Field": "the dictionary itself",
    "dictionary.Alias": "the dictionary itself",
    "dictionary.FieldRename": "the dictionary itself (each rename is in the field's history and the change log)",
    "dictionary.Usage": "the dictionary itself",
    "dictionary.ValueList": "the dictionary itself (its codes are on the value list pages)",
    "dictionary.ValueListVersion": "the dictionary itself (its codes are on the value list pages)",
    "dictionary.Code": "the dictionary itself (its codes are on the value list pages)",
    "notifications.Preference": "a person's own settings",
    "notifications.DeadlineReminder": "machinery (reminders already sent)",
    "notifications.Notification": "a person's own messages",
    "feedback.Feedback": "messages to the site's administrators",
    "sandbox.Refresh": "machinery (sandbox refreshes)",
    "sandbox.CopiedRecord": "machinery (sandbox refreshes)",
    "legacy.LegacySource": "machinery (legacy CEDARS imports; the imported years are filings)",
    "legacy.LegacyImport": "machinery (legacy CEDARS imports; the imported years are filings)",
    "impact_profiles.EtrmSyncRun": "machinery (eTRM synchronization runs)",
    "impact_profiles.ProcessingJob": "machinery (background jobs)",
    "calculations.InputDraft": "a person's working copy of a job's inputs",
    "refdata.CostTestBuild": "a person's working copy of a cost-test set (the set it becomes is a pack, described above)",
    "calculations.OutputTemplateDefault": "a person's own settings",
    "calculations.ResultView": "a person's own settings (saved Results columns)",
    "calculations.Worker": "machinery (calculation workers)",
    "rules.CheckJob": "machinery (a filing's rules queued for its engine release's worker; the filing records the findings)",
    "data.DownloadJob": "machinery (background downloads)",
    "data.SavedView": "a person's saved page settings",
    "data.RecordColumnDefault": "a person's own settings",
    "retention.PurgeRun": "machinery (retention purge runs)",
    "documents.FileText": "machinery (the search index of a file's text)",
    "documents.DocumentSubscription": "a person's own settings",
    "documents.LegacyLink": "machinery (redirects from legacy CEDARS links)",
    "documents.Task": "machinery (background jobs)",
    "accessibility.CheckSettings": "a setting of the accessibility check",
}

DEFINITIONS_FILE = Path(__file__).parent / "data" / "platform_fields.yaml"
DATA_TYPES = {
    models.BooleanField: "boolean",
    models.DateTimeField: "datetime",
    models.DateField: "date",
    models.DurationField: "duration",
    models.JSONField: "json",
    models.UUIDField: "text",
    models.BinaryField: "binary",
}
NUMBERS = (models.IntegerField, models.DecimalField, models.FloatField)


def our_models() -> list[type[models.Model]]:
    """Every concrete model of the platform's own apps."""
    return [m for m in apps.get_models() if m.__module__.startswith("cedars.") and not m._meta.abstract and not m._meta.proxy]


def label(model) -> str:
    return model._meta.label


def columns(model) -> list[models.Field]:
    """The table's stored columns, in the model's order (the primary key, then the fields as declared)."""
    return [f for f in model._meta.get_fields() if getattr(f, "concrete", False) and not f.auto_created and not f.many_to_many] + [
        f for f in model._meta.local_many_to_many
    ]


@cache
def definitions() -> dict:
    """The written definitions: {'<app>.<Model>': {'table': ..., 'fields': {column: definition}}}."""
    return yaml.safe_load(DEFINITIONS_FILE.read_text(encoding="utf-8")) or {}


def table_name(model) -> str:
    """The table in words, e.g. "Budgets: fund shift lines"."""
    return us(f"{model._meta.app_config.verbose_name}: {model._meta.verbose_name_plural}")


def us(text: str) -> str:
    """US spelling for words the models spell the other way (CLAUDE.md: identifiers keep `organisation`, text does not)."""
    text = re.sub(r"(?<![\w.])([Oo])rganis(ation|e|ed)", r"\1rganiz\2", text)
    for british, american in US_WORDS.items():
        text = re.sub(rf"\b{british}", american, text)
    return text


US_WORDS = {"acknowledgement": "acknowledgment", "Acknowledgement": "Acknowledgment", "cancelled": "canceled", "Cancelled": "Canceled"}


def _noun(model) -> str:
    return us(re.sub(r"\b(pii|etrm|acc)\b", lambda m: {"etrm": "eTRM"}.get(m.group(1), m.group(1).upper()), str(model._meta.verbose_name)))


def _is_person(f) -> bool:
    return f.is_relation and f.related_model is not None and label(f.related_model) == settings.AUTH_USER_MODEL


DONE = {
    "created": "made",
    "updated": "last changed",
    "loaded": "loaded",
    "uploaded": "uploaded",
    "submitted": "submitted",
    "validated": "validated",
    "confirmed": "confirmed",
    "published": "published",
    "decided": "decided",
    "approved": "approved",
    "acknowledged": "acknowledged",
    "requested": "requested",
    "withdrawn": "withdrawn",
    "retired": "retired",
    "released": "released",
    "started": "started",
    "finished": "finished",
    "scanned": "scanned",
    "checked": "checked",
    "reviewed": "reviewed",
    "closed": "closed",
    "opened": "opened",
    "sent": "sent",
    "added": "added",
    "answered": "answered",
    "responded": "answered",
    "superseded": "superseded",
    "removed": "removed",
    "deleted": "deleted",
    "imported": "imported",
    "exported": "exported",
    "processed": "processed",
    "queued": "queued",
    "cancelled": "canceled",
    "canceled": "canceled",
    "lifted": "lifted",
    "placed": "placed",
    "held": "held",
    "verified": "verified",
    "set": "set",
    "ended": "ended",
    "changed": "changed",
    "posted": "posted",
    "registered": "registered",
    "recorded": "recorded",
    "detected": "detected",
    "owned": "owned",
}


PAST = {"withdrawn": "withdrew", "made": "made"}

# Columns with the same meaning wherever they appear ({noun}: the table's record in words).
COMMON = {
    "module": "The platform module it belongs to (budgets, claims, monthly, calculations, documents, ...).",
    "title": "The {noun}'s title, as shown on its pages.",
    "name": "The {noun}'s name, as shown on its pages.",
    "label": "The {noun}'s label, as shown on its pages.",
    "description": "What the {noun} is, in words.",
    "summary": "A short summary of the {noun}.",
    "text": "The {noun}'s text, as written.",
    "acc_version": "The avoided cost calculator (ACC) it is priced on, named by its year (e.g. 2024).",
    "run": "The calculation run the results were read from (its ID).",
    "program_name": "The program's name, as the run's outputs gave it.",
    "measures": "How many measure records were added up into the row.",
    "claims": "How many claim records were added up into the row.",
    "on_bill_financing": "The program's on-bill financing, in dollars (the ProgramCost row's OnBillFinancing field).",
    "rebates_and_incentives": (
        "The rebates and incentives of the program's measures, in dollars: NumUnits x the four unit incentive fields, added up."
    ),
    "first_year": "The first program year it covers.",
    "last_year": "The last program year it covers (blank: open-ended or the same as the first).",
    "start_year": "The first program year it applies to.",
    "end_year": "The last program year it applies to (blank: still in use).",
    "effective_from": "The day the {noun} takes effect.",
    "valid_from": "The first day the {noun} is in force.",
    "valid_to": "The last day the {noun} is in force (blank: still in force).",
    "starts": "The first day of the {noun}.",
    "ends": "The last day of the {noun}.",
    "file_name": "The file's name, as uploaded.",
    "filename": "The file's name, as uploaded.",
    "findings": "What the check found, as structured data (rule, place and message per finding).",
    "active": "Whether the {noun} is in use (not retired).",
    "message": "The plain-language message shown when the check fails.",
    "fix": "The suggested fix shown with the message.",
    "severity": "What a failure does: reject (the file or record is refused) or warning (shown, not blocking).",
    "rule_id": "The rule's stable ID (a content hash, the same in every rule set that has it).",
    "engine_release": "The ReCET release that priced it.",
    "pda_id": "Its ID in the Energy Division's Public Document Area (PDA), kept so legacy links resolve.",
    "target_sector": "The target sector of the impact profile (AC_Elec_Target_Sector).",
    "sector": "The eTRM sector (Res, Com, Ind, Ag, ...).",
    "bldg_type": "The eTRM building type (BldgType).",
    "bldg_vint": "The eTRM building vintage (BldgVint).",
    "bldg_hvac": "The eTRM building HVAC type (BldgHVAC).",
    "bldg_loc": "The eTRM building location, a climate zone (BldgLoc).",
    "tech_group": "The eTRM technology group (TechGroup).",
    "tech_type": "The eTRM technology type (TechType).",
    "tech_id": "The eTRM technology ID (TechID).",
    "norm_unit": "The eTRM normalizing unit the values are per (NormUnit), e.g. each or per ton.",
    "pa_lead": "The PA that leads the eTRM measure package.",
    "start_date": "The first day the eTRM measure version is in effect.",
    "end_date": "The last day the eTRM measure version is in effect (blank: open-ended).",
    "measure_version_id": "The eTRM measure version ID, e.g. SWHC052-02.",
    "author_name": "The author's name, as it was when they wrote it.",
}


def _generic(model, f) -> str:
    """A definition for a common column, from its name and type; "" when there is no rule for it."""
    noun, name = _noun(model), f.name
    m = re.fullmatch(r"(\w+?)_(at|on)", name)
    if m and m.group(1) in DONE and isinstance(f, models.DateField | models.DateTimeField):
        return f"When the {noun} was {DONE[m.group(1)]}" + ("." if not f.null else " (blank: not yet).")
    m = re.fullmatch(r"(\w+?)_by", name)
    if m and m.group(1) in DONE and _is_person(f):
        blank = "." if not f.null else " (blank: not yet, or done by the platform)."
        return f"The person who {PAST.get(m.group(1), DONE[m.group(1)])} the {noun}{blank}"
    if name in ("organisation", "organization") and f.is_relation:
        return f"The organization (program administrator or the CPUC) the {noun} belongs to."
    if name == "prg_id":
        return "The program's ID (PrgID), as filed: its key in the program registry."
    if name == "pa":
        return "The program administrator's code (PA), as filed."
    if name == "year":
        return "The program year."
    if name == "number":
        return f"The {noun}'s number, counting from 1."
    if name in ("notes", "note"):
        return f"Free-text notes on the {noun}."
    if name == "reason":
        return f"Why the {noun} was made, in the words of the person who made it."
    if name == "sha256":
        return "The SHA-256 hash of the file's bytes, which identifies its exact content."
    if name in ("size", "bytes"):
        return "The file's size in bytes."
    if name == "content_type":
        return "The file's media type, e.g. application/pdf."
    if name == "key":
        return "Where the file is kept in the platform's object storage."
    if name == "position":
        return "Its place in the order shown, counting from 0."
    m = re.fullmatch(r"(\w+?)_(reason|note)", name)
    if m and m.group(1) in DONE | {"retire": "retired", "decision": "decided", "release": "released", "review": "reviewed"}:
        what = {"retire": "retired", "decision": "decided", "release": "released", "review": "reviewed"}.get(m.group(1)) or DONE[m.group(1)]
        return f"Why the {noun} was {what}, or the note given when it was, in the words of the person who did it."
    if name in COMMON:
        return COMMON[name].format(noun=noun)
    if name.endswith("_sha256"):
        return f"The SHA-256 hash of the {name[:-7].replace('_', ' ')} file's bytes, which identifies its exact content."
    if name == "id":
        return f"The {noun}'s permanent identifier."
    if f.choices and name == "kind":
        return f"What sort of {noun} it is (the values below)."
    if name == "status" and f.choices:
        return f"Where the {noun} stands in its workflow (the values below)."
    if f.is_relation and f.related_model is not None:
        target, what = f.related_model, str(f.verbose_name)
        if _is_person(f):
            return f"The {what} of the {noun}: a person's user account."
        if what == str(target._meta.verbose_name):
            return f"The {what} the {noun} belongs to ({label(target)})."
        kind = str(target._meta.verbose_name)
        return f"The {what} of the {noun}, {'an' if kind[:1] in 'aeiou' else 'a'} {kind} ({label(target)})."
    return ""


def _help(f) -> str:
    """The field's help text, which was written for a form beside its label: a fragment ("e.g. D.25-08-034", "blank: every module")
    is given the label in front."""
    text = " ".join(str(f.help_text or "").split())
    if text and (text[0].islower() or text[0] in "({[0123456789" or text.startswith(("e.g.", "Blank"))):
        return f"{str(f.verbose_name)[:1].upper()}{str(f.verbose_name)[1:]}: {text}"
    return text


def _program_guide(model, f):
    """The program fields guide's entry for a program record's column (registry/program_fields.yaml): its definition and valid
    values, so the dictionary and the guide say the same."""
    if label(model) not in ("registry.Program", "registry.ProgramVersion"):
        return None
    from cedars.services.registry import field_guide

    e = field_guide.entry(f.name)
    return e if e is not None and e.definition else None


def data_type(f) -> str:
    if f.is_relation:
        return "reference"
    if isinstance(f, NUMBERS):
        return "number"
    for cls, name in DATA_TYPES.items():
        if isinstance(f, cls):
            return name
    return "text"


def describe(model, f, audience: str) -> dict:
    written = (definitions().get(label(model), {}).get("fields") or {}).get(f.name)
    guide = _program_guide(model, f)
    definition = " ".join(str(written or (guide.definition if guide else "") or _help(f)).split()) or _generic(model, f)
    if definition and not definition.endswith((".", ")", "?")):
        definition += "."
    definition = us(definition)
    who = audience
    if _is_person(f) and audience == PUBLIC:
        who = RESTRICTED
    target = f" -> {label(f.related_model)}" if f.is_relation and f.related_model is not None else ""
    out = {
        "name": f"{label(model)}.{f.name}",
        "kind": "record",
        "group": table_name(model),
        "definition": definition,
        "data_type": data_type(f),
        "max_length": getattr(f, "max_length", None) if isinstance(f, models.CharField) else None,
        "source": f"platform record: table {model._meta.db_table}, column {f.column}{target}",
        "version": "",
        "audience": who,
        "aliases": [],
        "usage": [],
        "values": [[str(v), str(t)] for v, t in f.flatchoices] if f.choices else [[v.code, v.label] for v in guide.values] if guide else [],
    }
    if isinstance(f, models.DecimalField):
        out["rounding"] = f"stored to {f.decimal_places} decimal places"
    return out


def catalog(version: str | None = None) -> list[dict]:
    """Every described table's columns as catalog fields (the payload format load_catalog reads)."""
    import cedars

    version = version or f"CEDARS platform {cedars.__version__}"
    found = {label(m): m for m in our_models()}
    out = []
    for name, audience in TABLES.items():
        model = found[name]
        for f in columns(model):
            field = describe(model, f, audience)
            field["version"] = version
            out.append(field)
    return out


def table_definition(name: str) -> str:
    """The table's description: the written one, else the model's docstring."""
    written = definitions().get(name, {}).get("table")
    if written:
        return written
    model = apps.get_model(name)
    return " ".join((model.__doc__ or "").split())


def undescribed() -> list[str]:
    """Models in neither TABLES nor NOT_DESCRIBED (a new model the dictionary has not decided on)."""
    return sorted(label(m) for m in our_models() if label(m) not in TABLES and label(m) not in NOT_DESCRIBED)


def missing_definitions() -> list[str]:
    return [f["name"] for f in catalog() if not f["definition"]]


def stale_definitions() -> list[str]:
    """Written definitions for a table or column that no longer exists."""
    found = {label(m): m for m in our_models()}
    out = []
    for table, entry in definitions().items():
        if table not in TABLES or table not in found:
            out.append(table)
            continue
        names = {f.name for f in columns(found[table])}
        out += [f"{table}.{c}" for c in (entry.get("fields") or {}) if c not in names]
    return out
