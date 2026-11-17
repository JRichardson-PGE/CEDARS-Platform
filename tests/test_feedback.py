"""Requests from every page: the footer link, the message recorded with its kind, page and sender, the CPUC system
administrators told, the conversation (answers, questions, replies, staff notes), My requests, My queue, limits and retention."""

from datetime import timedelta
from html import unescape

import pytest
from django.core import mail
from django.utils import timezone

import cedars
from cedars.core import page_help
from cedars.core import queue as my_queue
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.feedback import service, suggestions
from cedars.services.feedback.models import Feedback, Reply
from cedars.services.notifications.models import Notification
from tests.conftest import page_only

pytestmark = pytest.mark.django_db


def test_every_page_links_to_feedback_on_itself(client, signed_in, pge_admin):
    page = client.get("/limits/?format=html").content.decode()
    assert '/feedback/?from=/limits/%3Fformat%3Dhtml&amp;help=releases/limits">Feedback on this page' in page
    assert 'help=releases/limits">Still stuck? Ask the CPUC about this page' in page  # in the page's help, too
    signed_in(pge_admin)
    assert "/feedback/?from=/notifications/" in client.get("/notifications/").content.decode()


def test_a_signed_in_member_sends_feedback(client, signed_in, cpuc_admin, pge, pge_admin, django_capture_on_commit_callbacks):
    signed_in(pge_admin)
    form = client.get("/feedback/?from=/budgets/%3Fyear%3D2028").content.decode()
    assert "/budgets/?year=2028" in form and "so they can reply" in form
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post("/feedback/", {"page": "/budgets/?year=2028", "kind": "problem", "message": "The year filter is hard to find."})
    assert r.status_code == 302 and r["Location"] == "/budgets/?year=2028"
    fb = Feedback.objects.get()
    assert (fb.kind, fb.site_version, fb.environment) == ("problem", cedars.__version__, "test") and fb.active_at
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
    assert client.get(f"/admin/feedback/{fb.pk}/").status_code == 200
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


def test_a_request_is_answered_asked_about_and_settled(client, signed_in, cpuc_admin, pge_admin, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        fb = service.submit(page="/claims/", message="Where is the 2027 claim?", user=pge_admin, kind="help")
    url, admin_url = f"/feedback/{fb.pk}/", f"/admin/feedback/{fb.pk}/"
    assert [i.url for i in service.to_answer(cpuc_admin)] == [admin_url] and service.to_answer(pge_admin) == []

    signed_in(cpuc_admin)
    assert client.get(url)["Location"] == admin_url  # an administrator reads it on the Feedback page
    client.post(f"{admin_url}start")
    assert Feedback.objects.get().status == "looking"
    client.post(f"{admin_url}note", {"body": "Probably the legacy import."})
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(f"{admin_url}ask", {"body": "Which program administrator?"})
    assert r.status_code == 302 and Feedback.objects.get().status == "waiting"
    told = Notification.objects.get(user=pge_admin)
    assert told.link == url and "question" in told.title and "Which program administrator?" in told.body
    page = page_only(client.get(admin_url).content.decode())
    assert "Waiting for the sender" in page and "Probably the legacy import." in page and "The CPUC asked" in page
    assert service.to_answer(cpuc_admin) == []

    # the sender: My queue, My requests, the page (no staff note), a reply
    signed_in(pge_admin)
    assert [i.url for i in service.questions_for(pge_admin)] == [url]
    assert any(s.kind.key == "feedback_question" and s.items for s in my_queue.sections(pge_admin))
    mine = page_only(client.get("/feedback/mine/").content.decode())
    assert "Waiting for you" in mine and "Where is the 2027 claim?" in mine
    page = page_only(client.get(url).content.decode())
    assert "Which program administrator?" in page and "Probably the legacy import." not in page and "Reply to the CPUC" in page
    assert client.post(f"{url}reply", {"body": " "}).status_code == 200 and Reply.objects.count() == 2
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(f"{url}reply", {"body": "PG&E."})
    assert r.status_code == 302 and Feedback.objects.get().status == "looking" and service.questions_for(pge_admin) == []
    assert Notification.objects.filter(user=cpuc_admin, link=admin_url, body="PG&E.").exists()

    signed_in(cpuc_admin)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"{admin_url}answer", {"body": "It is a legacy import; open Filings and choose 2027."})
    assert Feedback.objects.get().status == "answered"
    assert Notification.objects.filter(user=pge_admin, title__contains="answered").count() == 1

    signed_in(pge_admin)
    assert "The CPUC answered" in page_only(client.get(url).content.decode())
    r = client.post(f"{url}settle")
    fb.refresh_from_db()
    assert (fb.status, fb.closed_by) == ("closed", pge_admin)
    page = page_only(client.get(url).content.decode())
    assert "This request is closed" in page and "This is settled" not in page
    assert client.post(f"{url}reply", {"body": "One more thing"}).status_code == 200 and Reply.objects.count() == 4
    assert [e.action for e in AuditEvent.objects.filter(object_type="feedback.feedback").order_by("id")] == [
        "feedback.received",
        "feedback.started",
        "feedback.note_added",
        "feedback.question_asked",
        "feedback.reply_received",
        "feedback.answered",
        "feedback.settled",
    ]


