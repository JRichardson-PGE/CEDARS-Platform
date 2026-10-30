import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import InternalError, ProgrammingError, transaction
from django.utils import timezone

from cedars.modules.documents import comments, deliverables, service, studies
from cedars.modules.documents.models import Comment, CommentResponse, DocumentVersion, Stage
from cedars.services.accounts import service as access
from cedars.services.accounts.models import User
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from tests.conftest import sign_in
from tests.test_documents import details, make_people, option, upload

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()


def days(n):
    return TODAY + timedelta(days=n)


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    p = make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)
    access.grant(User.objects.get(email="admin@dnv.com"), p["evaluator"], p["dnv"], "documents.confirm")
    p["public"] = make_user("resident@example.org", "Rae Resident")
    return p


@pytest.fixture
def study(people):
    return studies.create_study(
        people["ed"],
        None,
        studies.StudyData(
            title="Residential HVAC impact evaluation",
            manager=people["dnv"],
            lead=option("study_lead", "CPUC"),
            sectors=[option("sector", "Residential")],
            budget=Decimal(500000),
        ),
    )


@pytest.fixture
def deliverable(people, study):
    """The evaluator's first draft workplan, published with a comment window open now."""
    d = details("emv", "Evaluation Plan", "Residential HVAC impact evaluation", study=study)
    return service.create_document(
        people["evaluator"],
        people["dnv"],
        "emv",
        d,
        [upload("workplan_draft1.pdf")],
        publish=True,
        deliverable=True,
        stage=Stage.DRAFT_WORKPLAN,
        comment_period=(days(-3), days(10)),
    )


def note(kind="comment", subject="Sample frame", text="Please include heat pump water heaters.", section="3.1"):
    return comments.NewComment(kind=kind, subject=subject, text=text, section=section)


def close(actor, version):
    comments.set_period(actor, version, days(-30), days(-1), reason="closed for the test")


def test_a_deliverable_belongs_to_a_study_and_starts_at_a_work_stage(people, study):
    ev, dnv = people["evaluator"], people["dnv"]
    with pytest.raises(service.DocumentError, match="name its study"):
        service.create_document(ev, dnv, "emv", details("emv", "Report"), [upload()], deliverable=True, stage=Stage.DRAFT_WORKPLAN)
    with pytest.raises(service.DocumentError, match="stage"):
        service.create_document(
            ev, dnv, "emv", details("emv", "Report", study=study), [upload()], deliverable=True, stage=Stage.PA_RESPONSE
        )
    with pytest.raises(service.DocumentError, match="Only a deliverable"):
        service.create_document(ev, dnv, "emv", details("emv", "Report"), [upload()], stage=Stage.DRAFT_WORKPLAN)
    with pytest.raises(service.DocumentError, match="close on or after"):
        service.create_document(ev, dnv, "emv", details("emv", "Report"), [upload()], comment_period=(days(5), days(1)))


