"""Retiring fields and the dictionary's change log (BR-08.4, BR-08.2).

A field is retired, never dropped, after a usage, audience and regulatory review: `retire` makes a new catalog version, a copy of the
current one with the field marked retired from a date and the three reviews and the reason kept on it, and `reinstate` makes one
with the field back in use. A field a published entry of the register of CPUC-mandated changes still requires cannot be retired until
that entry is superseded or withdrawn (releases.mandates.requiring_field).

`changes(version)` compares a catalog version with the one before it, field by field: added, dropped, retired, reinstated, and every
attribute, other name and usage that changed. The public Changes page (/dictionary/changes/) lists them for every version, with the
published versions of the value lists and the rule sets beside them, as the specification change log the register is published with
(BR-12.7)."""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import service
from .models import CatalogVersion, Field

# the attributes compared between versions, with the label the change log gives them
COMPARED = {
    "kind": "Kind",
    "group": "Group",
    "definition": "Definition",
    "data_type": "Type",
    "max_length": "Maximum length",
    "unit": "Unit",
    "source": "Source",
    "source_version": "Version",
    "cost_tests": "Cost tests",
    "time_basis": "Time basis",
    "aggregation": "Aggregation",
    "rounding": "Rounding",
    "years_in_use": "Years in use",
    "audience": "Audience",
    "required_by": "Required by",
    "doc_link": "Calculation documentation",
    "retired_from": "Retired from",
}
REVIEWS = ["review_usage", "review_audience", "review_regulatory"]


class RetirementError(ValueError):
    """The field cannot be retired or reinstated as asked; the message says why."""


def _require_admin(actor) -> None:
    if not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator retires or reinstates a field.")


def can_retire(user) -> bool:
    return bool(user and user.is_authenticated and access.is_system_admin(user))


def _next_label(base: CatalogVersion) -> str:
    """The base's label with the next revision number: "ReCET 0.1.87" -> "ReCET 0.1.87 rev 2" -> "ReCET 0.1.87 rev 3"."""
    root = re.sub(r" rev \d+$", "", base.label)
    n = 2
    while CatalogVersion.objects.filter(label=f"{root} rev {n}"[:80]).exists():
        n += 1
    return f"{root[: 80 - len(f' rev {n}')]} rev {n}"


def _copy(actor, base: CatalogVersion, notes: str, name: str, **changes) -> tuple[CatalogVersion, Field]:
    """A new catalog version: every field of the base copied, the named one with the changes."""
    version = CatalogVersion.objects.create(label=_next_label(base), notes=notes, loaded_by=actor, based_on=base)
    changed = None
    for f in base.fields.prefetch_related("aliases", "usages"):
        copy = service.copy_field(f, version, **(changes if f.name == name else {}))
        if f.name == name:
            changed = copy
    return version, changed


@transaction.atomic
def retire(
    actor, name: str, *, retired_from: date, reason: str, usage: str, audience: str, regulatory: str
) -> tuple[CatalogVersion, Field]:
    """Retire a field of the current catalog from a date (today or later), after the three reviews BR-08.4 asks for."""
    _require_admin(actor)
    base = service.current()
    f = base and base.fields.filter(name=name).first()
    if f is None:
        raise RetirementError(f"The current catalog has no field {name}.")
    if f.state == Field.State.RETIRED:
        raise RetirementError(f"{name} is already retired (from {f.retired_from:%Y-%m-%d}).")
    texts = {"reason": reason, "usage review": usage, "audience review": audience, "regulatory review": regulatory}
    missing = [k for k, v in texts.items() if not (v or "").strip()]
    if missing:
        raise RetirementError("Give the " + ", the ".join(missing) + ": a field is retired only after all three reviews.")
    if retired_from < timezone.localdate():
        raise RetirementError("A retirement takes effect today or later, so people can be told before the field leaves the templates.")
    from cedars.services.releases import mandates  # a service may read another service; imported here to keep the two loosely tied

    held = mandates.requiring_field(name)
    if held:
        raise RetirementError(
            f"{name} is required by {', '.join(m.reference for m in held)} in the register of CPUC-mandated changes. "
            "Mark that entry superseded or withdrawn first, if the requirement no longer stands."
        )
    version, field_ = _copy(
        actor,
        base,
        f"Retired {name} from {retired_from:%Y-%m-%d}: {reason.strip()}",
        name,
        state=Field.State.RETIRED,
        retired_from=retired_from,
        retirement_reason=reason.strip(),
        review_usage=usage.strip(),
        review_audience=audience.strip(),
        review_regulatory=regulatory.strip(),
    )
    audit.record(
        "dictionary.field_retired",
        obj=field_,
        before={"version": base.label, "state": f.state},
        after={"version": version.label, **audit.snapshot(field_, ["state", "retired_from", "retirement_reason", *REVIEWS])},
        reason=reason.strip(),
        actor=actor,
    )
    return version, field_