def test_a_request_is_read_by_its_sender_and_the_administrators_only(client, signed_in, cpuc_admin, pge_admin, make_user):
    fb = service.submit(page="/claims/", message="Mine", user=pge_admin)
    anon = service.submit(page="/", message="Signed out")
    other = make_user("other@example.org", "Other Person")
    assert client.get(f"/feedback/{fb.pk}/").status_code == 302  # to sign in
    signed_in(other)
    assert client.get(f"/feedback/{fb.pk}/").status_code == 404
    assert client.post(f"/feedback/{fb.pk}/reply", {"body": "x"}).status_code == 404
    assert client.post(f"/feedback/{fb.pk}/settle").status_code == 404
    assert client.post(f"/admin/feedback/{fb.pk}/answer", {"body": "x"}).status_code == 404
    assert "Mine" not in page_only(client.get("/feedback/mine/").content.decode())
    assert not service.can_read(other, fb) and service.can_read(pge_admin, fb) and service.can_read(cpuc_admin, fb)
    with pytest.raises(AccessDenied):
        service.reply(other, fb, "x")
    with pytest.raises(AccessDenied):
        service.answer(pge_admin, fb, "x")
    # nobody to reply to when it was sent signed out; a closed request takes no answer
    with pytest.raises(service.FeedbackRefused, match="signed out"):
        service.answer(cpuc_admin, anon, "Thanks")
    signed_in(cpuc_admin)
    page = page_only(client.get(f"/admin/feedback/{anon.pk}/").content.decode())
    assert "nobody to reply to" in page and "Send the answer" not in page
    service.close(cpuc_admin, fb, "done")
    with pytest.raises(service.FeedbackRefused, match="closed"):
        service.answer(cpuc_admin, fb, "x")


def test_the_inbox_lists_what_waits_on_the_cpuc_first(client, signed_in, cpuc_admin, pge_admin):
    first = service.submit(page="/a/", message="First question", user=pge_admin, kind="help")
    second = service.submit(page="/b/", message="Second idea", user=pge_admin, kind="idea")
    asked = service.submit(page="/c/", message="Third problem", user=pge_admin, kind="problem")
    service.ask(cpuc_admin, asked, "Which year?")
    signed_in(cpuc_admin)
    page = page_only(client.get("/admin/feedback/").content.decode())
    assert "To answer (2)" in page and "Waiting for the sender (1)" in page
    assert page.index("First question") < page.index("Second idea") and "Third problem" not in page
    page = page_only(client.get("/admin/feedback/?status=todo&kind=idea").content.decode())
    assert "Second idea" in page and "First question" not in page
    assert "Third problem" in page_only(client.get("/admin/feedback/?status=waiting").content.decode())
    assert all(m in page_only(client.get("/admin/feedback/?status=all").content.decode()) for m in ("First", "Second", "Third"))
    assert first.pk and second.pk