def test_the_whole_revision_workflow_on_one_record(people, pge, deliverable, client):
    """BR-15.11's acceptance test: two workplan drafts with comments, the final workplan, draft results with comments, a final revised
    once, one PA response, all on the deliverable's page in order; search lists it once, at the stage PA responses."""
    ev, ed, public = people["evaluator"], people["ed"], people["public"]
    first = deliverable.versions.get()
    comments.post(public, None, first, note())
    close(ev, first)
    second = service.add_version(
        ev,
        people["dnv"],
        deliverable,
        [upload("workplan_draft2.pdf")],
        reason="Revised after comments",
        publish=True,
        stage=Stage.DRAFT_WORKPLAN,
        comment_period=(days(-2), days(12)),
    )
    assert second.stage_label == "Draft workplan 2"
    comments.post(public, None, second, note(subject="Thanks", text="The revision addresses it."))
    close(ev, second)
    final_wp = service.add_version(
        ev, None, deliverable, [upload("workplan_final.pdf")], reason="Final", publish=True, stage=Stage.FINAL_WORKPLAN
    )
    with pytest.raises(service.DocumentError, match="cannot go back"):
        service.add_version(ev, None, deliverable, [upload("late.pdf")], reason="x", stage=Stage.DRAFT_WORKPLAN)
    draft_results = service.add_version(
        ev,
        None,
        deliverable,
        [upload("results_draft.pdf")],
        reason="Draft results",
        publish=True,
        stage=Stage.DRAFT_RESULTS,
        comment_period=(days(-1), days(13)),
    )
    assert deliverables.stage_label(deliverable, draft_results).startswith("Study-result comments")
    comments.post(public, None, draft_results, note(subject="Weather normalisation", kind="question"))
    close(ev, draft_results)
    service.add_version(ev, None, deliverable, [upload("results_final.pdf")], reason="Final", publish=True, stage=Stage.FINAL_RESULTS)
    with pytest.raises(service.DocumentError):
        service.add_version(ev, None, deliverable, [upload("x.pdf")], reason="x", stage=Stage.ED_REPLY)  # ED only
    revised = service.add_version(
        ev, None, deliverable, [upload("results_final_rev1.pdf")], reason="Table 5 corrected", publish=True, stage=Stage.FINAL_RESULTS
    )
    assert revised.stage_label == "Final study results, revision 1"
    deliverables.request_responses(ed, deliverable, [pge], days(30))
    response = deliverables.file_response(people["pge_writer"], pge, deliverable, [upload("pge_response.pdf")], publish=True)
    assert response.stage_label == "PA response to the recommendations (PGE)" and response.organisation == pge

    labels = [v.stage_label for v in deliverables.history(service.visible_versions(AnonymousUser(), deliverable))]
    assert labels == [
        "Draft workplan 1",
        "Draft workplan 2",
        "Final workplan",
        "Draft study results 1",
        "Final study results",
        "Final study results, revision 1",
        "PA response to the recommendations (PGE)",
    ]
    assert final_wp.number == 3
    page = client.get(deliverable.get_absolute_url()).content.decode()
    assert "Revision history" in page and "Table 5 corrected" in page and "Current stage: <strong>PA responses</strong>" in page
    assert page.index("Draft workplan 1") < page.index("Final workplan") < page.index("Final study results, revision 1")
    assert "Responded" in page
    hits = service.search(None, service.Search(study=deliverable.study))
    assert [d.pk for d in hits] == [deliverable.pk]
    library = client.get("/documents/", {"study": deliverable.study.study_id}).content.decode()
    assert library.count(deliverable.get_absolute_url()) == 1 and "PA responses" in library
    shown = client.get(f"/documents/{deliverable.pk}/comments/").content.decode()
    assert shown.index("Sample frame") < shown.index("Thanks") < shown.index("Weather normalisation")
    # The study page and every program it covers show the deliverable at its stage.
    assert "PA responses" in client.get(deliverable.study.get_absolute_url()).content.decode()
    # The ED reply closes it.
    reply = service.add_version(ed, None, deliverable, [upload("ed_reply.pdf")], reason="ED reply", publish=True, stage=Stage.ED_REPLY)
    assert deliverables.stage_label(deliverable, reply) == "CPUC ED reply"


def test_pa_responses_are_requested_by_ed_and_filed_by_the_pa(people, pge, deliverable, client):
    ev, ed, writer, drafter = people["evaluator"], people["ed"], people["pge_writer"], people["pge_drafter"]
    with pytest.raises(service.DocumentError, match="published final"):
        deliverables.request_responses(ed, deliverable, [pge], days(30))
    service.add_version(ev, None, deliverable, [upload("final.pdf")], reason="Final", publish=True, stage=Stage.FINAL_RESULTS)
    with pytest.raises(AccessDenied):
        deliverables.request_responses(ev, deliverable, [pge], days(30))
    with pytest.raises(service.DocumentError, match="program administrators"):
        deliverables.request_responses(ed, deliverable, [people["dnv"]], days(30))
    with pytest.raises(AccessDenied, match="not asked"):
        deliverables.file_response(writer, people["sce"], deliverable, [upload()])
    [req] = deliverables.request_responses(ed, deliverable, [pge], days(-1))
    assert deliverables.status(req).startswith("Overdue")
    # A PA asked to respond does not manage the deliverable: it files its response only.
    assert not service.can_manage(drafter, deliverable)
    assert deliverables.responding_pas(drafter, deliverable) == [pge]
    v = deliverables.file_response(drafter, pge, deliverable, [upload("response.pdf")])
    assert not v.published and deliverables.status(req) == "Filed, not yet published"
    assert v in service.visible_versions(drafter, deliverable) and v not in service.visible_versions(AnonymousUser(), deliverable)
    assert service.sees_version(drafter, v) and not service.sees_version(people["sce_user"], v)
    with pytest.raises(AccessDenied):
        service.publish_version(drafter, v)  # submit only
    service.publish_version(writer, v)  # PG&E's confirm publishes its own response
    assert deliverables.status(req).startswith("Responded late")
    with pytest.raises(service.DocumentError, match="earlier response"):
        deliverables.file_response(writer, pge, deliverable, [upload("response2.pdf")])
    v2 = deliverables.file_response(writer, pge, deliverable, [upload("response2.pdf")], reason="Corrected appendix")
    assert v2.stage_label.endswith("revision 1")
    deliverables.request_responses(ed, deliverable, [pge], days(60), reason="extension")
    assert AuditEvent.objects.filter(action="documents.response_due_changed").exists()
    sign_in(client, drafter)
    page = client.get(f"/documents/{deliverable.pk}/responses/new")
    assert page.status_code == 200 and "Responding for" in page.content.decode()
    sign_in(client, people["sce_user"])
    assert client.get(f"/documents/{deliverable.pk}/responses/new").status_code == 404


