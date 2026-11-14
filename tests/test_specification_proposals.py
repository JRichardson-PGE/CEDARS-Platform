"""Proposed specification versions (cedars.services.specifications: editor.propose and comments.py): a CPUC system administrator
proposes a draft that passes its checks, it is frozen (fields, rules, value lists) and read by anyone signed in, who comments on it
until the comment period closes; the CPUC answers, returns it to draft to act on the comments, proposes it again or publishes it once
comments have closed. Comments are never changed, and stay with the published version."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, InternalError, ProgrammingError, transaction
from django.utils import timezone

from cedars.core import queue as my_queue
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification, Subscription
from cedars.services.specifications import comments, editor
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import SpecComment, Specification, SpecificationVersion

from .conftest import page_only

pytestmark = pytest.mark.django_db
REFUSED = (InternalError, ProgrammingError, IntegrityError)


def current(code: str) -> SpecificationVersion:
    return specifications.current(Specification.objects.get(code=code))


def field(v, table: str, name: str):
    return v.tables.get(name=table).fields.get(name=name)


def soon(days: int):
    return timezone.localdate() + timedelta(days=days)


@pytest.fixture
def starting(db):
    """The starting versions (published by a migration, which transactional tests flush, so published again)."""
    import importlib

    from django.apps import apps

    if not Specification.objects.exists():
        importlib.import_module("cedars.services.specifications.migrations.0002_starting_versions").load_starting(apps, None)


@pytest.fixture
def draft(cpuc_admin, starting):
    v = editor.start_draft(cpuc_admin, current("monthly"), "Monthly reports 2029", "The 2029 reporting changes")
    editor.change_field(cpuc_admin, field(v, "Commitments", "Comments"), guidance="Say what changed this month.", reason="Clearer")
    return v


@pytest.fixture
def proposed(cpuc_admin, draft):
    return editor.propose(cpuc_admin, draft, last_day=soon(10), note="Look at the new guidance on Comments.")


def _close(v):
    """End the comment period without the editor (as if its last day had passed)."""
    SpecificationVersion.objects.filter(pk=v.pk).update(comments_close_at=timezone.now() - timedelta(minutes=1))
    v.refresh_from_db()
    return v


# --- proposing


def test_proposing_freezes_the_draft_and_opens_comments(cpuc_admin, proposed):
    v = proposed
    assert v.status == "proposed" and v.proposals == 1 and v.proposed_by == cpuc_admin and v.open_for_comment
    assert v.comments_last_day == soon(10)
    assert AuditEvent.objects.filter(action="specification.proposed", object_id=str(v.pk)).exists()
    with pytest.raises(editor.EditError, match="return it to draft"):
        editor.change_field(cpuc_admin, field(v, "Commitments", "Comments"), guidance="again")
    with transaction.atomic(), pytest.raises(REFUSED):  # the database refuses too
        SpecificationVersion.objects.filter(pk=v.pk).update(summary="changed")
    with transaction.atomic(), pytest.raises(REFUSED):
        field(v, "Commitments", "Comments").__class__.objects.filter(pk=field(v, "Commitments", "Comments").pk).update(guidance="x")
    # one draft or proposed version per specification
    with pytest.raises(editor.EditError, match="already has a proposed version"):
        editor.start_draft(cpuc_admin, current("monthly"), "Another", "why")


def test_a_draft_is_proposed_only_when_it_passes_its_checks_and_by_the_cpuc(cpuc_admin, pge_admin, starting):
    v = editor.start_draft(cpuc_admin, current("monthly"), "Monthly reports 2029", "why")
    with pytest.raises(editor.EditError, match="cannot be proposed yet: Nothing changes"):
        editor.propose(cpuc_admin, v, last_day=soon(10))
    editor.change_field(cpuc_admin, field(v, "Commitments", "Comments"), guidance="Say what changed.", reason="Clearer")
    with pytest.raises(AccessDenied):
        editor.propose(pge_admin, v, last_day=soon(10))
    with pytest.raises(editor.EditError, match="today or later"):
        editor.propose(cpuc_admin, v, last_day=soon(-1))
    assert editor.propose(cpuc_admin, v, last_day=timezone.localdate()).open_for_comment  # through the end of today


def test_the_rules_of_a_proposed_version_do_not_change(cpuc_admin, starting):
    rules = pytest.importorskip("cedars.services.rules.service")
    pytest.importorskip("recet.filing_rules")
    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "budget_filing"))
    doc = specifications.stored_starting("budget_filing")
    doc["version"]["name"] = "Budget filings 2029"
    base = specifications.load(doc, rule_set=rs)
    v = editor.start_draft(cpuc_admin, base, "Budget filings 2030", "New rules")
    rule = v.rule_set.rules.first()
    rules.remove_rule(cpuc_admin, rule, "Not needed")
    SpecificationVersion.objects.filter(pk=v.pk).update(status="proposed")  # its examples aside: the rules are what is tested
    with pytest.raises(AccessDenied, match="proposed for comment"):
        rules.remove_rule(cpuc_admin, v.rule_set.rules.first(), "again")
    with pytest.raises(AccessDenied, match="published with it"):
        rules.publish(cpuc_admin, v.rule_set)


def test_the_codes_of_a_list_made_for_a_proposed_version_do_not_change(cpuc_admin, draft):
    from cedars.services.dictionary import value_lists

    editor.add_field(cpuc_admin, draft, "Commitments", "ReportingNote", type="text", max_length=500, definition="A note")
    vl = editor.new_value_list(
        cpuc_admin, field(draft, "Commitments", "ReportingNote"), "reporting_note_kind", codes="Delay, A delay", reason="New codes"
    )
    lst = getattr(vl, "value_list", vl)
    from cedars.services.specifications import examples

    examples.check_all(draft)
    v = editor.propose(cpuc_admin, draft, last_day=soon(5))
    assert v.status == "proposed"
    with pytest.raises(value_lists.ValueListError, match="proposed for comment"):
        value_lists.add_code(cpuc_admin, value_lists.draft_of(lst), "Other", description="Anything else")


# --- comments


def test_filers_comment_while_comments_are_open_and_the_cpuc_answers(
    cpuc_admin, pge_admin, pge, proposed, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        c = comments.post(pge_admin, proposed, "Please keep Comments optional.", about="Commitments.Comments", organisation=pge)
    assert c.round == 1 and c.organisation == pge and c.about == "Commitments.Comments" and c.author_name
    # the proposer hears of it, once
    assert Notification.objects.filter(user=cpuc_admin, key=comments.notice_key(c)).count() == 1
    assert comments.counts(proposed) == {"comments": 1, "unanswered": 1}
    queued = my_queue.sections(cpuc_admin)
    assert any(s.kind.key == "specification_comments" and s.items for s in queued)
    assert not any(s.kind.key == "specification_comments" and s.items for s in my_queue.sections(pge_admin))
    with django_capture_on_commit_callbacks(execute=True):
        a = comments.reply(cpuc_admin, c, "It stays optional.")
    assert a.reply_to == c and a.organisation.code == comments.side(cpuc_admin).code
    assert Notification.objects.filter(user=pge_admin, key=comments.notice_key(a)).exists()
    assert comments.counts(proposed) == {"comments": 1, "unanswered": 0}
    assert comments.thread(proposed)[0]["replies"] == [a]
    with pytest.raises(AccessDenied):
        comments.reply(pge_admin, c, "I answer myself")
    with pytest.raises(comments.CommentRefused, match="no field"):
        comments.post(pge_admin, proposed, "x", about="Commitments.Nope", organisation=pge)
    with pytest.raises(comments.CommentRefused, match="Write the comment"):
        comments.post(pge_admin, proposed, "  ", organisation=pge)
    events = AuditEvent.objects.filter(action="specification.commented", object_id=str(proposed.pk))
    assert events.count() == 2


def test_a_comment_needs_an_organization_and_an_open_comment_period(cpuc_admin, pge_admin, pge, make_user, proposed, draft):
    loner = make_user("loner@example.org", "Lone Reader")
    with pytest.raises(AccessDenied):
        comments.post(loner, proposed, "Hello", organisation=None)
    with pytest.raises(AccessDenied):  # an organization the person is not a member of
        comments.post(loner, proposed, "Hello", organisation=pge)
    _close(proposed)
    with pytest.raises(comments.CommentRefused, match="closed"):
        comments.post(pge_admin, proposed, "Too late", organisation=pge)


def test_comments_are_never_changed(pge_admin, pge, proposed):
    c = comments.post(pge_admin, proposed, "A comment", organisation=pge)
    with transaction.atomic(), pytest.raises(REFUSED):
        SpecComment.objects.filter(pk=c.pk).update(text="changed")


def test_subscribers_hear_of_proposals_and_comments(cpuc_admin, pge_admin, pge, make_user, draft, django_capture_on_commit_callbacks):
    reader = make_user("reader@example.org", "Rea Der")
    Subscription.objects.create(user=reader, event="specification.proposed", channel="page")
    Subscription.objects.create(user=reader, event="specification.commented", channel="page")
    with django_capture_on_commit_callbacks(execute=True):
        v = editor.propose(cpuc_admin, draft, last_day=soon(7), note="See Comments.")
    told = Notification.objects.filter(user=reader)
    assert told.filter(title__startswith="Proposed for comment: Monthly reports").exists()
    assert f"/specifications/monthly/{v.slug}/" in told.get().link
    with django_capture_on_commit_callbacks(execute=True):
        c = comments.post(pge_admin, v, "A comment", organisation=pge)
    n = Notification.objects.filter(user=reader, key=comments.notice_key(c)).get()
    assert "Comment from PGE" in n.title and n.link.endswith(f"#comment-{c.pk}")


# --- the comment period, returning to draft and publishing


def test_the_comment_period_moves_or_ends(cpuc_admin, proposed):
    v = editor.change_comment_period(cpuc_admin, proposed, last_day=soon(20), reason="More time asked for")
    assert v.comments_last_day == soon(20)
    with pytest.raises(editor.EditError, match="Say why"):
        editor.end_comments(cpuc_admin, v, "")
    v = editor.end_comments(cpuc_admin, v, "Enough comments")
    assert v.status == "proposed" and not v.open_for_comment
    assert AuditEvent.objects.filter(action="specification.comment_period_changed").count() == 2


def test_publishing_waits_for_comments_to_close_then_keeps_them(cpuc_admin, pge_admin, pge, proposed):
    c = comments.post(pge_admin, proposed, "Fine by us", organisation=pge)
    with pytest.raises(editor.EditError, match="Comments on .* are open"):
        editor.publish(cpuc_admin, proposed, effective_on=soon(20))
    _close(proposed)
    v = editor.publish(cpuc_admin, proposed, effective_on=soon(20))
    assert v.status == "published" and v.proposals == 1 and v.fingerprint
    assert list(v.comments.all()) == [c]
    with pytest.raises(comments.CommentRefused, match="closed record"):
        comments.reply(cpuc_admin, c, "Too late")
    assert AuditEvent.objects.get(action="specification.published").after["proposed"] == 1


def test_returning_to_draft_acts_on_comments_and_a_second_proposal_is_a_new_round(cpuc_admin, pge_admin, pge, proposed):
    first = comments.post(pge_admin, proposed, "Add a unit", organisation=pge, about="Commitments")
    v = editor.return_to_draft(cpuc_admin, proposed, "Acting on PGE's comment")
    assert v.status == "draft" and not v.open_for_comment
    comments.reply(cpuc_admin, first, "Added in the draft")  # staff still answer in draft
    editor.change_field(cpuc_admin, field(v, "Commitments", "Comments"), unit="words", reason="PGE asked")
    v = editor.propose(cpuc_admin, v, last_day=soon(3))
    assert v.proposals == 2
    second = comments.post(pge_admin, v, "Thanks", organisation=pge)
    assert second.round == 2 and [row["c"].round for row in comments.thread(v)] == [1, 2]


# --- pages


def test_a_proposed_version_is_read_and_commented_on_by_anyone_signed_in(client, signed_in, cpuc_admin, pge_admin, pge, proposed):
    url = f"/specifications/monthly/{proposed.slug}/"
    assert client.get(url).status_code == 404  # not public
    c = signed_in(pge_admin)
    page = c.get(url).content.decode()
    assert "Comments are open through" in page and 'id="comment-form"' in page and "Look at the new guidance" in page
    index = c.get("/specifications/").content.decode()
    assert 'id="open-for-comment"' in index and proposed.name in index
    r = c.post(url + "comments/", {"about": "Commitments.Comments", "text": "Keep it optional, please."})
    assert r.status_code == 302 and "#comment-" in r["Location"]
    made = SpecComment.objects.get()
    assert made.organisation == pge and made.field == "Comments"
    page = c.get(url).content.decode()
    assert "Keep it optional, please." in page and "Not answered" in page
    assert "Answer " not in page_only(page)  # only the CPUC answers
    staff = signed_in(cpuc_admin)
    page = staff.get(url).content.decode()
    assert 'id="proposal-h"' in page and 'id="return-form"' in page and 'id="end-comments-form"' in page
    r = staff.post(url + f"comments/{made.pk}/answer", {"text": "It stays optional."})
    assert r.status_code == 302
    assert "It stays optional." in signed_in(pge_admin).get(url).content.decode()


def test_the_editor_pages_of_a_proposed_version(signed_in, cpuc_admin, pge_admin, pge, proposed):
    base = f"/specifications/monthly/{proposed.slug}/edit/"
    staff = signed_in(cpuc_admin)
    assert staff.get(base).status_code == 302  # frozen: the fields page sends you to the version
    for path in ("changes/", "impact/", "comments/", "publish/"):
        assert staff.get(base + path).status_code == 200, path
    page = staff.get(base + "publish/").content.decode()
    assert 'id="publish-comments"' in page and "Comments open" in page and 'id="propose-form"' not in page
    r = staff.post(f"/specifications/monthly/{proposed.slug}/return-to-draft", {"reason": "Acting on comments"})
    assert r.status_code == 302 and r["Location"].endswith("/edit/comments/")
    proposed.refresh_from_db()
    assert proposed.status == "draft"
    page = staff.get(base + "publish/").content.decode()
    assert 'id="propose-form"' in page
    r = staff.post(base + "propose", {"last_day": soon(4).isoformat(), "note": ""})
    assert r.status_code == 302
    proposed.refresh_from_db()
    assert proposed.status == "proposed" and proposed.proposals == 2
    r = staff.post(f"/specifications/monthly/{proposed.slug}/comment-period", {"last_day": soon(9).isoformat(), "reason": "Asked"})
    proposed.refresh_from_db()
    assert proposed.comments_last_day == soon(9)
    staff.post(f"/specifications/monthly/{proposed.slug}/end-comments", {"reason": "Done"})
    r = staff.post(base + "publish/", {"effective_on": soon(20).isoformat(), "changes": "Guidance on Comments.", "action": ""})
    assert r.status_code == 302
    proposed.refresh_from_db()
    assert proposed.status == "published"
    # a filer cannot do any of it
    assert signed_in(pge_admin).post(f"/specifications/monthly/{proposed.slug}/end-comments", {"reason": "x"}).status_code == 403


def test_a_file_is_checked_against_the_rules_of_a_proposed_version(signed_in, cpuc_admin, pge_admin, starting):
    rules = pytest.importorskip("cedars.services.rules.service")
    pytest.importorskip("recet.filing_rules")
    from django.core.files.uploadedfile import SimpleUploadedFile

    from cedars.services.rules import precheck

    from .test_rules import _measure_csv

    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "budget_filing"))
    doc = specifications.stored_starting("budget_filing")
    doc["version"]["name"] = "Budget filings 2029"
    base = specifications.load(doc, rule_set=rs)
    v = editor.start_draft(cpuc_admin, base, "Budget filings 2030", "New rules")
    assert precheck.checkable(v.rule_set.pk) is None  # a draft's rules are not offered
    SpecificationVersion.objects.filter(pk=v.pk).update(status="proposed", proposals=1)
    assert precheck.checkable(v.rule_set.pk) == v.rule_set
    assert precheck.release_for(precheck.checker_for(module="budgets"), v.rule_set) == specifications.engine_release_of(v)
    c = signed_in(pge_admin)
    page = c.get(f"/rules/check/?rule_set={v.rule_set.pk}").content.decode()
    assert f'<option value="{v.rule_set.pk}" selected>Budget and application filings: Budget filings 2030 (proposed for comment)' in page
    upload = SimpleUploadedFile("m.csv", _measure_csv([{"CEInputID": "M1", "NumUnits": "abc"}]))
    page = c.post("/rules/check/", {"rule_set": v.rule_set.pk, "table": "Measure", "file": upload}).content.decode()
    assert "would be rejected" in page
    assert f"/rules/check/?rule_set={v.rule_set.pk}" in c.get(f"/specifications/budget_filing/{v.slug}/").content.decode()
