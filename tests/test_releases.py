"""Release notes, limits and the FAQ (gap 9: BR-12.1, BR-12.3, BR-12.5): notes published before their changes take effect and sent to
subscribers, fixed once published; the Limits page read from the settings; upload limits enforced and shown; a searchable FAQ with
worked examples; each guide's release."""

import io
import zipfile
from datetime import timedelta
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, InternalError, ProgrammingError, transaction
from django.utils import timezone

from cedars.core import faq, revisions, uploads
from cedars.core.help import GUIDES
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications import service as notifications
from cedars.services.notifications.models import Notification
from cedars.services.releases import health, limits, service
from cedars.services.releases.models import Change, ReleaseNote
from cedars.services.releases.service import ReleaseError

from .conftest import page_only

pytestmark = pytest.mark.django_db


def later(days):
    return timezone.localdate() + timedelta(days=days)


def draft(user, days=30, version="", **change):
    note = service.create_draft(
        user, title="Larger claims uploads", summary="Claims files may be larger.", effective_on=later(days), version=version
    )
    service.add_change(
        user,
        note,
        **{"kind": Change.Kind.SYSTEM, "area": "claims", "text": "Claims uploads are compiled faster.", **change},
    )
    return note


# --- release notes


def test_a_note_is_published_before_it_takes_effect_with_the_usual_notice(cpuc_admin, settings):
    settings.CEDARS_RELEASE_NOTICE_DAYS = 14
    note = service.create_draft(cpuc_admin, title="T", summary="S", effective_on=later(30))
    with pytest.raises(ReleaseError, match="at least one change"):
        service.publish(cpuc_admin, note)
    service.add_change(cpuc_admin, note, kind="rule", area="budgets", text="Rule V123 becomes a reject.", action="Fix it before filing.")
    service.publish(cpuc_admin, note)
    note.refresh_from_db()
    assert note.published_at and service.status(note) == "coming" and note.short_notice_reason == ""
    assert AuditEvent.objects.filter(action="release.published", object_id=str(note.pk)).exists()

    soon = draft(cpuc_admin, days=5)
    with pytest.raises(ReleaseError, match="less than the usual 14 days"):
        service.publish(cpuc_admin, soon)
    service.publish(cpuc_admin, soon, short_notice_reason="A security fix.")
    soon.refresh_from_db()
    assert soon.short_notice_reason == "A security fix."

    past = draft(cpuc_admin, days=0)
    with pytest.raises(ReleaseError, match="before its changes take effect"):
        service.publish(cpuc_admin, past, short_notice_reason="late")


def test_only_system_administrators_write_notes(pge_admin, cpuc_admin, client, signed_in):
    with pytest.raises(AccessDenied):
        service.create_draft(pge_admin, title="T", summary="S", effective_on=later(30))
    signed_in(pge_admin)
    assert client.get("/releases/manage/").status_code == 403
    signed_in(cpuc_admin)
    assert client.get("/releases/manage/").status_code == 200


def test_a_published_note_never_changes_and_is_withdrawn_not_deleted(cpuc_admin):
    note = draft(cpuc_admin)
    service.publish(cpuc_admin, note)
    with pytest.raises(ReleaseError, match="does not change"):
        service.update_draft(cpuc_admin, note, title="Other")
    with pytest.raises(ReleaseError, match="does not change"):
        service.add_change(cpuc_admin, note, kind="system", area="claims", text="more")
    for attempt in (
        lambda: ReleaseNote.objects.filter(pk=note.pk).update(title="Edited"),
        lambda: ReleaseNote.objects.filter(pk=note.pk).delete(),
        lambda: Change.objects.filter(note=note).update(text="Edited"),
        lambda: Change.objects.create(note=note, kind="system", area="claims", text="Sneaked in"),
    ):
        with pytest.raises((InternalError, ProgrammingError, IntegrityError)), transaction.atomic():
            attempt()
    with pytest.raises(ReleaseError, match="Say why"):
        service.withdraw(cpuc_admin, note, "")
    service.withdraw(cpuc_admin, note, "Postponed to the next release.")
    note.refresh_from_db()
    assert service.status(note) == "withdrawn" and note.withdrawn_reason == "Postponed to the next release."
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        ReleaseNote.objects.filter(pk=note.pk).update(withdrawn_reason="Changed")
    # a withdrawn version may be announced again
    again = draft(cpuc_admin, version="")
    assert again.pk != note.pk


