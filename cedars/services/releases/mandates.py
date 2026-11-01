"""The register of CPUC-mandated data and calculation changes (BR-12.7), published with the specification change log.

Each entry (models.Mandate) names the decision or resolution, what it requires of CEDARS, when it takes effect and its implementation
status, and links to where CEDARS carries it: a published rule set, catalog (dictionary) version, value list version, a field, a
published data pack (an ACC, a cost-test set) or a published release note (LINK_KINDS; each checked to exist and to be public when it
is linked). CPUC system administrators keep the register; everyone can read it (/mandates/, /mandates/register.csv).

A draft entry is seen only by administrators and may be changed freely or discarded. Publishing puts it on the register; from then on
every change needs a reason, is audited ("mandate.changed", "mandate.link_added", "mandate.link_removed") and shows in the entry's
public history (what changed and when, never who). A published entry is never deleted: one entered in error is withdrawn.

The register is public, so it holds only public CPUC information: a document link must be https on a ca.gov site, and the forms say
not to enter anything that is not in the public document. A field a published entry in force links to cannot be retired from the
dictionary until the entry is superseded or withdrawn (`requiring_field`, read by dictionary.changes.retire)."""

import csv
import io
from dataclasses import dataclass
from urllib.parse import urlparse

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from .models import Mandate, MandateLink, ReleaseNote

FIELDS = [
    "reference",
    "authority",
    "title",
    "issued_on",
    "document_url",
    "requirement",
    "area",
    "effective_on",
    "status",
    "status_note",
    "implemented_on",
    "superseded_by",
]
Status = Mandate.Status


class MandateError(ValueError):
    """The entry cannot be saved, published or linked as it is; the message says why."""


def _require_admin(user) -> None:
    if not access.is_system_admin(user):
        raise AccessDenied("Only a CPUC system administrator keeps the register of CPUC-mandated changes.")


def can_edit(user) -> bool:
    return bool(user and user.is_authenticated and access.is_system_admin(user))


# --- what an entry links to


def _rule_set(key):
    from cedars.services.rules.models import RuleSet

    rs = RuleSet.objects.filter(pk=key, status="published").first()
    if rs is None:
        raise MandateError(f"There is no published rule set {key}.")
    return f"Rule set: {rs.label}, in force from {rs.effective_from:%Y-%m-%d}", reverse("rules:detail", args=[rs.pk])


def _catalog(key):
    from cedars.services.dictionary.models import CatalogVersion

    v = CatalogVersion.objects.filter(pk=key).first()
    if v is None:
        raise MandateError(f"There is no dictionary version {key}.")
    return f"Data dictionary version {v.label}", reverse("dictionary:version_changes", args=[v.pk])


def _value_list(key):
    from cedars.services.dictionary.models import ValueListVersion

    v = ValueListVersion.objects.filter(pk=key, status="published").select_related("value_list").first()
    if v is None:
        raise MandateError(f"There is no published value list version {key}.")
    url = reverse("dictionary:list", args=[v.value_list.name]) + f"?version={v.number}"
    return f"Value list {v.value_list.name}, version {v.number}, from {v.effective_from:%Y-%m-%d}", url


def _field(key):
    from cedars.services.dictionary import service as dictionary

    f = dictionary.resolve(key)
    if f is None:
        raise MandateError(f"The data dictionary has no field {key}.")
    return f"Field {f.name}", reverse("dictionary:field", args=[f.name])


def _pack(key):
    from cedars.services.refdata.models import PackVersion

    p = PackVersion.objects.filter(pk=key, status="published").first()
    if p is None:
        raise MandateError(f"There is no published reference data pack {key}.")
    return f"{p.get_kind_display()}: {p.label}{f' {p.version}' if p.version else ''}", reverse("refdata:detail", args=[p.pk])


def _release_note(key):
    n = ReleaseNote.objects.filter(pk=key, published_at__isnull=False).first()
    if n is None:
        raise MandateError(f"There is no published release note {key}.")
    return f"Release note: {n.title}, {n.effective_on:%Y-%m-%d}", reverse("releases:detail", args=[n.pk])


@dataclass(frozen=True)
class LinkKind:
    label: str
    key_help: str
    resolve: object  # key -> (label, url); raises MandateError
    numeric: bool = True


LINK_KINDS = {
    "rule_set": LinkKind("A validation rule set", "the number in the rule set's address, /rules/<number>/", _rule_set),
    "catalog": LinkKind("A data dictionary version", "the number in the version's address on the Changes page", _catalog),
    "value_list": LinkKind("A value list version", "the version's number from its list page's address", _value_list),
    "field": LinkKind("A field", "the field's name, e.g. TaxCredits", _field, numeric=False),
    "pack": LinkKind("A reference data pack (an ACC, a cost-test set)", "the number in the pack's address", _pack),
    "release_note": LinkKind("A release note", "the number in the note's address, /releases/<number>/", _release_note),
}