def test_comment_periods_gate_comments(people, deliverable, client):
    """BR-15.4's acceptance test: a public user comments inside the period, the evaluator responds, both show in order; once the period
    closes the form is gone; the report lists the comment, the author's organisation, the section and the response."""
    ev, public = people["evaluator"], people["public"]
    v = deliverable.versions.get()
    assert not comments.can_comment(None, v)
    sign_in(client, public)
    page = client.get(f"/documents/{deliverable.pk}/comments/").content.decode()
    assert "Comment or ask a question" in page
    r = client.post(
        f"/documents/{deliverable.pk}/comments/new",
        {"version": v.number, "kind": "question", "subject": "Baseline", "section": "Page 12", "text": "Which baseline year?"},
    )
    assert r.status_code == 302
    c = Comment.objects.get()
    assert c.author_name == "Rae Resident" and c.organisation is None
    with pytest.raises(AccessDenied):
        comments.respond(people["sce_user"], people["sce"], c, "Not ours")
    comments.respond(ev, people["dnv"], c, "2019, as the workplan says.")
    page = client.get(f"/documents/{deliverable.pk}/comments/").content.decode()
    assert page.index("Which baseline year?") < page.index("2019, as the workplan says.")
    client.logout()
    page = client.get(f"/documents/{deliverable.pk}/comments/").content.decode()
    assert "Rae Resident" in page and "resident@example.org" not in page
    sign_in(client, public)
    close(ev, v)
    assert "Post it" not in client.get(f"/documents/{deliverable.pk}/comments/").content.decode()
    r = client.post(f"/documents/{deliverable.pk}/comments/new", {"version": v.number, "kind": "comment", "subject": "x", "text": "y"})
    assert Comment.objects.count() == 1
    rows = list(csv.reader(io.StringIO(client.get(f"/documents/{deliverable.pk}/comments.csv").content.decode())))
    record = dict(zip(rows[0], rows[1], strict=True))
    assert (
        record["Section"] == "Page 12" and record["Response"] == "2019, as the workplan says." and record["ResponderOrganisation"] == "DNV"
    )
    assert record["StudyID"] == deliverable.study.study_id and record["Type"] == "Question"
    study_rows = client.get(f"/documents/studies/{deliverable.study.number}/comments.csv").content.decode()
    assert "Which baseline year?" in study_rows


def test_withdrawing_and_the_record_never_changing(people, pge, deliverable, client):
    public, ev = people["public"], people["evaluator"]
    v = deliverable.versions.get()
    c = comments.post(public, None, v, note())
    assert not comments.can_withdraw(ev, c)
    comments.withdraw(public, c)
    assert not comments.shown(deliverable).exists()
    with pytest.raises(service.DocumentError):
        comments.withdraw(public, c)
    answered = comments.post(people["pge_writer"], pge, v, note(subject="From PG&E"))
    assert answered.organisation == pge
    comments.respond(people["ed"], access.cpuc(), answered, "Noted.")
    with pytest.raises(service.DocumentError, match="until it is answered"):
        comments.withdraw(people["pge_writer"], answered)
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        Comment.objects.filter(pk=answered.pk).update(text="changed")
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        CommentResponse.objects.filter(comment=answered).update(text="changed")
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        DocumentVersion.objects.filter(pk=v.pk).update(stage=Stage.FINAL_RESULTS)