def test_one_live_note_per_release(cpuc_admin):
    first = draft(cpuc_admin, version="0.3.0")
    with pytest.raises(ReleaseError, match="already has a note"):
        draft(cpuc_admin, version="0.3.0")
    with pytest.raises(ReleaseError, match="not a release number"):
        draft(cpuc_admin, version="next")
    service.publish(cpuc_admin, first)
    service.withdraw(cpuc_admin, first, "Replanned")
    assert draft(cpuc_admin, version="0.3.0").version == "0.3.0"


def test_a_limit_change_names_its_limit_and_value(cpuc_admin):
    note = service.create_draft(cpuc_admin, title="T", summary="S", effective_on=later(30))
    with pytest.raises(ReleaseError, match="Choose the limit"):
        service.add_change(cpuc_admin, note, kind="limit", area="claims", text="Bigger files")
    with pytest.raises(ReleaseError, match="new value"):
        service.add_change(cpuc_admin, note, kind="limit", area="claims", text="Bigger files", limit="upload_mb")
    c = service.add_change(cpuc_admin, note, kind="limit", area="claims", text="Bigger files", limit="upload_mb", limit_value="200 MB")
    assert c.limit == "upload_mb"
    plain = service.add_change(cpuc_admin, note, kind="system", area="claims", text="x", limit="upload_mb", limit_value="1 MB")
    assert plain.limit == "" and plain.limit_value == ""


def test_subscribers_are_told_when_a_note_is_published_or_withdrawn(cpuc_admin, pge_admin, make_user, django_capture_on_commit_callbacks):
    quiet = make_user("quiet@example.org")
    notifications.subscribe(pge_admin, "release.published")
    notifications.subscribe(pge_admin, "release.withdrawn")
    notifications.set_follows(pge_admin, pa_scope="none")  # a release note concerns no PA, so following none still hears it
    note = draft(cpuc_admin, version="0.3.0", kind="limit", limit="upload_mb", limit_value="200 MB", text="Bigger files")
    with django_capture_on_commit_callbacks(execute=True):
        service.publish(cpuc_admin, note)
    n = Notification.objects.get(user=pge_admin)
    assert n.title.startswith("Coming on ") and "(CEDARS 0.3.0): Larger claims uploads" in n.title
    assert "New value: 200 MB." in n.body and n.link == f"/releases/{note.pk}/" and n.email
    assert not Notification.objects.filter(user=quiet).exists()
    with django_capture_on_commit_callbacks(execute=True):
        service.withdraw(cpuc_admin, note, "Not yet")
    w = Notification.objects.filter(user=pge_admin).order_by("-pk").first()
    assert w.title == "Withdrawn: Larger claims uploads" and "Reason: Not yet" in w.body


def test_the_pages(cpuc_admin, client, signed_in):
    note = draft(cpuc_admin, version="0.3.0")
    page = client.get("/releases/").content.decode()
    assert "Larger claims uploads" not in page_only(page)  # a draft is not public
    assert client.get(f"/releases/{note.pk}/").status_code == 404
    service.publish(cpuc_admin, note)
    page = page_only(client.get("/releases/").content.decode())
    assert "Coming changes" in page and f"/releases/{note.pk}/" in page and "This site runs CEDARS release 0.2.0" in page
    detail = page_only(client.get(f"/releases/{note.pk}/").content.decode())
    assert "Claims uploads are compiled faster." in detail and "Withdraw this note" not in detail
    feed = client.get("/releases/feed.xml")
    assert feed.status_code == 200 and b"Larger claims uploads" in feed.content
    signed_in(cpuc_admin)
    assert "Withdraw this note" in client.get(f"/releases/{note.pk}/").content.decode()


