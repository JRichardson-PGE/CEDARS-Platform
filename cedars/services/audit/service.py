import json
from collections.abc import Iterable

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from .context import current
from .models import AuditEvent


def snapshot(obj: models.Model, fields: Iterable[str] | None = None) -> dict:
    """A record's field values as JSON-ready data (foreign keys as their ids), for an event's before / after."""
    names = list(fields) if fields is not None else [f.name for f in obj._meta.concrete_fields]
    out = {}
    for name in names:
        field = obj._meta.get_field(name)
        value = getattr(obj, field.attname)
        out[name] = json.loads(json.dumps(value, cls=DjangoJSONEncoder))
    return out


def diff(before: dict | None, after: dict | None) -> list[tuple[str, object, object]]:
    """(field, old, new) for every field whose value differs."""
    before, after = before or {}, after or {}
    return [(k, before.get(k), after.get(k)) for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]


def record(
    action: str,
    *,
    obj: models.Model | None = None,
    before: dict | None = None,
    after: dict | None = None,
    reason: str = "",
    actor=None,
    organisation=None,
    object_type: str = "",
    object_id: str = "",
    object_repr: str = "",
    actor_label: str = "",
) -> AuditEvent:
    """Write one audit event. The actor, organization and assurance default to the current request's (audit.context). actor_label names
    an actor with no account (e.g. "public" for a signed-out reader) where the default would say "system"."""
    ctx = current()
    actor = actor if actor is not None else ctx.user
    organisation = organisation if organisation is not None else ctx.organisation
    if obj is not None:
        object_type = object_type or obj._meta.label_lower
        object_id = object_id or str(obj.pk)
        object_repr = object_repr or str(obj)[:300]
    return AuditEvent.objects.create(
        actor=actor,
        actor_label=(actor.email if actor is not None else actor_label or "system")[:254],
        organisation=organisation,
        assurance=ctx.assurance if actor is ctx.user else "",
        action=action,
        object_type=object_type,
        object_id=object_id,
        object_repr=object_repr,
        reason=reason,
        before=before,
        after=after,
        request_id=ctx.request_id,
    )


def changes(obj: models.Model):
    """Every event about one record, oldest first: its change history."""
    return AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk)).order_by("at", "id")


# An event's action in words, wherever a person reads a history or the audit trail (UI review 3, U12): "program_change.requested" is
# "Program change requested", or "Requested" beside the thing it happened to. The action names themselves keep their original spelling
# ("organisation", "cancelled"); the words are US English.
_SUBJECT_WORDS = {
    "organisation": "organization",
    "api_token": "API token",
    "etrm_sync": "eTRM sync",
    "pack": "data pack",
    "pack_check": "data pack check",
    "calculation_run": "run",
    "submission": "filing",
    "user": "account",
}
_VERB_WORDS = {
    "cancelled": "canceled",
    "set_up": "set up",
    "setup_changed": "set-up changed",
    "etrm_indexed": "eTRM permutations indexed",
    "code_added": "code added",
}


def words(action: str, *, verb_only: bool = False) -> str:
    """An audit action in plain words: "Program change requested", or with verb_only "Requested"."""
    subject, _, verb = (action or "").partition(".")
    if not verb:
        subject, verb = "", subject
    said = _VERB_WORDS.get(verb, verb.replace("_", " ").replace("cancelled", "canceled").replace("organisation", "organization"))
    if verb_only or not subject:
        return said[:1].upper() + said[1:]
    thing = _SUBJECT_WORDS.get(subject, subject.replace("_", " ").replace("organisation", "organization"))
    return f"{thing[:1].upper()}{thing[1:]} {said}"
