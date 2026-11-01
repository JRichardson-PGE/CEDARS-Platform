"""Feedback from every page (BR-12.4): the footer link, the message recorded with its page and sender, the CPUC system
administrators told, the admin list, limits and retention."""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from cedars.services.audit.models import AuditEvent
from cedars.services.feedback import service
from cedars.services.feedback.models import Feedback
from cedars.services.notifications.models import Notification
from tests.conftest import page_only

pytestmark = pytest.mark.django_db


def test_every_page_links_to_feedback_on_itself(client, signed_in, pge_admin):
    page = client.get("/limits/?format=html").content.decode()
    assert '/feedback/?from=/limits/%3Fformat%3Dhtml">Feedback on this page' in page
    signed_in(pge_admin)
    assert "/feedback/?from=/notifications/" in client.get("/notifications/").content.decode()


def test_a_signed_in_member_sends_feedback(client, signed_in, cpuc_admin, pge, pge_admin, django_capture_on_commit_callbacks):
    signed_in(pge_admin)
    form = client.get("/feedback/?from=/budgets/%3Fyear%3D2028").content.decode()
    assert "/budgets/?year=2028" in form and "so they can reply" in form
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post("/feedback/", {"page": "/budgets/?year=2028", "message": "The year filter is hard to find."})
    assert r.status_code == 302 and r["Location"] == "/budgets/?year=2028"
    fb = Feedback.objects.get()
    assert (fb.page, fb.user, fb.organisation, fb.status, fb.sender_key) == ("/budgets/?year=2028", pge_admin, pge, "new", "")
    note = Notification.objects.get(user=cpuc_admin)
    assert note.link == f"/admin/feedback/{fb.pk}/" and "year filter" in note.body and "PGE" in note.body
    assert fb.__class__.objects.get().notified == 1
    assert AuditEvent.objects.filter(action="feedback.received", object_id=str(fb.pk), actor=pge_admin).exists()
    assert not Notification.objects.filter(user=pge_admin).exists()  # only the CPUC's system administrators are told


def test_signed_out_feedback_keeps_no_name_or_address(client, cpuc_admin, settings, django_capture_on_commit_callbacks):
    settings.CEDARS_FEEDBACK_EMAIL = "cedars-team@example.org"
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/feedback/", {"page": "/data/overview/", "message": "Lovely charts"}, REMOTE_ADDR="203.0.113.9")
    fb = Feedback.objects.get()
    assert fb.user is None and fb.organisation is None
    assert fb.sender_key and "203.0.113.9" not in fb.sender_key
    assert any(m.to == ["cedars-team@example.org"] and "Lovely charts" in m.body for m in mail.outbox)
    assert AuditEvent.objects.get(action="feedback.received").actor_label == "public"


def test_the_page_must_be_on_this_site(client):
    for bad in ("https://example.org/x", "//example.org/x", "javascript:alert(1)", "/\\example.org"):
        assert service.safe_page(bad) == "/"
    r = client.post("/feedback/", {"page": "https://example.org/", "message": "hello"})
    assert r["Location"] == "/" and Feedback.objects.get().page == "/"


def test_limits(client, settings):
    settings.CEDARS_FEEDBACK_MAX_CHARS = 20
    settings.CEDARS_FEEDBACK_PER_HOUR = 2
    r = client.post("/feedback/", {"page": "/", "message": "x" * 21})
    assert r.status_code == 200 and not Feedback.objects.exists()
    for _ in range(2):
        client.post("/feedback/", {"page": "/", "message": "short"}, REMOTE_ADDR="198.51.100.1")
    r = client.post("/feedback/", {"page": "/", "message": "short"}, REMOTE_ADDR="198.51.100.1")
    assert r.status_code == 200 and "in the last hour" in page_only(r.content.decode())
    client.post("/feedback/", {"page": "/", "message": "short"}, REMOTE_ADDR="198.51.100.2")
    assert Feedback.objects.count() == 3
    limits = client.get("/limits/?format=json").json()
    keys = {row["key"] for group in limits["groups"] for row in group["limits"]} if "groups" in limits else str(limits)
    assert "feedback_chars" in keys and "feedback_per_hour" in keys


def test_administrators_list_and_close_feedback(client, signed_in, cpuc_admin, pge_admin):
    fb = service.submit(page="/claims/", message="Where is the 2027 claim?", user=pge_admin)
    signed_in(pge_admin)
    assert client.get("/admin/feedback/").status_code == 404
    signed_in(cpuc_admin)
    page = client.get("/admin/feedback/").content.decode()
    assert "Where is the 2027 claim?" in page
    detail = client.get(f"/admin/feedback/{fb.pk}/").content.decode()
    assert "mailto:admin@pge.com" in detail
    client.post(f"/admin/feedback/{fb.pk}/close", {"note": "Replied by email"})
    fb.refresh_from_db()
    assert (fb.status, fb.closed_by, fb.note) == ("closed", cpuc_admin, "Replied by email")
    assert "Where is the 2027 claim?" not in client.get("/admin/feedback/").content.decode()
    client.post(f"/admin/feedback/{fb.pk}/reopen", {"reason": "not answered"})
    fb.refresh_from_db()
    assert fb.status == "new"
    assert [e.action for e in AuditEvent.objects.filter(object_type="feedback.feedback").order_by("id")] == [
        "feedback.received",
        "feedback.closed",
        "feedback.reopened",
    ]


def test_closed_feedback_goes_when_its_retention_ends(cpuc_admin, pge_admin):
    from cedars.services.retention import service as retention
    from cedars.services.retention.models import RetentionPolicy

    assert RetentionPolicy.objects.get(record_class="feedback").keep_days == 730
    old_open = service.submit(page="/", message="open", user=pge_admin)
    old_closed = service.submit(page="/", message="closed", user=pge_admin)
    service.close(cpuc_admin, old_closed, "done")
    Feedback.objects.filter(pk__in=[old_open.pk, old_closed.pk]).update(created_at=timezone.now() - timedelta(days=800))
    retention.purge()
    assert list(Feedback.objects.values_list("message", flat=True)) == ["open"]