def test_every_page_announces_changes_within_the_notice_period(cpuc_admin, client, settings):
    settings.CEDARS_RELEASE_NOTICE_DAYS = 14
    soon, far = draft(cpuc_admin, days=10), service.create_draft(cpuc_admin, title="Far off", summary="S", effective_on=later(60))
    service.add_change(cpuc_admin, far, kind="system", area="everyone", text="x")
    service.publish(cpuc_admin, soon, short_notice_reason="Needed for the Q4 claims.")
    service.publish(cpuc_admin, far)
    page = client.get("/help/").content.decode()
    assert 'class="coming-changes"' in page and "Larger claims uploads" in page and "Far off" not in page_only(page)


def test_the_editing_pages(cpuc_admin, client, signed_in):
    signed_in(cpuc_admin)
    r = client.post("/releases/manage/", {"title": "New rules", "summary": "The 2028 rules.", "effective_on": later(20).isoformat()})
    note = ReleaseNote.objects.get(title="New rules")
    assert r.status_code == 302 and r["Location"] == f"/releases/manage/{note.pk}/"
    client.post(f"/releases/manage/{note.pk}/", {"action": "add", "kind": "rule", "area": "budgets", "text": "V1 rejects", "limit": ""})
    assert note.changes.count() == 1
    r = client.post(f"/releases/manage/{note.pk}/", {"action": "publish"})
    note.refresh_from_db()
    assert r["Location"] == f"/releases/{note.pk}/" and note.published_at


def test_the_running_release_and_the_guides(cpuc_admin, settings, client):
    assert revisions.stale() == [], "a user guide or the FAQ changed: run `manage.py help_revisions --update`"
    for g in GUIDES:
        assert revisions.version_key(g.revised) <= revisions.version_key(settings.VERSION), g.slug
    page = page_only(client.get("/help/budgets/").content.decode())
    assert f"Revised for CEDARS release {GUIDES[3].revised}" in page and f"this site runs\nrelease {settings.VERSION}" in page
    assert "Questions about this" in page and "/help/faq/#budget-made-of" in page

    assert health.check().status == "ok"  # quiet until notes are in use
    settings.VERSION = "0.3.0"
    old = draft(cpuc_admin, days=20, version="0.2.5", area="budgets")
    service.publish(cpuc_admin, old)
    with mock.patch.object(service, "today", return_value=later(25)):  # 0.2.5's changes are in effect
        result = health.check()
        behind = {g.slug: g.behind for g in service.guide_revisions()}
        page = page_only(client.get("/help/budgets/").content.decode())
    assert behind["budgets"] == ["0.2.5"] and behind["claims"] == []
    labels = [i.label for i in result.items]
    assert result.status == "attention" and any("Release 0.3.0 is running with no published release note" in x for x in labels)
    assert any("Budget filings and fund shifts guide was last revised for 0.2.0" in x for x in labels)
    assert "Release 0.2.5 changed this part of CEDARS" in page


def test_a_limit_change_in_effect_must_match_the_setting(cpuc_admin, settings):
    settings.CEDARS_UPLOAD_MAX_MB = 150
    note = draft(cpuc_admin, days=20, kind="limit", limit="upload_mb", limit_value="200 MB", text="Bigger files")
    service.publish(cpuc_admin, note)
    with mock.patch.object(service, "today", return_value=later(25)):
        assert [(c.limit, now) for _, c, now in service.limit_mismatches()] == [("upload_mb", "150 MB")]
        settings.CEDARS_UPLOAD_MAX_MB = 200
        assert service.limit_mismatches() == []


# --- limits


def test_the_limits_page_reads_the_settings_and_shows_announced_changes(cpuc_admin, client, settings):
    settings.CEDARS_UPLOAD_MAX_MB = 175
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {"user": "90/minute", "anon": "10/minute"}}
    page = page_only(client.get("/limits/").content.decode())
    assert "<strong>175 MB</strong>" in page and "90 calls a minute" in page and "10 calls a minute" in page
    assert limits.current("upload_files") == "20 files" and limits.current("run_records") == "2,000,000 measures"
    note = draft(cpuc_admin, kind="limit", limit="upload_mb", limit_value="250 MB", text="Bigger files")
    service.publish(cpuc_admin, note)
    page = page_only(client.get("/limits/").content.decode())
    assert f'<a href="/releases/{note.pk}/">250 MB from' in page
    data = client.get("/limits/?format=json").json()
    up = next(x for x in data["limits"] if x["key"] == "upload_mb")
    assert up["current"] == "175 MB" and up["coming"][0]["value"] == "250 MB" and data["release"] == "0.2.0"


