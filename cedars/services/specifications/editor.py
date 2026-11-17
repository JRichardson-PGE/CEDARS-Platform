"""Editing filing specifications: a CPUC system administrator starts a draft from a published version, changes its settings, its
tables' fields (renaming one keeps it the same field: its old name is still accepted, its rules follow it and the data dictionary
renames it when the version is published; a new field with new codes gets a new value list made in the draft, `new_value_list`, which
is published with the version), its rules and which built-in checks run, reads what changes, and publishes it with notice.

A draft is a copy of a published version (`start_draft`), one per specification at a time. Its rule set is a draft copy of the
version's rule set, made in the rule library at the same time and published with it: rules change with a specification version,
never on their own date. Every change is audited with its reason.

Some fields cannot be removed, renamed or given another type, and a required one cannot be made optional (`locks`): the fields the
engine reads to price a record (Measure and ProgramCost; the installed engine's list), the fields the filing modules' own code reads
(each declares them, `register_locked_fields`), each table's key and the columns an upload is refused without.

Publishing (`publish`) runs the checks a version must pass (`problems`), fixes the day it takes effect (at least the release notes'
notice after today, unless a reason is given), publishes its rule set and a release note with the version, and freezes it. New filing
sets are set up with the newest version in effect (`service.current`).

Before publishing, a draft may be proposed for comment (`propose`, optional): it is frozen and shown to anyone signed in, who may check
a file against it and comment on it (comments.py) until its comment period closes (`change_comment_period`, `end_comments`). Staff
return it to draft to act on the comments (`return_to_draft`) and may propose it again, or publish it as proposed once comments have
closed."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone
from django.utils.text import slugify

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import service
from .models import CheckSetting, ExampleSet, SpecField, SpecificationVersion, SpecTable
from .service import FIELD_ATTRS, SpecificationError

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
TEXT_TYPES = ("text", "phone", "email")
EDITABLE = ["requirement", "type", "max_length", "decimals", "value_list", "default", "example", "unit", "definition", "guidance"]
AREA = {"claims": "claims", "budgets": "budgets", "monthly": "monthly"}  # the release notes' part of CEDARS, by module
DICTIONARY_TYPES = {"text": "text", "number": "number", "boolean": "flag", "year_quarter": "year_quarter", "date": "date"}


class EditError(SpecificationError):
    """A change the editor does not allow; the message says why and what to do."""


# --- who


def can_manage(user) -> bool:
    """Start and edit drafts: CPUC system administrators."""
    return bool(getattr(user, "is_authenticated", False)) and access.is_system_admin(user)


def can_publish(user) -> bool:
    """Publish a draft: CPUC system administrators (one is enough; no second approver)."""
    return can_manage(user)


def _require(actor, publishing: bool = False) -> None:
    if actor is not None and not (can_publish(actor) if publishing else can_manage(actor)):
        raise AccessDenied("Only a CPUC system administrator changes filing specifications.")


def _draft(version: SpecificationVersion) -> SpecificationVersion:
    v = SpecificationVersion.objects.select_for_update().select_related("specification").get(pk=version.pk)
    if v.status == SpecificationVersion.Status.PROPOSED:
        raise EditError(f"{v.name} is proposed for comment and does not change: return it to draft to change it.")
    if v.status != SpecificationVersion.Status.DRAFT:
        raise EditError("A published version never changes: start a new version from it.")
    return v


def draft_of(specification) -> SpecificationVersion | None:
    """The specification's draft (or proposed version): there is one at a time."""
    return (
        SpecificationVersion.objects.filter(specification=specification, status__in=["draft", "proposed"])
        .select_related("specification", "based_on", "rule_set")
        .first()
    )


# --- hooks


_locked: list[tuple[str, dict[str, dict[str, str]]]] = []
_releases = []


def register_locked_fields(module: str, fields: dict[str, dict[str, str]]) -> None:
    """Called by a filing module (or the PII vault) in its AppConfig.ready() with the fields its code reads, {table: {field: why}}: a
    draft of the module's specification cannot remove, rename or retype them, nor make a required one optional. Registering the same
    module again adds to its list (the vault adds the claims PII tables). tests/test_specification_editor.py fails on a field name the
    module's code names that its list leaves out."""
    _locked.append((module, {t: dict(fs) for t, fs in fields.items()}))


def register_engine_releases(lister) -> None:
    """Called by the Calculations module with a function returning the engine releases it can run, [{"release", "active", "default",
    "accs"}]: a version names one of the active ones (blank: the default)."""
    _releases[:] = [lister]


def engine_releases() -> list[dict]:
    return list(_releases[0]()) if _releases else []


