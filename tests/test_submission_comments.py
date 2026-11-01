"""Comments and attachments on filing versions, and each user's queue (BR-09.5)."""

import io
from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.utils import DatabaseError
from django.template import Context, Template
from django.test import RequestFactory
from django.utils import timezone

from cedars.core import queue, storage
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from cedars.services.submissions import comments
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Attachment, Comment, SubmissionVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def analyst(make_user, pge, pge_admin):
    u = make_user("analyst@pge.com", "Ana Lyst")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


@pytest.fixture
def approver(make_user, cpuc_admin, cpuc):
    u = make_user("approver@cpuc.ca.gov", "Avery Prover")
    access.grant(cpuc_admin, u, cpuc, "claims.approve")
    return u


@pytest.fixture
def deadline(cpuc_admin):
    period = [p for p in registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]
    return submissions.add_deadline(cpuc_admin, module="claims", kind="Q1 claim", period=period, due_at=timezone.now() + timedelta(days=10))


def _priced(analyst, deadline, pge):
    v = submissions.start(analyst, deadline, pge)
    submissions.attach_files(analyst, v, [{"name": "Measure.csv", "key": "k", "sha256": "abc", "bytes": 10}])
    submissions.record_validation(analyst, v, None, {"records": 10, "rejected": 0, "warnings": 0})
    submissions.record_calculation(None, v, run="run-1", input_hash="h1")
    return SubmissionVersion.objects.get(pk=v.pk)


def _pdf(name="memo.pdf", body=b"%PDF-1.4 a memo"):
    return (name, io.BytesIO(body))


def test_both_sides_comment_with_files_and_each_is_audited(analyst, approver, deadline, pge, cpuc):
    v = _priced(analyst, deadline, pge)
    c1 = comments.post(approver, v, "Line 12's NTG looks high: see the memo.", [_pdf()])
    c2 = comments.post(analyst, v, "Fixed in the next upload.")
    assert (c1.organisation, c2.organisation) == (cpuc, pge)
    assert c1.author_name == "Avery Prover"
    (a,) = c1.attachments.all()
    assert a.name == "memo.pdf" and a.size == len(b"%PDF-1.4 a memo") and a.content_type == "application/pdf"
    assert storage.store("uploads").get_bytes(a.key) == b"%PDF-1.4 a memo"
    assert a.key.startswith(f"submissions/versions/{v.pk}/comments/{c1.pk}/")
    events = AuditEvent.objects.filter(object_type="submissions.submissionversion", object_id=str(v.pk), action="submission.commented")
    assert [e.after["side"] for e in events.order_by("id")] == ["CPUC", "PGE"]
    assert events.order_by("id").first().after["files"] == [{"name": "memo.pdf", "sha256": a.sha256, "bytes": a.size}]
    assert list(comments.thread(v.submission)) == [c1, c2]


def test_only_the_filer_and_cpuc_staff_take_part(analyst, deadline, pge, make_user, cpuc_admin):
    v = _priced(analyst, deadline, pge)
    other_pa = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    outsider = make_user("someone@sce.com")
    access.grant(None, outsider, other_pa, "claims.submit")
    assert not comments.can_discuss(outsider, v.submission)
    with pytest.raises(AccessDenied):
        comments.post(outsider, v, "Hello")
    c = comments.post(analyst, v, "Our note", [_pdf()])
    with pytest.raises(AccessDenied):
        comments.attachment_for(outsider, c.attachments.get().pk)
    assert comments.can_discuss(cpuc_admin, v.submission)  # a CPUC system administrator


def test_refusals(analyst, deadline, pge, settings):
    v = _priced(analyst, deadline, pge)
    with pytest.raises(comments.CommentRefused, match="Write the comment"):
        comments.post(analyst, v, "   ")
    settings.CEDARS_COMMENT_MAX_CHARS = 10
    with pytest.raises(comments.CommentRefused, match="10 characters"):
        comments.post(analyst, v, "x" * 11)
    settings.CEDARS_COMMENT_MAX_CHARS = 4000
    with pytest.raises(comments.CommentRefused, match="not accepted"):
        comments.post(analyst, v, "a program", [("tool.exe", io.BytesIO(b"MZ"))])
    with pytest.raises(comments.CommentRefused, match="same name"):
        comments.post(analyst, v, "twice", [_pdf(), _pdf()])
    with pytest.raises(comments.CommentRefused, match="empty"):
        comments.post(analyst, v, "nothing", [_pdf(body=b"")])
    assert not Comment.objects.exists() and not Attachment.objects.exists()  # a refused file keeps nothing