def _key(kind: str, key: str) -> str:
    """The record's key from what was typed: the key itself, or a pasted address ending in it."""
    key = (key or "").strip()
    if kind not in LINK_KINDS:
        raise MandateError("Choose what the entry links to.")
    if "/" in key:  # a pasted address: take its last part, ignoring the query
        path = urlparse(key).path.rstrip("/")
        key = path.rsplit("/", 1)[-1]
    if not key:
        raise MandateError("Give the record to link.")
    if LINK_KINDS[kind].numeric and not key.isdigit():
        raise MandateError(f"Give {LINK_KINDS[kind].key_help}.")
    return key


def link_target(link: MandateLink) -> str:
    """The link's address now, or "" when the record is gone or no longer public (the stored label still says what it was)."""
    try:
        return LINK_KINDS[link.kind].resolve(link.key)[1]
    except (KeyError, MandateError):
        return ""


# --- reading


def published():
    return Mandate.objects.filter(published_at__isnull=False).prefetch_related("links").select_related("superseded_by")


def drafts():
    return Mandate.objects.filter(published_at__isnull=True).prefetch_related("links")


def requiring_field(name: str) -> list[Mandate]:
    """Published entries still in force that link to the field: while one does, the field is not retired."""
    from cedars.services.dictionary import service as dictionary

    f = dictionary.resolve(name)
    names = {name.lower()} | ({f.name.lower()} | {a.name.lower() for a in f.aliases.all()} if f else set())
    links = MandateLink.objects.filter(kind="field", mandate__published_at__isnull=False).select_related("mandate")
    found = {ln.mandate for ln in links if ln.key.lower() in names and ln.mandate.in_force}
    return sorted(found, key=lambda m: m.reference)


def for_field(name: str) -> list[Mandate]:
    """Every published entry that links to the field, in force or not, for the field's page."""
    links = MandateLink.objects.filter(kind="field", key__iexact=name, mandate__published_at__isnull=False).select_related("mandate")
    return sorted({ln.mandate for ln in links}, key=lambda m: (-m.effective_on.toordinal(), m.reference))


def for_record(kind: str, key) -> list[Mandate]:
    """Published entries that link to a record (a catalog version's changes, a rule set), for that record's page."""
    links = MandateLink.objects.filter(kind=kind, key=str(key), mandate__published_at__isnull=False).select_related("mandate")
    return sorted({ln.mandate for ln in links}, key=lambda m: m.reference)


LABELS = {name: str(Mandate._meta.get_field(name).verbose_name).capitalize() for name in FIELDS}
LABELS["superseded_by"] = "Superseded by"
ACTIONS = {
    "mandate.published": "Published on the register",
    "mandate.changed": "Changed",
    "mandate.link_added": "Linked",
    "mandate.link_removed": "Link removed",
}


def _shown(name: str, value) -> str:
    if value in (None, ""):
        return ""
    if name in ("authority", "area", "status"):
        return dict(Mandate._meta.get_field(name).choices).get(value, value)
    if name == "superseded_by":
        m = Mandate.objects.filter(pk=value).first()
        return m.reference if m else str(value)
    return str(value)


def history(m: Mandate) -> list[dict]:
    """The entry's public history from the audit trail, oldest first: when, what happened, each change (field, before, after) and the
    reason. Who made a change is in the audit trail for administrators, not here."""
    out = []
    for e in audit.changes(m).filter(action__in=ACTIONS):
        if e.action == "mandate.changed":
            rows = [(LABELS.get(k, k), _shown(k, a), _shown(k, b)) for k, a, b in audit.diff(e.before, e.after)]
        elif e.action in ("mandate.link_added", "mandate.link_removed"):
            label = (e.after or e.before or {}).get("label", "")
            rows = [("Link", "" if e.action == "mandate.link_added" else label, label if e.action == "mandate.link_added" else "")]
        else:
            rows = []
        out.append({"at": e.at, "what": ACTIONS[e.action], "rows": rows, "reason": e.reason})
    return out


def register_csv(entries) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(
        ["reference", "kind", "title", "issued_on", "document", "requirement", "changes", "effective_on", "status", "about_status"]
        + ["implemented_on", "superseded_by", "carried_by", "published"]
    )
    for m in entries:
        w.writerow(
            [
                m.reference,
                m.get_authority_display(),
                m.title,
                m.issued_on or "",
                m.document_url,
                m.requirement,
                m.get_area_display(),
                m.effective_on,
                m.get_status_display(),
                m.status_note,
                m.implemented_on or "",
                m.superseded_by.reference if m.superseded_by else "",
                "; ".join(ln.label for ln in m.links.all()),
                f"{timezone.localtime(m.published_at):%Y-%m-%d}" if m.published_at else "",
            ]
        )
    return out.getvalue()


# --- keeping the register