def declared_locks(module: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for m, fields in _locked:
        if m == module:
            for t, fs in fields.items():
                out.setdefault(t, {}).update(fs)
    return out


# --- locks


ENGINE_WHY = "The engine reads it to price a record."


def _engine_columns() -> dict[str, frozenset[str]]:
    """{table: the columns the installed engine reads to price a record}, lower case; {} without the engine."""
    try:
        from recet import editor as engine_editor
        from recet.fieldnames import recet_name
    except ImportError:  # pragma: no cover - depends on the environment
        return {}
    out = {}
    for table, cols in engine_editor.CALC_COLUMNS.items():
        names = set(cols) | set(engine_editor.MEASURE_FROZEN if table == "Measure" else engine_editor.PROGRAMCOST_FROZEN)
        out[table] = frozenset({n.lower() for n in names} | {recet_name(n).lower() for n in names})
    return out


def engine_available() -> bool:
    return bool(_engine_columns())


def locks(version) -> dict[tuple[str, str], str]:
    """{(table, field): why it is locked} for a version's fields."""
    engine = _engine_columns()
    declared = declared_locks(version.specification.module)
    out: dict[tuple[str, str], str] = {}
    for t in version.tables.prefetch_related("fields"):
        cols = engine.get(t.name, frozenset())
        for f in t.fields.all():
            why = []
            if f.name in t.keys:
                why.append(f"It is the {t.name} table's key.")
            if f.name in t.refuse_without:
                why.append("An upload is refused without it.")
            if f.name in declared.get(t.name, {}):
                why.append(declared[t.name][f.name])
            if cols and ({f.name.lower(), *(o.lower() for o in f.other_names)} & cols):
                why.append(ENGINE_WHY)
            if why:
                out[(t.name, f.name)] = " ".join(dict.fromkeys(why))
    return out


# --- starting and discarding a draft


def _rule_set_date(code: str) -> date:
    """A free date for a draft rule set of `code`: after the newest published one, at least the usual notice ahead. The date is set
    again to the version's when it is published."""
    from cedars.services.releases import service as releases
    from cedars.services.rules.models import RuleSet

    day = timezone.localdate() + timedelta(days=releases.notice_days())
    newest = RuleSet.objects.filter(code=code, status=RuleSet.Status.PUBLISHED).aggregate(d=Max("effective_from"))["d"]
    if newest and day <= newest:
        day = newest + timedelta(days=1)
    taken = set(RuleSet.objects.filter(code=code, effective_from__gte=day).values_list("effective_from", flat=True))
    while day in taken:
        day += timedelta(days=1)
    return day


def _check_name(spec, name: str, exclude=None) -> tuple[str, str]:
    name = (name or "").strip()
    if not name:
        raise EditError("Give the version a name: the CPUC's name for it, such as the claim year it is for.")
    if len(name) > 120:
        raise EditError("Keep the name to 120 characters.")
    slug = slugify(name)
    if not slug:
        raise EditError("Use letters or digits in the name: its web address is made from them.")
    others = SpecificationVersion.objects.filter(specification=spec)
    if exclude is not None:
        others = others.exclude(pk=exclude.pk)
    if others.filter(name__iexact=name).exists():
        raise EditError(f"{spec.name} already has a version called {name}.")
    if others.filter(slug=slug).exists():
        raise EditError(f"{spec.name} already has a version whose web address would be the same ({slug}); choose another name.")
    return name, slug


@transaction.atomic
def start_draft(actor, based_on: SpecificationVersion, name: str, reason: str) -> SpecificationVersion:
    """A draft copy of a published version, with a draft copy of its rule set in the rule library."""
    _require(actor)
    based_on = SpecificationVersion.objects.select_related("specification", "rule_set").get(pk=based_on.pk)
    spec = based_on.specification
    if based_on.status != SpecificationVersion.Status.PUBLISHED:
        raise EditError("A draft is copied from a published version.")
    existing = draft_of(spec)
    if existing is not None:
        what = "a proposed version" if existing.status == SpecificationVersion.Status.PROPOSED else "a draft"
        raise EditError(f"{spec.name} already has {what}, {existing.name}: finish or discard it first.")
    if not (reason or "").strip():
        raise EditError("Say why the new version is made: the reason is kept with it.")
    name, slug = _check_name(spec, name)
    rule_set = None
    if based_on.rule_set_id is not None:
        from cedars.services.rules import service as rules_service

        rule_set = rules_service.new_version(
            actor,
            based_on.rule_set,
            _rule_set_date(based_on.rule_set.code),
            f"The rules of the draft {spec.name} version {name}: {reason.strip()}",
        )
    v = SpecificationVersion.objects.create(
        specification=spec,
        name=name,
        slug=slug,
        status=SpecificationVersion.Status.DRAFT,
        summary=based_on.summary,
        source=f"Copied from {based_on.name}",
        based_on=based_on,
        rule_set=rule_set,
        rule_set_code=based_on.rule_set_code,
        engine_release=based_on.engine_release,
        cost_test_set=based_on.cost_test_set,
        file_format=based_on.file_format,
        reason=reason.strip(),
        created_by=actor,
    )
    for t in based_on.tables.prefetch_related("fields"):
        nt = SpecTable.objects.create(version=v, position=t.position, **{k: getattr(t, k) for k in service.TABLE_ATTRS if k != "fields"})
        SpecField.objects.bulk_create(
            SpecField(table=nt, position=f.position, **{k: getattr(f, k) for k in FIELD_ATTRS}) for f in t.fields.all()
        )
    CheckSetting.objects.bulk_create(
        CheckSetting(version=v, position=c.position, **{k: getattr(c, k) for k in service.CHECK_ATTRS}) for c in based_on.checks.all()
    )
    ExampleSet.objects.bulk_create(
        ExampleSet(version=v, position=e.position, name=e.name, slug=e.slug, about=e.about, rows=e.rows) for e in based_on.examples.all()
    )
    _sync_checks(v)
    if not v.examples.exists():  # CEDARS writes the first example set; it is checked on the Examples page and before publishing
        from . import examples

        ExampleSet.objects.create(
            version=v, position=0, name="Generated by CEDARS", slug=examples.GENERATED, about=examples.GENERATED_ABOUT,
            rows=examples.generate_rows(v), generated=True,
        )  # fmt: skip
    audit.record(
        "specification.draft_started",
        obj=v,
        after={"specification": spec.code, "name": name, "based_on": based_on.name, "rule_set": rule_set.pk if rule_set else None},
        reason=reason,
        actor=actor,
    )
    return v


@transaction.atomic
def discard(actor, draft: SpecificationVersion, reason: str = "") -> None:
    """Drop a draft and its draft rule set. Nothing a filing uses changes."""
    _require(actor)
    v = _draft(draft)
    rs = v.rule_set
    audit.record(
        "specification.draft_discarded",
        obj=v,
        before={"specification": v.specification.code, "name": v.name, "based_on": v.based_on.name if v.based_on else None},
        reason=reason,
        actor=actor,
    )
    for vl in lists_made(v):
        _drop_list(actor, vl, f"Its specification draft was discarded. {reason}".strip())
    v.delete()
    if rs is not None and rs.status == rs.Status.DRAFT and not rs.specification_versions.exists():
        from cedars.services.rules import service as rules_service

        rules_service.discard_draft(actor, rs, f"Its specification draft was discarded. {reason}".strip())


# --- settings


SETTINGS = ["name", "slug", "summary", "engine_release", "cost_test_set"]


def cost_test_sets():
    from cedars.services.refdata.models import PackVersion

    return PackVersion.objects.filter(kind=PackVersion.Kind.COST_TESTS, status=PackVersion.Status.PUBLISHED).order_by(
        "label", "-published_at"
    )


def release_problem(release: str) -> str:
    """Why a version cannot name engine release `release` ("" = it can; blank names the default release)."""
    release = (release or "").strip()
    if not release:
        return ""
    known = {r["release"]: r for r in engine_releases()}
    if release not in known:
        return f"ReCET {release} is not a registered engine release: register it on Engine releases first."
    if not known[release].get("active", True):
        return f"ReCET {release} is retired: return it to service on Engine releases or choose another release."
    return ""


@transaction.atomic
def update_settings(
    actor, draft: SpecificationVersion, *, name: str, summary: str, engine_release: str, cost_test_set=None, reason: str = ""
) -> SpecificationVersion:
    """A draft's name (its web address follows), its summary, the engine release and the cost-test set that price its filings."""
    _require(actor)
    v = _draft(draft)
    before = audit.snapshot(v, SETTINGS)
    v.name, v.slug = _check_name(v.specification, name, exclude=v)
    v.summary = (summary or "").strip()
    problem = release_problem(engine_release)
    if problem:
        raise EditError(problem)
    v.engine_release = (engine_release or "").strip()
    if cost_test_set is not None and not cost_test_sets().filter(pk=cost_test_set.pk).exists():
        raise EditError("Choose a published cost-test set.")
    v.cost_test_set = cost_test_set
    after = audit.snapshot(v, SETTINGS)
    if after != before:
        v.save()
        audit.record("specification.settings_changed", obj=v, before=before, after=after, reason=reason, actor=actor)
        lists_made(v).update(made_for=made_for(v)[:200])  # the lists made in it are named for it
    return v


# --- fields


def value_list_names() -> list[str]:
    """The value lists a field may name: those the platform keeps versions of, the engine's lists and the platform's own."""
    from cedars.services.dictionary import value_lists
    from cedars.services.dictionary.models import ValueList

    names = set(ValueList.objects.values_list("name", flat=True)) | set(value_lists.platform_lists())
    try:
        names |= set(value_lists.engine_lists())
    except value_lists.EngineMissing:
        pass
    return sorted(names)


def _names_in(table: SpecTable, exclude: SpecField | None = None) -> dict[str, str]:
    """{name, lower case: the field holding it} of every name and accepted name in a draft table."""
    out = {}
    for f in table.fields.all():
        if exclude is not None and f.pk == exclude.pk:
            continue
        for n in (f.name, *f.other_names):
            out[n.lower()] = f.name
    return out


def _clean(attrs: dict, table: SpecTable, field: SpecField | None = None) -> dict:
    """Check a field's attributes as an editor gives them and return them clean."""
    out = {k: attrs[k] for k in EDITABLE if k in attrs}
    for k in ("default", "example", "unit", "definition", "guidance", "value_list"):
        if k in out:
            out[k] = (out[k] or "").strip()
    requirement = out.get("requirement", field.requirement if field else "optional")
    if requirement not in ("required", "optional") and not (field is not None and requirement == field.requirement == "system"):
        raise EditError("A field is required or optional. A field CEDARS fills in stays that way.")
    kind = out.get("type", field.type if field else "text")
    if kind not in SpecField.Type.values:
        raise EditError("Choose the field's type from the list.")
    if kind not in TEXT_TYPES:
        out["max_length"] = None
    elif "max_length" in out and out["max_length"] is not None and out["max_length"] < 1:
        raise EditError("The most characters is at least 1 (or blank for any length).")
    if kind != "number":
        out["decimals"] = None
    if out.get("value_list") and out["value_list"] not in value_list_names():
        raise EditError(f"There is no value list called {out['value_list']}.")
    if "other_names" in attrs:
        names = []
        for n in attrs["other_names"] or []:
            n = n.strip()
            if not n:
                continue
            if not NAME.match(n) or len(n) > 80:
                raise EditError(f"{n} is not a column name: use letters, digits and underscores, starting with a letter.")
            if n.lower() not in (x.lower() for x in names):
                names.append(n)
        taken = _names_in(table, exclude=field)
        own = field.name.lower() if field is not None else (attrs.get("name") or "").lower()
        for n in names:
            if n.lower() == own:
                raise EditError(f"{n} is the field's own name.")
            if n.lower() in taken:
                raise EditError(f"The {table.name} table already accepts {n}, as {taken[n.lower()]}.")
        out["other_names"] = names
    example = out.get("example", field.example if field else "")
    probe = {**({k: getattr(field, k) for k in FIELD_ATTRS} if field else service.field_doc("")), **out}
    if example:
        problem = service.value_problem(probe, example)
        if problem:
            raise EditError(f"The example value {example!r} is not valid: {problem}")
    return out


def _renumber(table: SpecTable, order: list[int]) -> None:
    """Give the table's fields the positions of `order` (field ids), in two steps since a position is unique."""
    SpecField.objects.filter(table=table).update(position=F("position") + 10000)
    for i, pk in enumerate(order):
        SpecField.objects.filter(pk=pk).update(position=i)


def _place(table: SpecTable, field: SpecField, position: int | None) -> None:
    order = [f.pk for f in table.fields.order_by("position") if f.pk != field.pk]
    if position is None:
        position = len(order)
    position = max(0, min(position, len(order)))
    order.insert(position, field.pk)
    _renumber(table, order)


def _field_snapshot(f: SpecField) -> dict:
    return {k: getattr(f, k) for k in ("position", *FIELD_ATTRS)}


def _table(draft: SpecificationVersion, table_name: str) -> SpecTable:
    t = draft.tables.filter(name=table_name).first()
    if t is None:
        raise EditError(f"This version has no {table_name} table.")
    return t


def dictionary_attrs(name: str) -> dict | None:
    """A field's attributes as the data dictionary gives them (its canonical name, definition, type, length, unit and who may see it),
    or None for a name it does not know."""
    from cedars.services.dictionary import service as dictionary

    f = dictionary.resolve(name)
    if f is None:
        return None
    return {
        "name": f.name,
        "definition": f.definition,
        "type": DICTIONARY_TYPES.get(f.data_type, "text"),
        "max_length": f.max_length if DICTIONARY_TYPES.get(f.data_type, "text") == "text" else None,
        "unit": f.unit[:80],
        "audience": f.audience,
        "value_list": "",
    }


@transaction.atomic
def add_field(actor, draft: SpecificationVersion, table_name: str, name: str, *, position=None, reason: str = "", **attrs) -> SpecField:
    """A new field in a draft's table: a data dictionary field (its definition, type and length come with it unless given) or a new
    one with its definition. A confidential (PII) dictionary field goes only in a vault table."""
    _require(actor)
    v = _draft(draft)
    table = _table(v, table_name)
    name = (name or "").strip()
    known = dictionary_attrs(name) if name else None
    if known is not None:
        if known["audience"] == "confidential" and not table.vault:
            raise EditError(f"{known['name']} holds personal information: it goes only in a table kept in the PII vault.")
        name = known["name"]  # under the dictionary's own name, whichever of its names was given
        for k in ("definition", "type", "max_length", "unit"):
            if not attrs.get(k) and attrs.get(k) != 0:
                attrs[k] = known[k]
    if not NAME.match(name) or len(name) > 80:
        raise EditError("A field's name is letters, digits and underscores, starting with a letter (80 characters at most).")
    taken = _names_in(table)
    if name.lower() in taken:
        raise EditError(
            f"The {table.name} table already has {name}" + (f" (as {taken[name.lower()]})." if taken[name.lower()] != name else ".")
        )
    if not (attrs.get("definition") or "").strip():
        raise EditError("Say what the field holds: its definition is shown to filers.")
    clean = _clean({**attrs, "name": name}, table)
    f = SpecField(table=table, position=10000 + table.fields.count(), name=name, **{k: v for k, v in clean.items()})
    f.save()
    _place(table, f, position)
    f.refresh_from_db()
    audit.record("specification.field_added", obj=v, after={"table": table.name, **_field_snapshot(f)}, reason=reason, actor=actor)
    return f


def _lock_problem(lock: str, field: SpecField, clean: dict) -> str:
    if not lock:
        return ""
    if clean.get("type", field.type) != field.type:
        return f"{field.name} is locked, so its type stays {field.get_type_display()}: {lock}"
    if field.requirement == "required" and clean.get("requirement", "required") != "required":
        return f"{field.name} is locked, so it stays required: {lock}"
    return ""


@transaction.atomic
def change_field(actor, field: SpecField, *, position=None, reason: str = "", **attrs) -> SpecField:
    """Change a draft field's requirement, type, size, value list, default, example, unit, definition, guidance, other accepted names
    or place in the template. A locked field keeps its type, and a required locked field stays required."""
    _require(actor)
    f = SpecField.objects.select_for_update().select_related("table__version__specification").get(pk=field.pk)
    v = _draft(f.table.version)
    clean = _clean(attrs, f.table, f)
    problem = _lock_problem(locks(v).get((f.table.name, f.name), ""), f, clean)
    if problem:
        raise EditError(problem)
    before = _field_snapshot(f)
    for k, value in clean.items():
        setattr(f, k, value)
    f.save()
    if position is not None and position != f.position:
        _place(f.table, f, position)
        f.refresh_from_db()
    after = _field_snapshot(f)
    if after != before:
        audit.record("specification.field_changed", obj=v, before={"table": f.table.name, **before}, after={"table": f.table.name, **after},
                     reason=reason, actor=actor)  # fmt: skip
    return f


@transaction.atomic
def order_fields(actor, table: SpecTable, names: list[str], reason: str = "") -> SpecTable:
    """Put a draft table's fields in the order of `names` (its fields' names; any left out keep their order after them), the order of
    the template's columns. One audit event for the whole order."""
    _require(actor)
    t = SpecTable.objects.select_for_update().select_related("version__specification").get(pk=table.pk)
    v = _draft(t.version)
    fields = list(t.fields.order_by("position"))
    by_name = {f.name.lower(): f for f in fields}
    order = []
    for n in names:
        f = by_name.get((n or "").lower())
        if f is not None and f not in order:
            order.append(f)
    order += [f for f in fields if f not in order]
    before, after = [f.name for f in fields], [f.name for f in order]
    if after != before:
        _renumber(t, [f.pk for f in order])
        audit.record("specification.fields_reordered", obj=v, before={"table": t.name, "order": before},
                     after={"table": t.name, "order": after}, reason=reason, actor=actor)  # fmt: skip
    return t


@transaction.atomic
def remove_field(actor, field: SpecField, reason: str = "") -> None:
    """Take a field out of a draft. A file that still has the column is accepted and the column ignored with a notice; data filed under
    earlier versions keeps it."""
    _require(actor)
    f = SpecField.objects.select_for_update().select_related("table__version__specification").get(pk=field.pk)
    v = _draft(f.table.version)
    lock = locks(v).get((f.table.name, f.name))
    if lock:
        raise EditError(f"{f.name} is locked and cannot be removed: {lock}")
    if not (reason or "").strip():
        raise EditError("Say why the field is removed: the reason is kept with the version.")
    table = f.table
    before = {"table": table.name, **_field_snapshot(f)}
    f.delete()
    _renumber(table, [x.pk for x in table.fields.order_by("position")])
    audit.record("specification.field_removed", obj=v, before=before, reason=reason, actor=actor)


@transaction.atomic
def rename_field(actor, field: SpecField, new_name: str, reason: str = "") -> SpecField:
    """Rename a draft's field: one field under a new name, not one removed and another added. The old name becomes one of its other
    accepted names (a file in the old layout still uploads), the draft's rules that name it are rewritten to the new name, and when the
    version is published the data dictionary renames the field too, keeping the old name as a former name. A locked field's name is
    fixed (the engine or CEDARS' code reads it by that name)."""
    from cedars.services.dictionary import service as dictionary

    _require(actor)
    f = SpecField.objects.select_for_update().select_related("table__version__specification").get(pk=field.pk)
    v = _draft(f.table.version)
    lock = locks(v).get((f.table.name, f.name))
    if lock:
        raise EditError(f"{f.name} is locked, so its name is fixed: {lock}")
    if not (reason or "").strip():
        raise EditError("Say why the field is renamed: the reason is kept with the version.")
    old, new = f.name, (new_name or "").strip()
    if not NAME.match(new) or len(new) > 80:
        raise EditError("A field's name has letters, digits and underscores, starting with a letter.")
    if new == old:
        raise EditError(f"The field is already called {old}.")
    taken = _names_in(f.table, exclude=f)
    if new.lower() in taken:
        raise EditError(f"{taken[new.lower()]} in the {f.table.name} table is already called or accepted as {new}.")
    mine, other = dictionary.resolve(old), dictionary.resolve(new)
    if other is not None and (mine is None or other.identity != mine.identity) and new.lower() != (f.renamed_from or "").lower():
        raise EditError(f"The data dictionary already has a field called {other.name}: add that field instead of renaming this one.")
    before = {"table": f.table.name, "name": old, "other_names": list(f.other_names)}
    if new.lower() == (f.renamed_from or "").lower():  # back to the name it had: the in-between name was never filed
        f.renamed_from = ""
        f.other_names = [n for n in f.other_names if n.lower() not in (old.lower(), new.lower())]
    else:
        f.renamed_from = f.renamed_from or old
        f.other_names = [n for n in f.other_names if n.lower() != new.lower()]
        if old.lower() not in (n.lower() for n in f.other_names):
            f.other_names.append(old)
    f.name = new
    f.save()
    rewritten = 0
    if v.rule_set_id is not None and v.rule_set.status == v.rule_set.Status.DRAFT:
        from cedars.services.rules import service as rules_service

        rewritten = rules_service.rename_field(
            actor, v.rule_set, f.table.name, old, new, f"{f.table.name}.{old} is renamed {new}: {reason}"
        )
    audit.record("specification.field_renamed", obj=v, before=before,
                 after={"table": f.table.name, "name": new, "other_names": list(f.other_names), "rules_rewritten": rewritten},
                 reason=reason, actor=actor)  # fmt: skip
    return f


@dataclass(frozen=True)
class PendingRename:
    """A field the data dictionary has renamed (in another specification's version) that a draft still has under its old name."""

    table: str
    field: str
    new_name: str
    by: str  # the specification version that renamed it


def pending_renames(draft: SpecificationVersion) -> list[PendingRename]:
    """The draft's fields under a name the data dictionary has since renamed, for staff to rename here too (or keep)."""
    from cedars.services.dictionary.models import FieldRename

    renames = {r.old_name.lower(): r for r in FieldRename.objects.all()}  # a later rename of the same name wins
    if not renames:
        return []
    out = []
    for t in draft.tables.prefetch_related("fields"):
        names = {f.name.lower() for f in t.fields.all()}
        for f in t.fields.all():
            r = renames.get(f.name.lower())
            if r is not None and r.new_name.lower() not in names:
                out.append(PendingRename(t.name, f.name, r.new_name, r.specification))
    return out


@transaction.atomic
def change_table(actor, table: SpecTable, *, other_names=None, guidance=None, reason: str = "") -> SpecTable:
    """A draft table's other accepted file or sheet names and its guidance for filers. Its key, how a later upload replaces its records
    and whether it is kept in the vault are CEDARS' processing, not editable."""
    _require(actor)
    t = SpecTable.objects.select_for_update().select_related("version__specification").get(pk=table.pk)
    v = _draft(t.version)
    before = {"other_names": list(t.other_names), "guidance": t.guidance}
    if other_names is not None:
        names = []
        others = {n.lower() for x in v.tables.exclude(pk=t.pk) for n in (x.name, *x.other_names)}
        for n in other_names:
            n = n.strip()
            if not n:
                continue
            if not re.match(r"^[A-Za-z][A-Za-z0-9_ \-]*$", n) or len(n) > 60:
                raise EditError(f"{n} is not a table name: use letters, digits, spaces, dashes and underscores.")
            if n.lower() in others:
                raise EditError(f"Another table of this version is already called {n}.")
            if n.lower() != t.name.lower() and n.lower() not in (x.lower() for x in names):
                names.append(n)
        t.other_names = names
    if guidance is not None:
        t.guidance = guidance.strip()
    after = {"other_names": list(t.other_names), "guidance": t.guidance}
    if after != before:
        t.save()
        audit.record("specification.table_changed", obj=v, before={"table": t.name, **before}, after={"table": t.name, **after},
                     reason=reason, actor=actor)  # fmt: skip
    return t


# --- new value lists


def made_in(v: SpecificationVersion) -> str:
    """How a value list made in draft `v` names it (dictionary ValueList.made_in)."""
    return f"specification:{v.pk}"


def made_for(v: SpecificationVersion) -> str:
    return f"{v.specification.name}: {v.name}"


def lists_made(v: SpecificationVersion):
    """The value lists made in draft `v` for its new fields, by name."""
    from cedars.services.dictionary.models import ValueList

    return ValueList.objects.filter(made_in=made_in(v)).order_by("name")


@dataclass(frozen=True)
class MadeList:
    """A value list made in a draft, as its pages show it: the list, its draft (None once published), its codes and the fields
    naming it."""

    value_list: object
    draft: object
    codes: int
    fields: list[str]


def made_lists(v: SpecificationVersion) -> list[MadeList]:
    from cedars.services.dictionary import value_lists

    named: dict[str, list[str]] = {}
    for t in v.tables.prefetch_related("fields"):
        for f in t.fields.all():
            if f.value_list:
                named.setdefault(f.value_list.lower(), []).append(f"{t.name}.{f.name}")
    out = []
    for vl in lists_made(v):
        d = value_lists.draft_of(vl)
        shown = d or value_lists.latest_published(vl)
        out.append(MadeList(vl, d, shown.codes.count() if shown else 0, named.get(vl.name.lower(), [])))
    return out


def read_codes(text: str) -> list[dict]:
    """Codes typed one to a line: the code, then (separated by commas) its description, first year and last year, each optional. A
    description with a comma goes in double quotes."""
    out = []
    for n, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip():
            continue
        parts = [p.strip() for p in next(csv.reader([line], skipinitialspace=True))]
        if len(parts) > 4:
            raise EditError(
                f"Line {n} has more than four parts: give the code, description, first year and last year, and put a description "
                "that has a comma in double quotes."
            )
        parts += [""] * (4 - len(parts))
        out.append({"code": parts[0], "description": parts[1], "start_year": parts[2], "end_year": parts[3], "line": n})
    return out


@transaction.atomic
def new_value_list(actor, field: SpecField, name: str, *, codes: str = "", reason: str = "") -> object:
    """A new value list for a field of a draft, for new codes no list has: it is made in the data dictionary (dictionary.value_lists
    .create_list) with the codes given, typed one to a line (`read_codes`; more can be added or loaded from a CSV on the list's draft
    page), and the field takes its codes from it. The engine has no such list, so the filing modules check the field against it
    themselves (their *-LIST checks). The list is published with the version, taking effect the same day, and dropped if the draft is
    discarded. Returns the list."""
    from cedars.services.dictionary import value_lists

    _require(actor)
    field = SpecField.objects.select_related("table__version__specification").get(pk=field.pk)
    v = _draft(field.table.version)
    lock = locks(v).get((field.table.name, field.name))
    if lock:
        raise EditError(f"{field.table.name}.{field.name} is locked, so its codes stay those CEDARS' processing reads: {lock}")
    if not (reason or "").strip():
        raise EditError("Say why the field needs a new value list: the reason is kept with the change.")
    rows = read_codes(codes)
    seen: dict[str, int] = {}
    for r in rows:
        if r["code"].lower() in seen:
            raise EditError(f"Line {r['line']}: {r['code']} is on line {seen[r['code'].lower()]} too (codes are matched ignoring case).")
        seen[r["code"].lower()] = r["line"]
    try:
        draft = value_lists.create_list(
            actor, (name or "").strip(), label=field.name, made_for=made_for(v), made_in=made_in(v), reason=reason
        )
        for r in rows:
            try:
                value_lists.add_code(
                    actor, draft, r["code"], description=r["description"], start_year=r["start_year"], end_year=r["end_year"],
                    reason=reason,
                )  # fmt: skip
            except value_lists.ValueListError as e:
                raise EditError(f"Line {r['line']}: {e}") from e
    except value_lists.ValueListError as e:
        raise EditError(str(e)) from e
    before = field.value_list
    field.value_list = draft.value_list.name
    field.save(update_fields=["value_list"])
    audit.record(
        "specification.value_list_made",
        obj=v,
        before={"table": field.table.name, "field": field.name, "value_list": before},
        after={"table": field.table.name, "field": field.name, "value_list": field.value_list, "codes": len(rows)},
        reason=reason,
        actor=actor,
    )
    return draft.value_list


def _drop_list(actor, vl, reason: str) -> None:
    from cedars.services.dictionary import value_lists

    value_lists.drop_unpublished(actor, vl, reason, with_specification=True)


def _publish_lists(actor, v: SpecificationVersion, effective_on: date) -> list[str]:
    """Publish the value lists made in the draft with it, taking effect the same day; one no field names any more is dropped."""
    from cedars.services.dictionary import value_lists

    named = {f.value_list.lower() for t in v.tables.all() for f in t.fields.all() if f.value_list}
    published = []
    for vl in lists_made(v):
        d = value_lists.draft_of(vl)
        if vl.name.lower() not in named:
            if d is not None:
                _drop_list(actor, vl, f"No field of {made_for(v)} names it any more.")
            continue
        if d is not None:
            value_lists.publish(actor, d, effective_on, f"Made for {made_for(v)}: {v.reason or v.summary}".strip(), with_specification=True)
            published.append(vl.name)
    return published


# --- built-in checks


@dataclass(frozen=True)
class CheckRow:
    """A built-in check on the draft's Built-in checks page: the check as CEDARS' code has it, and its row in the draft (None = the
    draft leaves it out)."""

    check: object  # rules.coded.CodedCheck
    setting: CheckSetting | None

    @property
    def runs(self) -> bool:
        return self.setting is not None or not self.check.optional

    @property
    def severity(self) -> str:
        return self.setting.severity if self.setting is not None else self.check.severity


def _groups_of(v: SpecificationVersion) -> list[str]:
    """The built-in check lists a version's filings run (claims and the vault's, budgets, monthly), from its rows and its base's."""
    names = set(v.checks.values_list("group", flat=True))
    if v.based_on_id is not None:
        names |= set(v.based_on.checks.values_list("group", flat=True))
    from cedars.services.rules import coded

    return [g.module for g in coded.groups() if g.module in names]


def check_rows(v: SpecificationVersion) -> list[tuple[str, str, list[CheckRow]]]:
    """[(group, label, rows)] of every check CEDARS' code has in the version's lists, with the version's row of each."""
    from cedars.services.rules import coded

    listed = {c.rule_id: c for c in v.checks.all()}
    out = []
    for name in _groups_of(v):
        g = coded.group(name)
        out.append((g.module, g.label, [CheckRow(c, listed.get(c.rule_id)) for c in g.checks]))
    return out


def _setting(v: SpecificationVersion, group: str, c, severity: str = "") -> CheckSetting:
    return CheckSetting(version=v, position=0, group=group, rule_id=c.rule_id, severity=severity or c.severity, tables=c.tables,
                        fields=c.fields, finds=c.finds, fix=c.fix)  # fmt: skip


def _reposition(v: SpecificationVersion) -> None:
    """The draft's checks in the order CEDARS' code lists them."""
    from cedars.services.rules import coded

    order = {c.rule_id: i for i, c in enumerate(c for g in coded.groups() for c in g.checks)}
    rows = sorted(v.checks.all(), key=lambda c: (order.get(c.rule_id, len(order)), c.position))
    for i, c in enumerate(rows):
        if c.position != i:
            c.position = i
            c.save(update_fields=["position"])


def _sync_checks(v: SpecificationVersion) -> None:
    """A new draft lists the checks CEDARS' code has now: a check added to the code since its base was published is added (it runs
    anyway unless it is optional), and one the code no longer has is dropped."""
    from cedars.services.rules import coded

    groups = _groups_of(v)
    known = {c.rule_id for name in groups for c in coded.group(name).checks}
    v.checks.filter(group__in=groups).exclude(rule_id__in=known).delete()
    listed = set(v.checks.values_list("rule_id", flat=True))
    CheckSetting.objects.bulk_create(
        _setting(v, name, c) for name in groups for c in coded.group(name).checks if c.rule_id not in listed and not c.optional
    )
    _reposition(v)


@transaction.atomic
def set_checks(actor, draft: SpecificationVersion, *, runs, severities: dict[str, str], reason: str = "") -> SpecificationVersion:
    """Which of the draft's optional built-in checks run (`runs`, rule IDs) and the severity of each that lets the version choose
    (`severities`, {rule ID: reject or warning}). The other checks are CEDARS' own processing: they always run as written."""
    _require(actor)
    v = _draft(SpecificationVersion.objects.select_for_update().get(pk=draft.pk))
    if not (reason or "").strip():
        raise EditError("Say why the built-in checks change: the reason is kept with the version.")
    runs = set(runs)
    before = {c.rule_id: c.severity for c in v.checks.all()}
    for group, _label, rows in check_rows(v):
        for row in rows:
            c = row.check
            severity = severities.get(c.rule_id, row.severity)
            if severity != row.severity and severity not in c.severities:
                raise EditError(f"{c.rule_id}'s severity is CEDARS' own and cannot change.")
            wanted = c.rule_id in runs or not c.optional
            if row.setting is None and wanted:
                _setting(v, group, c, severity).save()
            elif row.setting is not None and not wanted:
                row.setting.delete()
            elif row.setting is not None and row.setting.severity != severity:
                row.setting.severity = severity
                row.setting.save(update_fields=["severity"])
    _reposition(v)
    after = {c.rule_id: c.severity for c in v.checks.all()}
    if after != before:
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        audit.record(
            "specification.checks_changed",
            obj=v,
            before={k: before.get(k, "") for k in changed},
            after={k: after.get(k, "") for k in changed},
            reason=reason,
            actor=actor,
        )
    return v


def writable_as_rules(v: SpecificationVersion) -> list:
    """The built-in checks the draft runs that can be written as rules of its rule set instead (rules.coded `as_rules`)."""
    from cedars.services.rules import coded

    listed = set(v.checks.values_list("rule_id", flat=True))
    known = coded.by_id()
    return [known[r] for r in sorted(listed) if r in known and known[r].as_rules] if v.rule_set_id else []


@transaction.atomic
def write_as_rules(actor, draft: SpecificationVersion, rule_id: str, reason: str = "") -> list:
    """Replace a built-in check the draft runs with the rules that give its findings (`as_rules`), added to the draft's rule set with
    the severity the draft gives the check and its fix; the check then leaves the draft's list, so a filing on the version is checked
    by the rules, which the CPUC can change as data from then on. Filings on earlier versions keep the check. Returns the rules."""
    from cedars.services.rules import coded
    from cedars.services.rules import service as rules

    _require(actor)
    v = _draft(SpecificationVersion.objects.select_for_update().get(pk=draft.pk))
    if not (reason or "").strip():
        raise EditError("Say why the check is written as rules: the reason is kept with the version.")
    check = coded.by_id().get(rule_id)
    setting = v.checks.filter(rule_id=rule_id).first()
    if check is None or not check.as_rules:
        raise EditError(f"{rule_id} cannot be written as rules.")
    if setting is None:
        raise EditError(f"This draft does not run {rule_id}; tick it on Built-in checks first if its rules are wanted.")
    if v.rule_set_id is None:
        raise EditError("This draft has no rule set to write the rules in.")
    written = []
    for ar in check.as_rules:
        d = rules.rule_definition(ar.field, ar.rule, ar.value, [(*c, "") for c in ar.conds], ar.connector, ar.table)
        rid, _text = rules._id_and_text("rule", setting.severity, ar.table, ar.field, {**d, "field": ar.field})
        found = v.rule_set.rules.filter(rule_id=rid).first()
        if found is None:
            found = rules.add_rule(
                actor, v.rule_set, kind="rule", severity=setting.severity, table=ar.table, field=ar.field, definition=d, fix=check.fix,
                reason=f"{reason} (built-in check {rule_id} written as rules)",
            )  # fmt: skip
        written.append(found)
    severity = setting.severity
    setting.delete()
    audit.record(
        "specification.check_written_as_rules",
        obj=v,
        before={rule_id: severity},
        after={rule_id: "", "rules": [r.rule_id for r in written]},
        reason=reason,
        actor=actor,
    )
    return written


# --- the checks before publishing


@dataclass(frozen=True)
class Problem:
    """Something that stops a draft from being published: the part of the draft it is in (a tab), and what to do."""

    part: str  # settings, fields, rules, checks, examples, changes
    text: str


def problems(draft: SpecificationVersion) -> list[Problem]:
    """Why the draft cannot be published as it stands ([] = it can, once its date and note are given)."""
    out: list[Problem] = []
    v = draft
    if not v.summary.strip():
        out.append(Problem("settings", "Say what the version is and why it is made (its summary, shown at the top of its page)."))
    problem = release_problem(v.engine_release)
    if problem:
        out.append(Problem("settings", problem))
    if v.cost_test_set_id is not None and not cost_test_sets().filter(pk=v.cost_test_set_id).exists():
        out.append(Problem("settings", "The cost-test set it names is not published."))
    lists = set(value_list_names())
    unready = _unready_lists(v)
    for t in v.tables.prefetch_related("fields"):
        names = {f.name for f in t.fields.all()}
        for k in t.keys:
            if k not in names:
                out.append(Problem("fields", f"The {t.name} table's key {k} is not one of its fields."))
        for k in t.refuse_without:
            if k not in names:
                out.append(
                    Problem("fields", f"The {t.name} table's column {k}, which an upload is refused without, is not one of its fields.")
                )
        for f in t.fields.all():
            if f.value_list and f.value_list not in lists:
                out.append(Problem("fields", f"{t.name}.{f.name} names the value list {f.value_list}, which CEDARS does not have."))
            elif f.value_list and f.value_list.lower() in unready:
                why = unready[f.value_list.lower()]
                out.append(Problem("fields", f"{t.name}.{f.name} names the value list {f.value_list}, which {why}"))
            if f.example:
                p = service.value_problem(f, f.example)
                if p:
                    out.append(Problem("fields", f"{t.name}.{f.name}'s example value is not valid: {p}"))
    if v.based_on_id is not None:
        base = v.based_on
        base_locks = locks(base)
        draft_fields = {(t.name, f.name): f for t in v.tables.prefetch_related("fields") for f in t.fields.all()}
        base_fields = {(t.name, f.name): f for t in base.tables.prefetch_related("fields") for f in t.fields.all()}
        for key, why in base_locks.items():
            f, b = draft_fields.get(key), base_fields[key]
            if f is None:
                out.append(Problem("fields", f"{key[0]}.{key[1]} is locked and must stay in the version: {why}"))
            elif f.type != b.type or (b.requirement == "required" and f.requirement != "required"):
                out.append(Problem("fields", f"{key[0]}.{key[1]} is locked: its type and requirement stay as they were. {why}"))
        if service.compare(base, v).empty:
            out.append(Problem("changes", f"Nothing changes from {base.name}: there is nothing to publish."))
    out += _example_problems(v)
    if (
        v.rule_set_id is not None
        and v.rule_set.status != v.rule_set.Status.DRAFT
        and v.based_on_id
        and v.rule_set_id != v.based_on.rule_set_id
    ):
        out.append(Problem("rules", "Its rule set is already published on its own; start the draft again."))
    return out


def _example_problems(v: SpecificationVersion) -> list[Problem]:
    from . import examples

    sets = list(v.examples.all())
    if not sets:
        return [Problem("examples", "The version has no example set: generate one on the Examples page.")]
    if examples.stale(v):  # checked on the Examples and Publish pages and when it is published, as the draft then stands
        return []
    out = []
    for e in sets:
        n = len(examples.rejects(e.report))
        if n:
            words = f"{n} rejected finding{'s' if n != 1 else ''}"
            out.append(Problem("examples", f"The example {e.name} has {words}: change its records so it passes."))
    return out


def _unready_lists(v: SpecificationVersion) -> dict[str, str]:
    """{list name, lower case: what is wrong} of the value lists made for a specification that the draft cannot publish with: one of
    its own with no codes, or another draft's not yet published."""
    from cedars.services.dictionary import value_lists
    from cedars.services.dictionary.models import ValueList

    out = {}
    for vl in ValueList.objects.exclude(made_for=""):
        if value_lists.latest_published(vl) is not None:
            continue
        if vl.made_in == made_in(v):
            d = value_lists.draft_of(vl)
            if d is None or not d.codes.exists():
                out[vl.name.lower()] = "has no codes yet: add them on the list's draft page."
        else:
            out[vl.name.lower()] = f"is made for {vl.made_for} and is not published yet."
    return out


def notice_problem(effective_on: date | None, short_notice_reason: str = "", on: date | None = None) -> str:
    """Why `effective_on` is not a day the version may take effect ("" = it may): after today, with the release notes' notice unless a
    reason is given."""
    from cedars.services.releases import service as releases

    on = on or releases.today()
    if effective_on is None:
        return "Give the day it takes effect."
    if effective_on <= on:
        return "A version takes effect after the day it is published: choose a later day."
    days = (effective_on - on).days
    if days < releases.notice_days() and not (short_notice_reason or "").strip():
        return (
            f"It takes effect in {days} day{'s' if days != 1 else ''}, less than the usual {releases.notice_days()} days' notice: "
            "say why it cannot wait (the reason is shown with it)."
        )
    return ""


def earliest_day() -> date:
    """The first day a version published today takes effect with the usual notice."""
    from cedars.services.releases import service as releases

    return releases.today() + timedelta(days=releases.notice_days())


def change_lines(draft: SpecificationVersion) -> list[str]:
    """What changes from the version it was copied from, as plain sentences for its release note."""
    if draft.based_on_id is None:
        return []
    c = service.compare(draft.based_on, draft)
    out = []
    for t in c.tables_added:
        out.append(f"New table {t.name}.")
    for t in c.tables_removed:
        out.append(f"The {t.name} table is removed.")
    for table, f in c.fields_added:
        out.append(f"New field {table}.{f.name} ({f.get_requirement_display().lower()}).")
    for table, old, new, _diffs in c.fields_renamed:
        out.append(f"{table}.{old} is renamed {new}: a file with the old name still uploads, and data filed earlier keeps it.")
    for table, f in c.fields_removed:
        out.append(f"{table}.{f.name} is removed: a file that still has the column is accepted and the column ignored.")
    for table, name, diffs in [*c.fields_changed, *((t, new, d) for t, _old, new, d in c.fields_renamed if d)]:
        words = "; ".join(
            f"{w.lower()} {old or 'blank'} to {new or 'blank'}" for w, old, new in diffs if w not in ("Definition", "Guidance")
        )
        if words:
            out.append(f"{table}.{name}: {words}.")
        elif diffs:
            out.append(f"{table}.{name}: its definition or guidance is reworded.")
    if c.rules_added:
        out.append(f"{len(c.rules_added)} new rule{'s' if len(c.rules_added) != 1 else ''}.")
    if c.rules_removed:
        out.append(f"{len(c.rules_removed)} rule{'s' if len(c.rules_removed) != 1 else ''} removed.")
    if c.rules_renamed:
        n, which = len(c.rules_renamed), "fields" if len(c.fields_renamed) != 1 else "field"
        rules_words = "1 rule now names" if n == 1 else f"{n} rules now name"
        out.append(f"{rules_words} the renamed {which}; what {'it checks' if n == 1 else 'they check'} is unchanged.")
    if c.rules_changed:
        out.append(f"{len(c.rules_changed)} rule{'s' if len(c.rules_changed) != 1 else ''} changed.")
    for c_ in c.checks_added:
        out.append(f"Built-in check {c_.rule_id} added.")
    for c_ in c.checks_removed:
        out.append(f"Built-in check {c_.rule_id} no longer runs.")
    for a, b in c.checks_changed:
        if a.severity != b.severity and b.severity in ("reject", "warning"):
            out.append(f"Built-in check {a.rule_id} now {'rejects the record' if b.severity == 'reject' else 'only warns'}.")
        else:
            out.append(f"Built-in check {a.rule_id} changed.")
    for name, old, new in c.settings_changed:
        out.append(f"{name}: {old or 'none'} to {new or 'none'}.")
    for m in made_lists(draft):
        if m.fields:
            out.append(
                f"New value list {m.value_list.name} ({m.codes} code{'s' if m.codes != 1 else ''}) for {', '.join(m.fields)}: a value "
                "not in it is rejected."
            )
    return out


@transaction.atomic
def publish(
    actor, draft: SpecificationVersion, *, effective_on: date, short_notice_reason: str = "", changes: str = "", action: str = ""
) -> SpecificationVersion:
    """Publish a draft: it must pass `problems`, take effect after today with the usual notice (or a reason), and say what changes
    for its release note. Its rule set is published with it (taking effect the same day), the release note is published, and the
    version is frozen. Filing sets set up from `effective_on` on are set up with it; filings already set up keep theirs."""
    from cedars.services.releases import service as releases
    from cedars.services.rules import service as rules_service
    from cedars.services.rules.models import RuleSet

    _require(actor, publishing=True)
    v = SpecificationVersion.objects.select_for_update().get(pk=draft.pk)
    if (
        v.status == SpecificationVersion.Status.PROPOSED
    ):  # published as it was proposed: comments must have closed, then it is published as a draft would be
        if v.open_for_comment:
            raise EditError(
                f"Comments on {v.name} are open through {_day(v.comments_last_day)}: wait for them to close, or end the comment period "
                "first."
            )
        SpecificationVersion.objects.filter(pk=v.pk).update(status=SpecificationVersion.Status.DRAFT)
    v = _draft(draft)
    from . import examples

    if examples.stale(v):  # the examples are checked as the draft now stands
        examples.check_all(v)
    found = problems(v)
    if found:
        raise EditError(f"It cannot be published yet: {found[0].text}" + (f" (and {len(found) - 1} more)" if len(found) > 1 else ""))
    problem = notice_problem(effective_on, short_notice_reason)
    if problem:
        raise EditError(problem)
    from . import impact

    if impact.stale(v):  # the version keeps the impact test of the draft as it is published
        impact.run(actor, v)
    changes = (changes or "").strip() or " ".join(change_lines(v))
    if not changes:
        raise EditError("Say what changes, for the release note that announces the version.")
    rs = v.rule_set
    if rs is not None and rs.status == RuleSet.Status.DRAFT:
        clash = RuleSet.objects.filter(code=rs.code, effective_from=effective_on).exclude(pk=rs.pk).first()
        if clash is not None:
            raise EditError(f"The {clash} rule set already takes effect that day: choose another day.")
        if rs.effective_from != effective_on:
            RuleSet.objects.filter(pk=rs.pk).update(effective_from=effective_on)
            rs.refresh_from_db()
        rules_service.publish(actor, rs, with_specification=True)
    short = (short_notice_reason or "").strip()
    note = releases.create_draft(
        actor,
        title=f"{v.specification.name}: {v.name}",
        summary=(v.summary or f"A new version of the {v.specification.name} specification.").strip(),
        effective_on=effective_on,
    )
    releases.add_change(
        actor,
        note,
        kind="specification",
        area=AREA.get(v.specification.module, "reference"),
        text=changes,
        action=(action or "").strip()
        or f"Filing sets set up from {effective_on:%B} {effective_on.day}, {effective_on.year} use this version; download its template "
        "from its page.",
    )
    note = releases.publish(actor, note, short_notice_reason=short)
    _publish_lists(actor, v, effective_on)
    renames = list(dict.fromkeys((f.renamed_from, f.name) for t in v.tables.all() for f in t.fields.all() if f.renamed_from))
    if renames:  # the data dictionary renames them too, keeping each old name as a former name
        from cedars.services.dictionary import changes as dictionary_changes

        dictionary_changes.rename_fields(actor, renames, specification=f"{v.specification.name}: {v.name}", reason=v.reason)
    v.status = SpecificationVersion.Status.PUBLISHED
    v.published_at, v.published_by = timezone.now(), actor
    v.effective_on = effective_on
    v.short_notice_reason = note.short_notice_reason
    v.release_note = note
    v.fingerprint = service.fingerprint(service.as_document(v))
    v.save()
    audit.record(
        "specification.published",
        obj=v,
        after={
            "specification": v.specification.code,
            "name": v.name,
            "effective_on": effective_on.isoformat(),
            "fingerprint": v.fingerprint,
            "rule_set": rs.pk if rs else None,
            "release_note": note.pk,
            "changes": changes,
            "proposed": v.proposals,
        },
        reason=v.short_notice_reason,
        actor=actor,
    )
    return v


# --- proposing a draft for comment


def _day(d: date | None) -> str:
    return f"{d:%B} {d.day}, {d.year}" if d else ""


def _close_at(last_day: date):
    """The moment comments close: the start of the day after `last_day`, local time."""
    from datetime import datetime, time

    return timezone.make_aware(datetime.combine(last_day + timedelta(days=1), time.min))


def _last_day_problem(last_day: date | None) -> str:
    from cedars.services.releases import service as releases

    if last_day is None:
        return "Give the last day comments are taken."
    if last_day < releases.today():
        return "The last day for comments is today or later."
    return ""


def _proposed(version: SpecificationVersion) -> SpecificationVersion:
    v = SpecificationVersion.objects.select_for_update().select_related("specification").get(pk=version.pk)
    if v.status != SpecificationVersion.Status.PROPOSED:
        raise EditError(f"{v.name} is not proposed for comment.")
    return v


@transaction.atomic
def propose(actor, draft: SpecificationVersion, *, last_day: date, note: str = "") -> SpecificationVersion:
    """Propose a draft for comment: it is frozen (its fields, rules, checks and examples), shown to anyone signed in, who may run the
    pre-upload checker against it and comment on it through `last_day`. It must pass the checks before publishing first, so what filers
    read is what would be published. Staff return it to draft to act on the comments (`return_to_draft`), or publish it once comments
    have closed. Each proposal is a round of comments. Optional: a draft may be published without being proposed."""
    _require(actor)
    v = _draft(draft)
    from . import examples

    if examples.stale(v):
        examples.check_all(v)
    found = problems(v)
    if found:
        raise EditError(f"It cannot be proposed yet: {found[0].text}" + (f" (and {len(found) - 1} more)" if len(found) > 1 else ""))
    problem = _last_day_problem(last_day)
    if problem:
        raise EditError(problem)
    v.status = SpecificationVersion.Status.PROPOSED
    v.proposals += 1
    v.proposed_at, v.proposed_by = timezone.now(), actor
    v.proposal_note = (note or "").strip()
    v.comments_close_at = _close_at(last_day)
    v.save()
    audit.record(
        "specification.proposed",
        obj=v,
        after={"specification": v.specification.code, "name": v.name, "round": v.proposals, "last_day": _day(last_day),
               "note": v.proposal_note},
        reason=v.proposal_note,
        actor=actor,
    )  # fmt: skip
    return v


@transaction.atomic
def change_comment_period(actor, version: SpecificationVersion, *, last_day: date, reason: str) -> SpecificationVersion:
    """Move the last day comments are taken on a proposed version (to today or later)."""
    _require(actor)
    v = _proposed(version)
    if not (reason or "").strip():
        raise EditError("Say why the comment period changes: filers are told.")
    problem = _last_day_problem(last_day)
    if problem:
        raise EditError(problem)
    before = _day(v.comments_last_day)
    v.comments_close_at = _close_at(last_day)
    v.save(update_fields=["comments_close_at"])
    audit.record(
        "specification.comment_period_changed", obj=v, before={"last_day": before}, after={"last_day": _day(last_day)},
        reason=reason, actor=actor,
    )  # fmt: skip
    return v


@transaction.atomic
def end_comments(actor, version: SpecificationVersion, reason: str) -> SpecificationVersion:
    """End the comment period on a proposed version now, so it can be published or returned to draft without more comments."""
    _require(actor)
    v = _proposed(version)
    if not (reason or "").strip():
        raise EditError("Say why comments end early: the reason is kept with the version.")
    if not v.open_for_comment:
        raise EditError(f"Comments on {v.name} are already closed.")
    before = _day(v.comments_last_day)
    v.comments_close_at = timezone.now()
    v.save(update_fields=["comments_close_at"])
    audit.record(
        "specification.comment_period_changed", obj=v, before={"last_day": before}, after={"ended": v.comments_close_at.isoformat()},
        reason=reason, actor=actor,
    )  # fmt: skip
    return v


@transaction.atomic
def return_to_draft(actor, version: SpecificationVersion, reason: str) -> SpecificationVersion:
    """Return a proposed version to draft, to act on the comments: it can be changed again and proposed again (a new round of
    comments) or published. Its comments stay with it, and staff can still answer them."""
    _require(actor)
    v = _proposed(version)
    if not (reason or "").strip():
        raise EditError("Say why it goes back to draft, for example the comments it acts on.")
    v.status = SpecificationVersion.Status.DRAFT
    v.save(update_fields=["status"])
    audit.record(
        "specification.returned_to_draft", obj=v, after={"specification": v.specification.code, "name": v.name, "round": v.proposals},
        reason=reason, actor=actor,
    )  # fmt: skip
    return v