def test_my_requests_is_in_the_footer_when_signed_in(client, signed_in, pge_admin):
    assert "/feedback/mine/" not in client.get("/limits/").content.decode()
    signed_in(pge_admin)
    assert '<a href="/feedback/mine/">My requests</a>' in client.get("/limits/").content.decode()


def test_the_form_shows_the_pages_help_and_what_may_answer_a_question(client, signed_in, pge_admin):
    signed_in(pge_admin)
    page = page_only(client.get("/feedback/?from=/limits/&help=releases/limits").content.decode())
    entry = page_help.entries()["releases/limits"]
    assert f"About that page: {entry.title}" in page and "/help/pages/releases/limits/?from=/limits/" in page
    assert 'name="help" value="releases/limits"' in page and "These may answer it" not in page
    # a help key that is not a page's is ignored
    assert "About that page" not in page_only(client.get("/feedback/?from=/limits/&help=no/such").content.decode())

    # a question that Help may answer is held once, with what matches; nothing is sent
    question = {"page": "/limits/", "help": "releases/limits", "kind": "help", "message": "How do I reset my password or my authenticator?"}
    r = client.post("/feedback/", question)
    page = page_only(r.content.decode())
    assert r.status_code == 200 and not Feedback.objects.exists()
    assert "These may answer it" in page and "has not been sent yet" in page and 'name="checked" value="1"' in page
    assert "How do I reset my password or my authenticator?" in page  # what they wrote is kept
    # sending again sends it
    r = client.post("/feedback/", {**question, "checked": "1"})
    assert r.status_code == 302 and Feedback.objects.get().kind == "help"

    # other kinds go at once; "Check for answers first" holds any kind, and says when nothing matches
    r = client.post("/feedback/", {**question, "kind": "problem"})
    assert r.status_code == 302 and Feedback.objects.count() == 2
    r = client.post("/feedback/", {**question, "kind": "idea", "check": "1"})
    assert r.status_code == 200 and "These may answer it" in page_only(r.content.decode()) and Feedback.objects.count() == 2
    r = client.post("/feedback/", {"page": "/", "kind": "help", "message": "Zzyzx qwertyuiop", "check": "1"})
    assert "Nothing in Help matches" in page_only(r.content.decode())
    # a question nothing matches is not held
    r = client.post("/feedback/", {"page": "/", "kind": "help", "message": "Zzyzx qwertyuiop"})
    assert r.status_code == 302 and Feedback.objects.count() == 3


def test_suggestions_rank_by_shared_telling_words():
    assert suggestions.telling_words("How do I find the page where I upload my claims?") == ["upload", "claims"]
    assert suggestions.matching("the and of it") == [] and suggestions.matching("") == []
    found = suggestions.matching("Where is the filing calendar and when is the deadline for my budget filing?")
    assert 0 < len(found) <= suggestions.MOST and all(m.url.startswith("/help/") for m in found)
    assert any("calendar" in (m.title + m.text).lower() or "deadline" in (m.title + m.text).lower() for m in found)
    assert suggestions.for_page("feedback/form") == (None, None) and suggestions.for_page("") == (None, None)


# ---- who reads and answers: the support desk, the sender's organization's administrators, urgent kinds


def _text(html: str) -> str:
    return unescape(page_only(html))


@pytest.fixture
def desk(make_user, cpuc_admin, cpuc):
    user = make_user("desk@cpuc.example.org", "Dee Desk")
    access.grant(cpuc_admin, user, cpuc, perms.SUPPORT, reason="support desk")
    return user


def test_the_support_permission_is_the_cpucs_alone(make_user, cpuc_admin, pge, pge_admin):
    with pytest.raises(AccessDenied):
        access.grant(cpuc_admin, make_user("x@example.org", "X"), pge, perms.SUPPORT, reason="no")
    assert perms.SUPPORT in perms.CPUC_ONLY and perms.label(perms.SUPPORT) and perms.describe(perms.SUPPORT)