def test_a_comment_file_and_the_period_change(people, deliverable, client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    public, ev = people["public"], people["evaluator"]
    v = deliverable.versions.get()
    c = comments.post(
        public,
        None,
        v,
        comments.NewComment(
            "comment", "Data", "See the table.", attachment=service.Upload("t.csv", SimpleUploadedFile("t.csv", b"a,b\n1,2\n"))
        ),
    )
    r = client.get(c.get_file_url())
    assert r.status_code == 200 and b"".join(r.streaming_content) == b"a,b\n1,2\n"
    with pytest.raises(service.DocumentError, match="files of type"):
        comments.post(
            public,
            None,
            v,
            comments.NewComment("comment", "x", "y", attachment=service.Upload("x.exe", SimpleUploadedFile("x.exe", b"MZ"))),
        )
    with pytest.raises(AccessDenied):
        comments.set_period(public, v, days(0), days(20))
    with pytest.raises(service.DocumentError, match="reason"):
        comments.set_period(ev, v, days(0), days(20))
    comments.set_period(ev, v, days(-3), days(20), reason="Extended at the PAs' request")
    assert AuditEvent.objects.filter(action="documents.comment_period_set", reason="Extended at the PAs' request").exists()


def test_the_new_document_and_version_pages_take_the_stage(people, study, client):
    sign_in(client, people["evaluator"])
    page = client.get("/documents/new/?collection=emv").content.decode()
    assert "Track it as a study deliverable" in page and "Comments open" in page
    r = client.post(
        "/documents/new/",
        {
            "collection": "emv",
            "document_type": option("deliverable_type", "Evaluation Plan").pk,
            "title": "Workplan",
            "study": study.study_id,
            "deliverable": "on",
            "stage": "draft_workplan",
            "main_file": upload().file,
            "comments_open": str(days(0)),
            "comments_close": str(days(14)),
        },
    )
    assert r.status_code == 302
    doc = study.documents.get()
    assert doc.deliverable and doc.versions.get().comment_period.closes == days(14)
    page = client.get(f"/documents/{doc.pk}/versions/new/").content.decode()
    assert "Draft workplan" in page and "Final study results" in page


def test_the_aging_review_shows_who_is_behind(people, pge, study, deliverable, client):
    from cedars.modules.documents import aging
    from cedars.modules.documents.models import Study

    ev, ed = people["evaluator"], people["ed"]
    v = deliverable.versions.get()
    comments.post(people["public"], None, v, note(kind="question", subject="Unanswered"))
    close(ev, v)
    service.add_version(ev, None, deliverable, [upload("final.pdf")], reason="Final", publish=True, stage=Stage.FINAL_RESULTS)
    service.add_version(ev, None, deliverable, [upload("draft_fix.pdf")], reason="Draft fix", stage=Stage.FINAL_RESULTS)  # left unpublished
    deliverables.request_responses(ed, deliverable, [pge], days(-5))
    Study.objects.filter(pk=study.pk).update(next_activity="Draft report", next_activity_due=days(-40))
    Study.objects.filter(pk=study.pk).update(updated_at=timezone.now() - timedelta(days=120))
    found = aging.items(TODAY)
    kinds = {(i.kind, i.owner.code) for i in found}
    assert kinds == {("draft", "DNV"), ("activity", "DNV"), ("question", "DNV"), ("stale", "DNV"), ("response", "PGE")}
    assert {i.kind: i.age(TODAY) for i in found}["activity"] == 40
    rows = {r["owner"].code: r for r in aging.by_owner(found, TODAY)}
    assert rows["DNV"]["total"] == 4 and rows["DNV"]["oldest"] == 120 and rows["PGE"]["total"] == 1
    assert sum(rows["DNV"]["buckets"]) == 4
    assert {i.kind for i in aging.items(TODAY, stale_days=200)} == {"draft", "activity", "question", "response"}
    # A published response clears PG&E's item; drafts are never purged, only aged.
    deliverables.file_response(people["pge_writer"], pge, deliverable, [upload("r.pdf")], publish=True)
    assert "response" not in {i.kind for i in aging.items(TODAY)}
    # CPUC staff only.
    sign_in(client, ev)
    assert client.get("/documents/studies/aging/").status_code == 404
    sign_in(client, ed)
    page = client.get("/documents/studies/aging/").content.decode()
    assert "Aging review" in page and "Draft report" in page
    rows = list(csv.reader(io.StringIO(client.get("/documents/studies/aging/", {"format": "csv", "kind": "activity"}).content.decode())))
    assert rows[1][:2] == ["DNV", "Next activity overdue"] and rows[1][5] == "40"