def _clean(values: dict, m: Mandate | None = None) -> dict:
    v = {k: values[k] for k in FIELDS if k in values}
    for k in ("reference", "title", "requirement", "status_note", "document_url"):
        if k in v:
            v[k] = (v[k] or "").strip()
    merged = {**({k: getattr(m, k) for k in FIELDS} if m else {}), **v}
    if not merged.get("reference"):
        raise MandateError("Give the decision or resolution, as the CPUC cites it.")
    if not merged.get("title") or not merged.get("requirement"):
        raise MandateError("Give the title and what it requires of CEDARS.")
    if not merged.get("effective_on"):
        raise MandateError("Give the date it takes effect.")
    url = merged.get("document_url") or ""
    if url:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == "ca.gov" or host.endswith(".ca.gov")):
            raise MandateError("Link the public document on a ca.gov site (https), such as docs.cpuc.ca.gov: the register is public.")
    status = merged.get("status") or Status.NOT_STARTED
    if status == Status.IMPLEMENTED and not merged.get("implemented_on"):
        raise MandateError("Give the date it was implemented.")
    if status != Status.IMPLEMENTED and merged.get("implemented_on") and "implemented_on" not in v:
        v["implemented_on"] = None  # moved off Implemented: the date no longer applies
    elif status != Status.IMPLEMENTED and v.get("implemented_on"):
        raise MandateError("An implementation date goes with the Implemented status.")
    if status in (Status.SUPERSEDED, Status.WITHDRAWN) and not merged.get("status_note"):
        raise MandateError("Say why it is superseded or withdrawn, under About the status.")
    sup = merged.get("superseded_by")
    if sup is not None:
        if status != Status.SUPERSEDED:
            raise MandateError("Name what supersedes it only with the Superseded status.")
        if m is not None and sup.pk == m.pk:
            raise MandateError("An entry does not supersede itself.")
        if sup.is_draft:
            raise MandateError("It can be superseded only by a published entry.")
    return v


@transaction.atomic
def create_draft(user, **values) -> Mandate:
    _require_admin(user)
    m = Mandate.objects.create(created_by=user, **_clean(values))
    audit.record("mandate.drafted", obj=m, after=audit.snapshot(m, FIELDS), actor=user)
    return m


@transaction.atomic
def update(user, m: Mandate, *, reason: str = "", **values) -> Mandate:
    """Change an entry. A published entry's change needs a reason, and shows in its history."""
    _require_admin(user)
    m = Mandate.objects.select_for_update().get(pk=m.pk)
    if not m.is_draft and not (reason or "").strip():
        raise MandateError("Say why the entry changes: the reason is kept with it and shown in its history.")
    v = _clean(values, m)
    before = audit.snapshot(m, FIELDS)
    for k, val in v.items():
        setattr(m, k, val)
    m.save()
    after = audit.snapshot(m, FIELDS)
    if before != after:
        audit.record(
            "mandate.drafted" if m.is_draft else "mandate.changed",
            obj=m,
            before=before,
            after=after,
            reason=(reason or "").strip(),
            actor=user,
        )
    return m


@transaction.atomic
def publish(user, m: Mandate) -> Mandate:
    _require_admin(user)
    m = Mandate.objects.select_for_update().get(pk=m.pk)
    if not m.is_draft:
        raise MandateError("It is already on the register.")
    _clean({}, m)
    m.published_at, m.published_by = timezone.now(), user
    m.save(update_fields=["published_at", "published_by", "updated_at"])
    audit.record("mandate.published", obj=m, after=audit.snapshot(m, FIELDS), actor=user)
    return m


@transaction.atomic
def discard_draft(user, m: Mandate) -> None:
    _require_admin(user)
    if not m.is_draft:
        raise MandateError("A published entry is never deleted: withdraw it instead, with the reason.")
    audit.record("mandate.discarded", obj=m, before=audit.snapshot(m, FIELDS), actor=user)
    m.delete()


@transaction.atomic
def add_link(user, m: Mandate, kind: str, key: str, *, reason: str = "") -> MandateLink:
    _require_admin(user)
    m = Mandate.objects.get(pk=m.pk)
    key = _key(kind, key)
    if not m.is_draft and not (reason or "").strip():
        raise MandateError("Say why the link is added: the reason is shown in the entry's history.")
    label, _ = LINK_KINDS[kind].resolve(key)
    if kind == "field":
        key = label.removeprefix("Field ")  # the canonical name, whichever name was typed
    if m.links.filter(kind=kind, key=key).exists():
        raise MandateError("The entry already links to it.")
    link = MandateLink.objects.create(mandate=m, kind=kind, key=key, label=label)
    audit.record("mandate.link_added", obj=m, after={"kind": kind, "key": key, "label": label}, reason=(reason or "").strip(), actor=user)
    return link


@transaction.atomic
def remove_link(user, link: MandateLink, *, reason: str = "") -> None:
    _require_admin(user)
    m = Mandate.objects.get(pk=link.mandate_id)
    if not m.is_draft and not (reason or "").strip():
        raise MandateError("Say why the link is removed: the reason is shown in the entry's history.")
    audit.record(
        "mandate.link_removed",
        obj=m,
        before={"kind": link.kind, "key": link.key, "label": link.label},
        reason=(reason or "").strip(),
        actor=user,
    )
    link.delete()
