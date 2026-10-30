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
) -> AuditEvent:
    """Write one audit event. The actor, organisation and assurance default to the current request's (audit.context)."""
    ctx = current()
    actor = actor if actor is not None else ctx.user
    organisation = organisation if organisation is not None else ctx.organisation
    if obj is not None:
        object_type = object_type or obj._meta.label_lower
        object_id = object_id or str(obj.pk)
        object_repr = object_repr or str(obj)[:300]
    return AuditEvent.objects.create(
        actor=actor,
        actor_label=(actor.email if actor is not None else "system")[:254],
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
