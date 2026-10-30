"""Output templates (BR-06.10): which measure-level columns a run writes, and in what order. A template names fields of the published
field catalog (the dictionary service, ReCET's renamed field names, decision L): the Measure input columns a run echoes and the output
columns. An old name (a CET name or one ReCET used before) is saved under the field's canonical name, as the engine reads it.

A run given a template carries it in its job spec's settings (`output_template`, `output_template_name`), so the run record says exactly
which columns were asked for even if the template changes later. A column the run does not produce (an SCT column with the Societal Cost
Tests off) is left out of the file by the engine, which says so in the run's notes. Every change is an audit event."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.db.models import Q

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.dictionary import service as dictionary
from cedars.services.dictionary.models import Alias, Field, Usage

from .models import OutputTemplate, OutputTemplateDefault

INPUT_GROUP = "Input columns"
# Per-component value columns (ElecValue_<component>) depend on the ACC, so the catalog does not list them; a template may name them.
COMPONENT_PREFIXES = ("ElecValue_", "GasValue_")
MAX_NAME = 60


@dataclass
class Catalog:
    """The columns a template may name: [(group, [Field])] in the order a run writes them (inputs first), and every accepted name."""

    groups: list[tuple[str, list[Field]]] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)  # lower-case name or alias -> canonical name

    def __bool__(self):
        return bool(self.groups)

    def canonical(self, name: str) -> str | None:
        n = str(name).strip()
        if n.startswith(COMPONENT_PREFIXES) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n) and n not in COMPONENT_PREFIXES:
            return n
        return self.names.get(n.lower())


def catalog() -> Catalog:
    """The current field catalog's measure-level columns (empty when no catalog is published)."""
    version = dictionary.current()
    if version is None:
        return Catalog()
    inputs = [u.field for u in Usage.objects.filter(field__version=version, module="calculations", table="Measure").select_related("field")]
    outputs = list(Field.objects.filter(version=version, kind=Field.Kind.OUTPUT).order_by("pk"))
    groups: dict[str, list[Field]] = {INPUT_GROUP: inputs} if inputs else {}
    for f in outputs:
        groups.setdefault(f.group or "Other outputs", []).append(f)
    allowed = {f.pk: f.name for f in inputs + outputs}
    names = {n.lower(): n for n in allowed.values()}
    for a in Alias.objects.filter(field_id__in=allowed):
        names.setdefault(a.name.lower(), allowed[a.field_id])
    return Catalog(groups=list(groups.items()), names=names)


def parse(text: str) -> list[str]:
    """Column names from text: one per line or separated by commas; blank lines and '#' comments ignored."""
    out = []
    for line in str(text or "").splitlines():
        out += [c.strip() for c in line.split("#", 1)[0].split(",") if c.strip()]
    return out


def check(columns, cat: Catalog | None = None) -> tuple[list[str], list[str]]:
    """(the columns under their canonical names, in order and without repeats; the names that are not measure-level columns)."""
    cat = cat if cat is not None else catalog()
    ok, bad = [], []
    for c in columns:
        n = cat.canonical(c)
        if n is None:
            if str(c).strip() and c not in bad:
                bad.append(str(c).strip())
        elif n not in ok:
            ok.append(n)
    return ok, bad


# --- who sees and changes which


def visible(user):
    """The templates offered to `user`: everyone's (no owner) first, then their own."""
    return OutputTemplate.objects.filter(Q(owner__isnull=True) | Q(owner=user))


def can_change(user, template: OutputTemplate) -> bool:
    return template.owner_id == user.pk if template.owner_id else access.is_system_admin(user)


def _snapshot(t: OutputTemplate) -> dict:
    return {"name": t.name, "columns": list(t.columns), "shared": t.shared}


@transaction.atomic
def save(actor, name: str, columns, *, template: OutputTemplate | None = None, shared: bool = False) -> OutputTemplate:
    """Save a new template (the actor's own, or everyone's when `shared` and the actor is a CPUC system administrator) or change one
    the actor may change. Raises AccessDenied with the reason when it cannot be saved."""
    name = str(name or "").strip()
    if not name:
        raise AccessDenied("Give the template a name.")
    if len(name) > MAX_NAME:
        raise AccessDenied(f"A template name has at most {MAX_NAME} characters.")
    cat = catalog()
    if not cat:
        raise AccessDenied("No field catalog is published yet, so columns cannot be checked.")
    cols, bad = check(columns, cat)
    if bad:
        raise AccessDenied(f"Not measure-level columns: {', '.join(bad)}.")
    if not cols:
        raise AccessDenied("Pick at least one column.")
    if template is None:
        if shared and not access.is_system_admin(actor):
            raise AccessDenied("Only a CPUC system administrator adds templates offered to everyone.")
        template, before = OutputTemplate(owner=None if shared else actor), None
    else:
        if not can_change(actor, template):
            raise AccessDenied(
                "Only a CPUC system administrator changes a template offered to everyone; save your copy under another name."
                if template.shared
                else "This is someone else's template."
            )
        before = _snapshot(template)
    template.name, template.columns = name, cols
    try:
        with transaction.atomic():
            template.save()
    except IntegrityError:
        raise AccessDenied(f'There is already a template named "{name}".') from None
    audit.record(
        "output_template.changed" if before else "output_template.added",
        obj=template,
        before=before,
        after=_snapshot(template),
        actor=actor,
    )
    return template


@transaction.atomic
def delete(actor, template: OutputTemplate) -> None:
    if not can_change(actor, template):
        raise AccessDenied("You cannot delete this template.")
    audit.record("output_template.deleted", obj=template, before=_snapshot(template), actor=actor)
    template.delete()


def default_for(user) -> OutputTemplate | None:
    """The template `user`'s new runs start with, or None for every column."""
    d = OutputTemplateDefault.objects.filter(user=user).select_related("template").first()
    return d.template if d and visible(user).filter(pk=d.template_id).exists() else None


@transaction.atomic
def set_default(actor, template: OutputTemplate | None) -> None:
    """Make `template` (None: every column) the one the actor's new runs start with."""
    if template is not None and not visible(actor).filter(pk=template.pk).exists():
        raise AccessDenied("Unknown output template.")
    before = default_for(actor)
    if template is None:
        OutputTemplateDefault.objects.filter(user=actor).delete()
    else:
        OutputTemplateDefault.objects.update_or_create(user=actor, defaults={"template": template})
    audit.record(
        "output_template.default",
        object_type="accounts.user",
        object_id=str(actor.pk),
        object_repr=actor.email,
        before={"template": before.name if before else None},
        after={"template": template.name if template else None},
        actor=actor,
        organisation=None,
    )


def settings_for(template: OutputTemplate | None) -> dict:
    """The job spec settings a run with `template` carries (none for every column)."""
    if template is None:
        return {}
    return {"output_template": list(template.columns), "output_template_name": template.name}