def test_the_support_desk_works_on_requests_but_not_security_concerns(
    client, signed_in, desk, cpuc_admin, pge, pge_admin, settings, django_capture_on_commit_callbacks
):
    settings.CEDARS_FEEDBACK_EMAIL = "team@cpuc.example.org"
    with django_capture_on_commit_callbacks(execute=True):
        ask = service.submit(page="/claims/", message="Where is my claim?", user=pge_admin, organisation=pge, kind="help")
        secret = service.submit(
            page="/account/", message="I can read another PA's draft.", user=pge_admin, organisation=pge, kind="security"
        )
    told = lambda fb: set(Notification.objects.filter(link=f"/admin/feedback/{fb.pk}/").values_list("user__email", flat=True))  # noqa: E731
    assert told(ask) == {cpuc_admin.email, desk.email}
    assert told(secret) == {cpuc_admin.email}  # and no email of it to the team inbox
    assert Notification.objects.get(link=f"/admin/feedback/{secret.pk}/").title.startswith("Urgent: A security concern")
    assert [m.subject for m in mail.outbox if m.to == ["team@cpuc.example.org"]] == ["CEDARS: A question from /claims/"]

    assert service.can_answer(desk) and service.can_answer(desk, ask) and not service.can_answer(desk, secret)
    assert not service.can_read(desk, secret) and not access.is_system_admin(desk)
    with pytest.raises(AccessDenied, match="security concern"):
        service.answer(desk, secret, "x")
    assert [i.url for i in service.to_answer(desk)] == [f"/admin/feedback/{ask.pk}/"]
    assert [i.url for i in service.to_answer(cpuc_admin)] == [f"/admin/feedback/{secret.pk}/", f"/admin/feedback/{ask.pk}/"]
    assert service.to_answer(cpuc_admin)[0].what.startswith("Urgent: ")

    signed_in(desk)
    home = client.get("/").content.decode()
    assert 'href="/admin/feedback/"' in home and "/admin/people/" not in home  # the header offers Feedback, no other admin page
    assert client.get("/admin/people/").status_code in (403, 404)
    page = _text(client.get("/admin/feedback/").content.decode())
    assert "Where is my claim?" in page and "another PA" not in page and "To answer (1)" in page
    assert "A security concern" not in page  # not among the kinds to filter by either
    assert client.get(f"/admin/feedback/{secret.pk}/").status_code == 404
    assert client.post(f"/admin/feedback/{secret.pk}/answer", {"body": "x"}).status_code == 404
    assert client.post(f"/admin/feedback/{secret.pk}/close", {"note": "x"}).status_code == 404
    assert client.get(f"/feedback/{secret.pk}/").status_code == 404
    assert client.get(f"/feedback/{ask.pk}/").status_code == 302  # to the staff's page
    assert client.post(f"/admin/feedback/{ask.pk}/answer", {"body": "It is under 2027."}, follow=True).status_code == 200
    ask.refresh_from_db()
    assert ask.status == "answered" and Reply.objects.get(feedback=ask).author == desk
    assert client.post(f"/admin/feedback/{ask.pk}/close", {"note": "done"}).status_code == 302

    signed_in(cpuc_admin)
    page = _text(client.get("/admin/feedback/").content.decode())
    assert "another PA" in page and "Urgent" in page
    page = _text(client.get(f"/admin/feedback/{secret.pk}/").content.decode())
    assert "only system administrators read it" in page and "the CPUC's system administrators" in page


def test_urgent_requests_are_first(client, signed_in, cpuc_admin, pge_admin):
    service.submit(page="/a/", message="An older question", user=pge_admin, kind="help")
    barrier = service.submit(page="/b/", message="The menu cannot be opened by keyboard", user=pge_admin, kind="barrier")
    assert barrier.is_urgent
    signed_in(cpuc_admin)
    page = _text(client.get("/admin/feedback/").content.decode())
    assert page.index("The menu cannot be opened") < page.index("An older question")
    assert '<strong class="status warn">Urgent</strong>' in page
    assert "Urgent" in _text(client.get(f"/admin/feedback/{barrier.pk}/").content.decode())