def _zip(size):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("Measure.csv", "a" * size)
    return out.getvalue()


def test_upload_limits_are_enforced_and_shown(settings):
    settings.CEDARS_UPLOAD_MAX_MB = 1
    settings.CEDARS_UPLOAD_MAX_FILES = 2
    big = _zip(1024 * 1024 + 10)
    with pytest.raises(uploads.CombineError, match=r"big.zip is 1.0 MB; each file may be at most 1 MB"):
        uploads.combine([("big.zip", big)], lambda n: "Measure")
    with pytest.raises(uploads.CombineError, match="at most 2 files"):
        uploads.combine([("a.csv", b"x"), ("b.csv", b"x"), ("c.csv", b"x")], lambda n: None)
    field = uploads.MultipleFileField(help_text="A .zip.")
    assert str(field.help_text) == "A .zip. Up to 2 files of up to 1 MB each."
    settings.CEDARS_UPLOAD_MAX_FILES = 5
    assert str(field.help_text) == "A .zip. Up to 5 files of up to 1 MB each."  # read when shown
    from django import forms

    with pytest.raises(forms.ValidationError, match="at most 1 MB"):
        field.clean([SimpleUploadedFile("big.zip", big)])
    from cedars.modules.calculations.forms import NewRunForm

    job = NewRunForm.base_fields["job"]
    assert "Up to 1 MB." in str(job.help_text)
    with pytest.raises(forms.ValidationError, match="at most 1 MB"):
        job.clean(SimpleUploadedFile("job.zip", big))


def test_the_upload_pages_show_the_limits(pge_admin, client, signed_in, settings):
    from cedars.modules.budgets.forms import UploadForm

    settings.CEDARS_UPLOAD_MAX_MB = 99
    assert "of up to 99 MB each" in str(UploadForm().as_div())


# --- the FAQ


def test_the_faq_loads_and_fills_in_the_limits(settings):
    settings.CEDARS_UPLOAD_MAX_MB = 123
    settings.CEDARS_RELEASE_NOTICE_DAYS = 21
    found = {q.id: q for q in faq.questions()}
    assert len(found) >= 15 and sum(1 for q in found.values() if q.example) >= 5
    assert "at most 123 MB" in found["file-too-large"].a[0]
    assert "at least 21 days" in found["hear-about-changes"].a[0]
    for q in found.values():
        assert "{" not in " ".join(q.a), q.id  # every placeholder names a real limit
        for _, href in q.links:
            assert href.startswith("/"), q.id


def test_the_faq_search(client):
    ids = [q.id for q in faq.search("upload too large")]
    assert ids[0] == "file-too-large"
    assert [q.id for q in faq.search("TRC ratio")][0] == "portfolio-ratio"
    assert "quarter-compiled" in [q.id for q in faq.search("withdraw claims")]
    assert faq.search("zebra crossing") == []
    assert {q.topic for q in faq.search("", "claims")} == {"claims"}
    page = page_only(client.get("/help/faq/?q=uploads+too+large").content.decode())
    assert "answers with every word of “uploads too large”" in page or "answer with every word" in page
    assert 'id="file-too-large"' in page and "Worked example: Sending a large Measure table in parts" in page
    assert "No question matches" in page_only(client.get("/help/faq/?q=zebra").content.decode())
    whole = page_only(client.get("/help/faq/").content.decode())
    assert 'id="topic-claims"' in whole and "<caption>The TRC ratio" not in whole  # captions are the tables' own
    assert "<caption>Two programs&#x27; TRC benefits and costs, and the ratio of their sums</caption>" in whole