@transaction.atomic
def reinstate(actor, name: str, *, reason: str) -> tuple[CatalogVersion, Field]:
    """Put a retired field back in use, in a new catalog version."""
    _require_admin(actor)
    base = service.current()
    f = base and base.fields.filter(name=name).first()
    if f is None:
        raise RetirementError(f"The current catalog has no field {name}.")
    if f.state != Field.State.RETIRED:
        raise RetirementError(f"{name} is not retired.")
    if not (reason or "").strip():
        raise RetirementError("Give the reason the field is back in use.")
    version, field_ = _copy(
        actor,
        base,
        f"Reinstated {name}: {reason.strip()}",
        name,
        state=Field.State.ACTIVE,
        retired_from=None,
        retirement_reason="",
        review_usage="",
        review_audience="",
        review_regulatory="",
    )
    audit.record(
        "dictionary.field_reinstated",
        obj=field_,
        before={"version": base.label, **audit.snapshot(f, ["state", "retired_from", "retirement_reason"])},
        after={"version": version.label, "state": field_.state},
        reason=reason.strip(),
        actor=actor,
    )
    return version, field_


# --- the change log


@dataclass
class FieldChange:
    name: str
    what: str  # added, dropped, retired, reinstated, changed
    details: list[tuple[str, str, str]] = field(default_factory=list)  # (attribute, before, after)
    note: str = ""  # the retirement's reason, for a retired field


@dataclass
class VersionChanges:
    version: CatalogVersion
    previous: CatalogVersion | None
    fields: list[FieldChange] = field(default_factory=list)

    def of(self, what: str) -> list[FieldChange]:
        return [c for c in self.fields if c.what == what]

    @property
    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in ("added", "retired", "reinstated", "changed", "dropped")}
        for c in self.fields:
            out[c.what] += 1
        return out


def previous_of(version: CatalogVersion) -> CatalogVersion | None:
    """The version loaded just before this one (catalog versions are ordered newest first)."""
    return (
        CatalogVersion.objects.filter(loaded_at__lt=version.loaded_at).first()
        or CatalogVersion.objects.filter(loaded_at=version.loaded_at, pk__lt=version.pk).first()
    )


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return f"{value:%Y-%m-%d}"
    return str(value)


def _fields(version: CatalogVersion) -> dict[str, Field]:
    return {f.name.lower(): f for f in version.fields.prefetch_related("aliases", "usages")}


def _names(f: Field) -> set[str]:
    return {f"{a.name} ({a.system})" for a in f.aliases.all()}


def _uses(f: Field) -> set[str]:
    return {f"{u.module.capitalize()} {u.table}{' (required)' if u.required else ''}" for u in f.usages.all()}


def _compare(old: Field, new: Field) -> list[tuple[str, str, str]]:
    out = [(label, _text(getattr(old, a)), _text(getattr(new, a))) for a, label in COMPARED.items() if getattr(old, a) != getattr(new, a)]
    for label, before, after in (("Other names", _names(old), _names(new)), ("Used in", _uses(old), _uses(new))):
        if before != after:
            out.append((label, "; ".join(sorted(before - after)), "; ".join(sorted(after - before))))
    return out


def changes(version: CatalogVersion) -> VersionChanges:
    """What changed in a catalog version against the one before it; the first version lists every field as added."""
    previous = previous_of(version)
    new = _fields(version)
    old = _fields(previous) if previous else {}
    out = VersionChanges(version, previous)
    for key in sorted(set(new) | set(old)):
        n, o = new.get(key), old.get(key)
        if o is None:
            out.fields.append(FieldChange(n.name, "added", note="retired" if n.state == Field.State.RETIRED else ""))
        elif n is None:
            out.fields.append(FieldChange(o.name, "dropped"))
        else:
            details = _compare(o, n)
            if n.name != o.name:
                details.insert(0, ("Name", o.name, n.name))
            if o.state != n.state:
                details = [d for d in details if d[0] != "Retired from"]
                what = "retired" if n.state == Field.State.RETIRED else "reinstated"
                note = n.retirement_reason if what == "retired" else ""
                if what == "retired":
                    details.insert(0, ("Retired from", "", _text(n.retired_from)))
                out.fields.append(FieldChange(n.name, what, details, note))
            elif details:
                out.fields.append(FieldChange(n.name, "changed", details))
    order = {"retired": 0, "reinstated": 1, "added": 2, "dropped": 3, "changed": 4}
    out.fields.sort(key=lambda c: (order[c.what], c.name.lower()))
    return out


def history(name: str) -> list[tuple[CatalogVersion, FieldChange]]:
    """Every catalog version that added, changed, retired or reinstated the field, newest first."""
    out = []
    for v in CatalogVersion.objects.filter(fields__name__iexact=name).distinct():
        for c in changes(v).fields:
            if c.name.lower() == name.lower():
                out.append((v, c))
    return out


def log_csv() -> str:
    """Every catalog version's changes as one file: version, when, field, change, attribute, before, after."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["catalog_version", "loaded", "previous_version", "field", "change", "attribute", "before", "after", "note"])
    for v in CatalogVersion.objects.all():
        ch = changes(v)
        prev = ch.previous.label if ch.previous else ""
        for c in ch.fields:
            for attr, before, after in c.details or [("", "", "")]:
                w.writerow([v.label, f"{timezone.localtime(v.loaded_at):%Y-%m-%d}", prev, c.name, c.what, attr, before, after, c.note])
    return out.getvalue()