def test_an_organizations_administrators_read_its_members_requests(client, signed_in, cpuc_admin, pge, pge_admin, make_user):
    member = make_user("member@example.org", "Mem Ber")
    access.grant(pge_admin, member, pge, "claims.read", reason="filer")
    sent = {
        kind: service.submit(page="/claims/", message=f"A {kind} message", user=member, organisation=pge, kind=kind)
        for kind in ("help", "problem", "barrier", "idea", "comment", "security")
    }
    kept = service.submit(page="/claims/", message="Kept to myself", user=member, organisation=pge, kind="help", private=True)
    alone = service.submit(page="/claims/", message="Acting for nobody", user=member, kind="help")
    service.add_note(cpuc_admin, sent["help"], "A staff note")
    service.answer(cpuc_admin, sent["help"], "The answer")
    for kind, fb in sent.items():
        assert service.can_read(pge_admin, fb) == (kind in ("help", "problem", "barrier")), kind
    assert not service.can_read(pge_admin, kept) and not service.can_read(pge_admin, alone)
    assert service.can_read(cpuc_admin, kept)  # the CPUC still reads what is kept from the organization

    signed_in(pge_admin)
    assert "/feedback/organization/" in _text(client.get("/feedback/mine/").content.decode())
    page = _text(client.get("/feedback/organization/").content.decode())
    assert all(f"A {k} message" in page for k in ("help", "problem", "barrier"))
    assert not any(f"A {k} message" in page for k in ("idea", "comment", "security"))
    assert "Kept to myself" not in page and "Acting for nobody" not in page and "Mem Ber" in page
    page = _text(client.get(f"/feedback/{sent['help'].pk}/").content.decode())
    assert "The answer" in page and "A staff note" not in page and "Mem Ber" in page
    assert 'id="read-only"' in page and "reply-h" not in page and "settle-h" not in page
    assert "PGE's administrators" in page  # who reads it
    for fb in (sent["idea"], sent["security"], kept, alone):
        assert client.get(f"/feedback/{fb.pk}/").status_code == 404
    assert client.post(f"/feedback/{sent['help'].pk}/reply", {"body": "x"}).status_code == 404
    assert client.post(f"/feedback/{sent['help'].pk}/settle").status_code == 404
    with pytest.raises(AccessDenied):
        service.reply(pge_admin, sent["help"], "x")
    with pytest.raises(AccessDenied):
        service.answer(pge_admin, sent["help"], "x")

    signed_in(member)  # not an administrator: no list, and their own page says who reads each
    assert client.get("/feedback/organization/").status_code == 404
    assert "/feedback/organization/" not in _text(client.get("/feedback/mine/").content.decode())
    assert "PGE's administrators" in _text(client.get(f"/feedback/{sent['problem'].pk}/").content.decode())
    assert "PGE's administrators" not in _text(client.get(f"/feedback/{kept.pk}/").content.decode())
    own = _text(client.get(f"/feedback/{sent['security'].pk}/").content.decode())
    assert "the CPUC's system administrators" in own and "PGE's administrators" not in own


def test_the_form_offers_to_keep_a_request_private(client, signed_in, pge, pge_admin):
    assert 'name="private"' not in client.get("/feedback/?from=/limits/").content.decode()
    signed_in(pge_admin)
    page = _text(client.get("/feedback/?from=/limits/").content.decode())
    assert 'name="private"' in page and "Keep this from PGE's administrators" in page
    assert "accessibility barrier" in page and "security concern" in page
    client.post("/feedback/", {"page": "/limits/", "kind": "problem", "message": "The limits page is blank.", "private": "on"})
    fb = Feedback.objects.get()
    assert fb.private and fb.kind == "problem" and fb.organisation == pge


def test_the_cpucs_own_requests_are_shared_with_nobody_else(client, signed_in, cpuc, cpuc_admin):
    fb = service.submit(page="/claims/", message="A staff question", user=cpuc_admin, organisation=cpuc, kind="help", private=True)
    assert not fb.private and service.readers(fb) == "the CPUC's CEDARS team"
    signed_in(cpuc_admin)
    assert 'name="private"' not in client.get("/feedback/?from=/limits/").content.decode()
    assert client.get("/feedback/organization/").status_code == 404
    assert "/feedback/organization/" not in _text(client.get("/feedback/mine/").content.decode())