def test_a_comment_is_never_edited(analyst, deadline, pge):
    v = _priced(analyst, deadline, pge)
    c = comments.post(analyst, v, "As filed")
    with pytest.raises(DatabaseError, match="never changed"), connection.cursor() as cur:
        cur.execute("UPDATE submissions_comment SET text = 'changed' WHERE id = %s", [c.pk])


def test_comments_run_across_versions_and_go_with_an_expired_draft(analyst, deadline, pge):
    v = _priced(analyst, deadline, pge)
    c = comments.post(analyst, v, "On the draft", [_pdf()])
    key = c.attachments.get().key
    for _label, delete in submissions.expired_drafts(timezone.now() + timedelta(days=1)):
        delete()
    assert not Comment.objects.exists()
    assert not storage.store("uploads").exists(key)


def test_pages_post_show_and_download(client, signed_in, analyst, approver, deadline, pge):
    v = _priced(analyst, deadline, pge)
    signed_in(approver)
    f = SimpleUploadedFile("notes.txt", b"see line 12", content_type="text/plain")
    r = client.post(f"/filings/versions/{v.pk}/comments", {"text": "Please check line 12", "files": [f]})
    c = Comment.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/claims/versions/{v.pk}/#comment-{c.pk}"
    a = c.attachments.get()
    r = client.get(f"/filings/attachments/{a.pk}")
    assert r.status_code == 200 and b"".join(r.streaming_content) == b"see line 12"
    assert AuditEvent.objects.filter(action="submission.attachment_downloaded", object_id=str(v.pk)).count() == 1
    r = client.post(f"/filings/versions/{v.pk}/comments", {"text": ""})
    assert r.status_code == 302 and Comment.objects.count() == 1

    request = RequestFactory().get("/")
    request.user = analyst
    html = Template("{% load filing_comments %}{% filing_comments v %}").render(Context({"request": request, "v": v, "csrf_token": "t"}))
    assert "Please check line 12" in html and "notes.txt" in html and "Avery Prover" in html and "approver@" not in html
    assert 'for="comment-text"' in html and 'for="comment-files"' in html
    request.user = access.User.objects.create_user(email="public@example.com", password="x")
    html = Template("{% load filing_comments %}{% filing_comments v %}").render(Context({"request": request, "v": v}))
    assert "Please check line 12" not in html


def test_the_queue_shows_each_side_its_work(client, signed_in, analyst, approver, deadline, pge):
    v = _priced(analyst, deadline, pge)

    def kinds(user):
        return {s.kind.key: s.items for s in queue.sections(user)}

    assert len(kinds(analyst)["confirm"]) == 1  # priced on its current rows: ready to confirm
    assert kinds(approver)["confirm"] == []
    comments.post(analyst, v, "Question for the CPUC")
    assert len(kinds(approver)["reply"]) == 1 and kinds(analyst)["reply"] == []
    comments.post(approver, v, "Answered")
    assert kinds(approver)["reply"] == [] and len(kinds(analyst)["reply"]) == 1

    v = submissions.confirm(analyst, v, "h1")
    assert kinds(analyst)["confirm"] == []
    submissions.move_deadline(None, deadline, timezone.now() - timedelta(days=1), "moved for the test")
    submissions.request_reopen(analyst, v.submission, "Two measures were counted twice")
    (item,) = kinds(approver)["reopen"]
    assert item.organisation == "PGE" and "counted twice" in item.note
    assert kinds(analyst)["reopen"] == []

    signed_in(approver)
    r = client.get("/queue/")
    assert r.status_code == 200
    page = r.content.decode()
    assert "Correction requests to decide" in page and "counted twice" in page and "/filings/reopen-requests/" in page


def test_a_delegate_sees_the_approvers_requests(analyst, approver, deadline, pge, make_user):
    v = submissions.confirm(analyst, _priced(analyst, deadline, pge), "h1")
    submissions.move_deadline(None, deadline, timezone.now() - timedelta(days=1), "moved for the test")
    submissions.request_reopen(analyst, v.submission, "A late invoice")
    delegate = make_user("delegate@cpuc.ca.gov", "Del Egate")
    submissions.delegate(approver, module="claims", delegate_to=delegate, starts=date.today(), ends=date.today() + timedelta(days=5))
    (item,) = {s.kind.key: s.items for s in queue.sections(delegate)}["reopen"]
    assert "Avery Prover's delegate" in item.note


def test_a_failing_provider_does_not_hide_the_rest(analyst):
    def broken(user):
        raise RuntimeError("boom")

    queue.register("broken", "Broken", broken, action="Go", order=1)
    try:
        assert "broken" not in {s.kind.key for s in queue.sections(analyst)}
        assert "confirm" in {s.kind.key for s in queue.sections(analyst)}
    finally:
        queue._kinds.pop("broken")


def test_the_queue_needs_sign_in(client):
    r = client.get("/queue/")
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]
