import pytest
from django.db import IntegrityError, connection, transaction

from cedars.services import audit
from cedars.services.audit.context import acting_as
from cedars.services.audit.models import AuditEvent

pytestmark = pytest.mark.django_db


def test_record_takes_the_actor_from_the_context(make_user, pge):
    u = make_user("someone@pge.example.org")
    with acting_as(u, pge, "mfa", "req-1"):
        e = audit.record("thing.changed", obj=pge, before={"a": 1}, after={"a": 2}, reason="because")
    assert (e.actor, e.actor_label, e.organisation, e.assurance, e.request_id) == (u, "someone@pge.example.org", pge, "mfa", "req-1")
    assert (e.object_type, e.object_id, e.reason) == ("accounts.organisation", str(pge.pk), "because")
    assert audit.diff(e.before, e.after) == [("a", 1, 2)]


def test_system_events_have_no_actor(pge):
    e = audit.record("job.ran", obj=pge)
    assert e.actor is None and e.actor_label == "system" and e.assurance == ""


def test_events_cannot_be_updated_or_deleted(pge):
    e = audit.record("thing.changed", obj=pge)
    for sql in ("UPDATE audit_auditevent SET action = 'x' WHERE id = %s", "DELETE FROM audit_auditevent WHERE id = %s"):
        with pytest.raises(Exception, match="append-only"), transaction.atomic(), connection.cursor() as c:
            c.execute(sql, [e.pk])
    with pytest.raises(Exception, match="append-only"), transaction.atomic():
        AuditEvent.objects.filter(pk=e.pk).update(reason="edited")
    with pytest.raises(Exception, match="append-only"), transaction.atomic():
        e.delete()
    assert AuditEvent.objects.get(pk=e.pk).action == "thing.changed"


def test_an_actor_with_events_cannot_be_deleted(make_user, pge):
    u = make_user("someone@pge.example.org")
    audit.record("thing.changed", obj=pge, actor=u)
    with pytest.raises(IntegrityError), transaction.atomic():
        u.delete()


def test_snapshot_and_changes(pge):
    snap = audit.snapshot(pge, ["code", "name", "federated"])
    assert snap == {"code": "PGE", "name": "Pacific Gas and Electric", "federated": False}
    audit.record("organisation.renamed", obj=pge, before=snap, after={**snap, "name": "PG&E"})
    assert [e.action for e in audit.changes(pge)] == ["organisation.created", "organisation.renamed"]


def test_requests_carry_a_request_id(client):
    r = client.get("/")
    assert len(r["X-Request-Id"]) == 32
