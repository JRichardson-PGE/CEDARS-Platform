"""Testing on the site: the test scripts loaded as a version, a round of testing, scripts assigned to testers' own accounts
and shown in My queue, the run that stays open while the site is signed in as a test account or signed out, and a result saved
per test."""

import io
import json
import time
from datetime import date
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from cedars.core import queue as my_queue
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification
from cedars.services.testing import service
from cedars.services.testing.models import Assignment, Campaign, Draft, Result, ScriptVersion
from cedars.services.testing.models import TestCase as Case  # not a pytest class
from cedars.services.testing.templatetags.testing_tags import step_text
from tests.conftest import page_only, sign_in

pytestmark = pytest.mark.django_db


def kept_link(client) -> str:
    return client.cookies[service.COOKIE].value


SCRIPTS = {
    "site": "https://tests.example.org",
    "how": ["Record a result for every test.", "Say what happened when a test fails."],
    "scripts": [
        {
            "code": "PUB",
            "num": "01",
            "title": "Public visitor",
            "who": "Anyone, signed out.",
            "scope": "The public pages.",
            "accounts": [],
            "before": ["Use a private window."],
            "order": [],
            "sections": [
                {
                    "title": "Front page",
                    "intro": "",
                    "tests": [
                        {"id": "PUB-001", "title": "The front page opens", "pri": "P1", "acct": "", "pre": "Signed out.",
                         "steps": ["Open https://tests.example.org/."], "exp": "The front page shows.", "section": "Front page"},
                        {"id": "PUB-002", "title": "Help opens", "pri": "P2", "acct": "None", "pre": "",
                         "steps": ["Open https://tests.example.org/help/.", "Read the list of guides."], "exp": "Help lists the guides."},
                    ],
                }
            ],
        },
        {
            "code": "PAF",
            "num": "03",
            "title": "PA filer",
            "who": "PA staff who file.",
            "scope": "Filing.",
            "accounts": [["PG&E Filer", "The main account."]],
            "before": [],
            "order": [],
            "sections": [
                {"title": "Queue", "intro": "", "tests": [
                    {"id": "PAF-001", "title": "My queue", "pri": "P1", "acct": "PG&E Filer", "pre": "Signed in as PG&E Filer.",
                     "steps": ["Open https://tests.example.org/queue/."], "exp": "Filings due come first."}]},
            ],
        },
    ],
}  # fmt: skip


@pytest.fixture(autouse=True)
def from_this_site(client):
    """A browser says which site a form came from; a test's forms are taken from this site's pages only."""
    client.defaults["HTTP_ORIGIN"] = "http://testserver"


@pytest.fixture
def version():
    return service.load_scripts(None, SCRIPTS, "2026-11-12")


@pytest.fixture
def tester(make_user):
    return make_user("pat@example.org", "Pat Example")


@pytest.fixture
def campaign(cpuc_admin, version):
    return service.create_campaign(
        cpuc_admin, name="Release acceptance", version=version, starts_on=date(2026, 11, 16), ends_on=date(2026, 11, 30)
    )


@pytest.fixture
def assignment(cpuc_admin, campaign, tester, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return service.assign(cpuc_admin, campaign, campaign.version.scripts.get(code="PUB"), tester)


def test_the_scripts_load_as_a_version_that_is_never_replaced(version, tmp_path):
    assert version.site == "https://tests.example.org" and version.about["how"][0].startswith("Record")
    assert [(s.code, s.tests.count()) for s in version.scripts.all()] == [("PUB", 2), ("PAF", 1)]
    t = Case.objects.get(test_id="PUB-002")
    assert (t.section, t.priority, t.steps[1], t.position) == ("Front page", "P2", "Read the list of guides.", 1)
    assert AuditEvent.objects.get(action="testing.scripts_loaded").after["tests"] == 3
    with pytest.raises(service.TestingRefused, match="never replaced"):
        service.load_scripts(None, SCRIPTS, "2026-11-12")
    file = tmp_path / "uat.json"
    file.write_text(json.dumps(SCRIPTS))
    call_command("load_test_scripts", str(file), label="second")
    assert ScriptVersion.objects.count() == 2
    with pytest.raises(CommandError, match="never replaced"):
        call_command("load_test_scripts", str(file), label="second")


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d.update(scripts=[]), "scripts"),
        (lambda d: d["scripts"][0]["sections"][0]["tests"][0].update(steps=[]), "no steps"),
        (lambda d: d["scripts"][0]["sections"][0]["tests"][0].update(exp=""), "expected result is missing"),
        (lambda d: d["scripts"][1]["sections"][0]["tests"][0].update(id="PUB-001"), "twice"),
        (lambda d: d["scripts"][1].update(sections=[]), "no tests"),
    ],
)
def test_a_faulty_file_loads_nothing(change, message):
    data = json.loads(json.dumps(SCRIPTS))
    change(data)
    with pytest.raises(service.TestingRefused, match=message):
        service.load_scripts(None, data, "bad")
    assert not ScriptVersion.objects.exists() and not Case.objects.exists()


def test_only_a_system_administrator_sets_up_testing(client, signed_in, cpuc_admin, pge_admin, version, tester):
    with pytest.raises(AccessDenied):
        service.create_campaign(pge_admin, name="Mine", version=version, starts_on=date(2026, 11, 16))
    signed_in(pge_admin)
    assert client.get("/admin/testing/").status_code == 404
    signed_in(cpuc_admin)
    page = client.get("/admin/testing/").content.decode()
    assert "2026-11-12" in page and "No round of testing yet" in page
    r = client.post(
        "/admin/testing/", {"name": "Release acceptance", "version": version.pk, "starts_on": "2026-11-16", "ends_on": "2026-11-30"}
    )
    c = Campaign.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/admin/testing/campaigns/{c.pk}/"
    assert AuditEvent.objects.filter(action="testing.campaign_opened", actor=cpuc_admin).exists()
    r = client.post("/admin/testing/", {"name": "release acceptance", "version": version.pk, "starts_on": "2026-11-16"})
    assert "already a round of testing named" in page_only(r.content.decode())
    # assigning: the tester needs an account; a script goes to one tester once
    script = version.scripts.get(code="PAF")
    r = client.post(f"/admin/testing/campaigns/{c.pk}/assign", {"script": script.pk, "email": "nobody@example.org"})
    assert "No CEDARS account has that address" in page_only(r.content.decode())
    r = client.post(f"/admin/testing/campaigns/{c.pk}/assign", {"script": script.pk, "email": "PAT@example.org"})
    assert r.status_code == 302 and Assignment.objects.get().tester == tester
    r = client.post(f"/admin/testing/campaigns/{c.pk}/assign", {"script": script.pk, "email": "pat@example.org"})
    assert "already has that script" in page_only(r.content.decode())
    page = page_only(client.get(f"/admin/testing/campaigns/{c.pk}/").content.decode())
    assert "Pat Example" in page and "0 of 1" in page and "Not assigned" in page


def test_an_assignment_is_in_the_testers_queue_until_every_test_has_a_result(client, signed_in, assignment, tester):
    note = Notification.objects.get(user=tester)
    assert note.link == f"/testing/runs/{assignment.pk}/" and "2 tests" in note.body
    items = service.waiting_on(tester)
    assert [(i.what, i.url, i.due, i.note) for i in items] == [
        ("Release acceptance: script 01 Public visitor", f"/testing/runs/{assignment.pk}/", date(2026, 11, 30), "0 of 2 tests done.")
    ]
    assert any(s.kind.key == "testing" and s.items for s in my_queue.sections(tester))
    signed_in(tester)
    assert f"/testing/runs/{assignment.pk}/" in client.get("/queue/").content.decode()
    assert "Public visitor" in page_only(client.get("/testing/").content.decode())
    for t in assignment.script.tests.all():
        service.record(assignment, t, outcome="passed")
    assert service.waiting_on(tester) == []
    assert "All done" in page_only(client.get("/testing/").content.decode())


def test_the_run_stays_open_as_a_test_account_and_signed_out(client, assignment, tester, pge_admin):
    run = f"/testing/runs/{assignment.pk}/"
    # not the tester and no link: not found out, told how to open it
    r = client.get(run)
    assert r.status_code == 403 and "not open in this browser" in r.content.decode()
    sign_in(client, pge_admin)
    assert client.get(run).status_code == 403
    # the tester, as themselves: the page, the link, and the link left in the browser
    sign_in(client, tester)
    r = client.get(run)
    page = page_only(r.content.decode())
    assert r.status_code == 200 and "Your run link" in page and "Start with PUB-001" in page
    assert service.COOKIE in r.cookies and r.cookies[service.COOKIE]["httponly"] and r.cookies[service.COOKIE]["path"] == "/testing/"
    # signed in as someone else (a test account), the same browser still has the run, without the link shown
    sign_in(client, pge_admin)
    r = client.get(run)
    page = page_only(r.content.decode())
    assert r.status_code == 200 and "Your run link" not in page and "whichever account the site is signed in to" in page
    r = client.post(f"{run}PUB-001/", {"outcome": "passed", "comment": "", "next": "1"})
    assert r.status_code == 302 and r["Location"] == f"{run}PUB-002/"
    # the link is the same whenever it is shown (no time in it), so the browser still has the run on a later day
    with mock.patch("time.time", return_value=time.time() + 3 * 86400):
        assert service.run_token(assignment) in kept_link(client)
        assert service.holds_link(kept_link(client), assignment)
    # signed out too (signing out ends the session, not the run: the test client drops every cookie, a browser keeps this one)
    kept = client.cookies[service.COOKIE].value
    client.logout()
    client.cookies[service.COOKIE] = kept
    r = client.post(f"{run}PUB-002/", {"outcome": "failed", "comment": "Help is empty.", "note_2": "No guides listed", "stay": "1"})
    assert r.status_code == 302 and r["Location"] == f"{run}PUB-002/"
    results = {x.test.test_id: x for x in Result.objects.filter(assignment=assignment)}
    assert results["PUB-001"].outcome == "passed" and results["PUB-002"].step_notes == ["", "No guides listed"]
    # every result is the tester's, whoever the site was signed in as
    assert set(AuditEvent.objects.filter(action="testing.result_recorded").values_list("actor__email", flat=True)) == {"pat@example.org"}
    page = page_only(client.get(f"{run}PUB-002/").content.decode())
    assert "Help is empty." in page and "No guides listed" in page and "run this test signed out" in page


def test_a_run_link_opens_the_run_in_another_browser_and_can_be_replaced(client, assignment, tester):
    token = service.run_token(assignment)
    r = client.get(f"/testing/open/{token}/")
    assert r.status_code == 302 and r["Location"] == f"/testing/runs/{assignment.pk}/"
    assert client.get(f"/testing/runs/{assignment.pk}/").status_code == 200
    assert client.get("/testing/open/not-a-link/").status_code == 403
    sign_in(client, tester)
    r = client.post(f"/testing/runs/{assignment.pk}/new-link")
    assert r.status_code == 302
    assignment.refresh_from_db()
    assert assignment.link_number == 2 and service.assignment_of(token) is None
    assert service.assignment_of(service.run_token(assignment)) == assignment
    kept = client.cookies[service.COOKIE].value  # this browser was given the new link when it was made
    client.logout()
    client.cookies[service.COOKIE] = kept
    assert client.get(f"/testing/runs/{assignment.pk}/").status_code == 200
    client.cookies[service.COOKIE] = token  # a browser still holding the old one
    assert client.get(f"/testing/runs/{assignment.pk}/").status_code == 403


def test_a_result_needs_what_happened_when_it_failed_and_changes_are_audited(client, assignment, tester):
    sign_in(client, tester)
    url = f"/testing/runs/{assignment.pk}/PUB-001/"
    r = client.post(url, {"outcome": "failed", "comment": " ", "next": "1"})
    assert r.status_code == 200 and "Say what happened" in page_only(r.content.decode()) and not Result.objects.exists()
    r = client.post(url, {"comment": "x", "next": "1"})
    assert r.status_code == 200 and not Result.objects.exists()
    client.post(url, {"outcome": "blocked", "comment": "No test file.", "stay": "1"})
    client.post(url, {"outcome": "blocked", "comment": "No test file (c).", "stay": "1"})  # same outcome: no second audit event
    client.post(url, {"outcome": "passed", "comment": "", "stay": "1"})
    events = list(AuditEvent.objects.filter(action="testing.result_recorded").order_by("pk"))
    assert [(e.before, e.after["outcome"]) for e in events] == [(None, "blocked"), ({"outcome": "blocked"}, "passed")]
    assert Result.objects.get().outcome == "passed"
    assert service.progress(assignment) == {
        "total": 2,
        "done": 1,
        "left": 1,
        "percent": 50,
        "passed": 1,
        "failed": 0,
        "blocked": 0,
        "na": 0,
    }
    assert client.get(f"/testing/runs/{assignment.pk}/NOPE-1/").status_code == 404


def test_a_closed_round_and_a_withdrawn_assignment_take_no_results(client, signed_in, cpuc_admin, campaign, assignment, tester):
    t = assignment.script.tests.first()
    service.record(assignment, t, outcome="passed")
    signed_in(cpuc_admin)
    client.post(f"/admin/testing/campaigns/{campaign.pk}/close", {"reason": ""})
    campaign.refresh_from_db()
    assert not campaign.is_open and service.waiting_on(tester) == []
    with pytest.raises(service.TestingRefused, match="closed"):
        service.record(assignment, t, outcome="failed", comment="late")
    sign_in(client, tester)
    page = page_only(client.get(f"/testing/runs/{assignment.pk}/PUB-001/").content.decode())
    assert "can no longer be changed" in page and "Save and go to the next test" not in page
    signed_in(cpuc_admin)
    r = client.post(f"/admin/testing/campaigns/{campaign.pk}/reopen", {"reason": ""})
    campaign.refresh_from_db()
    assert not campaign.is_open  # a reason is needed
    client.post(f"/admin/testing/campaigns/{campaign.pk}/reopen", {"reason": "Two testers were away."})
    campaign.refresh_from_db()
    assert campaign.is_open
    r = client.post(f"/admin/testing/assignments/{assignment.pk}/withdraw", {"reason": "Left the team."})
    assert r.status_code == 302
    with pytest.raises(service.TestingRefused, match="no longer assigned"):
        service.record(assignment, t, outcome="failed", comment="late")
    sign_in(client, tester)
    assert client.get(f"/testing/runs/{assignment.pk}/").status_code == 403
    assert Result.objects.count() == 1  # what was recorded is kept
    assert {"testing.campaign_closed", "testing.campaign_reopened", "testing.assignment_withdrawn"} <= set(
        AuditEvent.objects.values_list("action", flat=True)
    )


def test_where_testing_is_off_its_pages_are_not_found(client, signed_in, cpuc_admin, assignment, tester, settings):
    settings.CEDARS_TESTING = False
    assert service.waiting_on(tester) == []
    sign_in(client, tester)
    assert client.get("/testing/").status_code == 404
    assert client.get(f"/testing/runs/{assignment.pk}/").status_code == 404
    assert client.get(f"/testing/open/{service.run_token(assignment)}/").status_code == 404
    with pytest.raises(service.TestingRefused):
        service.record(assignment, assignment.script.tests.first(), outcome="passed")
    signed_in(cpuc_admin)
    page = page_only(client.get("/admin/testing/").content.decode())
    assert "not switched on for this site" in page and "Open a round of testing" not in page
    assert client.get(f"/admin/testing/campaigns/{assignment.campaign_id}/").status_code == 404


def test_step_links_open_this_site_in_a_new_tab():
    html = step_text(
        "Open https://tests.example.org/queue/. Then <b> see https://other.example.org/x?y=1, and stop.", "https://tests.example.org"
    )
    assert '<a href="/queue/" target="_blank" rel="noopener">/queue/<span class="sr-only"> (opens in a new tab)</span></a>.' in html
    assert 'href="https://other.example.org/x?y=1"' in html and "&lt;b&gt;" in html
    assert step_text("Open https://tests.example.org.", "https://tests.example.org").startswith('Open <a href="/"')


def test_a_result_saves_after_signing_in_and_out_elsewhere_but_never_from_another_site(assignment, tester, pge_admin):
    """The test page stays open while the tester signs in to the site as a test account in another tab, which changes the usual
    form token: the test's forms do not use it, and take a form from this site's own pages only."""
    browser = Client(enforce_csrf_checks=True, HTTP_ORIGIN="http://testserver")
    sign_in(browser, tester)
    url = f"/testing/runs/{assignment.pk}/PUB-001/"
    assert browser.get(url).status_code == 200
    sign_in(browser, pge_admin)  # in another tab, as a test account
    r = browser.post(url, {"outcome": "passed", "comment": "", "stay": "1"})
    assert r.status_code == 302 and Result.objects.get().outcome == "passed"
    assert browser.post(f"{url}draft", {"outcome": "failed", "comment": "Typed"}).json()["draft"] is True
    # from another site, or from nowhere a browser names: refused, nothing changed
    for headers in (
        {"HTTP_ORIGIN": "https://elsewhere.example.org"},
        {"HTTP_ORIGIN": "", "HTTP_REFERER": "https://elsewhere.example.org/x"},
        {"HTTP_ORIGIN": ""},
    ):
        r = browser.post(url, {"outcome": "failed", "comment": "Not mine", "stay": "1"}, **headers)
        assert r.status_code == 403 and "Form not sent" in r.content.decode()
        assert browser.post(f"{url}draft", {"comment": "Not mine"}, **headers).status_code == 403
    assert Result.objects.get().outcome == "passed" and Draft.objects.get().comment == "Typed"
    # a browser that sends no Origin sends the page's address
    r = browser.post(url, {"outcome": "na", "comment": "", "stay": "1"}, HTTP_ORIGIN="", HTTP_REFERER=f"http://testserver{url}")
    assert r.status_code == 302 and Result.objects.get().outcome == "na"
    # the other forms of the site keep their token
    assert browser.post(f"/testing/runs/{assignment.pk}/new-link").status_code == 403


def test_what_a_tester_types_is_kept_until_they_save_or_discard_it(client, assignment, tester):
    sign_in(client, tester)
    url = f"/testing/runs/{assignment.pk}/PUB-002/"
    whole = client.get(url).content.decode()
    assert f'data-draft-url="{url}draft"' in whole and "testing/test.js" in whole and "have not saved it" not in page_only(whole)
    r = client.post(f"{url}draft", {"outcome": "failed", "comment": "Help is em", "note_2": "No guides"})
    assert r.json()["saved"] is True and r.json()["draft"] is True
    client.post(f"{url}draft", {"outcome": "failed", "comment": "Help is empty.", "note_2": "No guides listed"})
    d = Draft.objects.get()
    assert (d.outcome, d.comment, d.step_notes) == ("failed", "Help is empty.", ["", "No guides listed"])
    # not a result: nothing counted, nothing audited, and the run page says so
    assert not Result.objects.exists() and service.progress(assignment)["done"] == 0
    assert not AuditEvent.objects.filter(action="testing.result_recorded").exists()
    assert "Typed, not saved" in page_only(client.get(f"/testing/runs/{assignment.pk}/").content.decode())
    # coming back, the form holds what was typed
    page = page_only(client.get(url).content.decode())
    assert "have not saved it" in page and "Help is empty." in page and 'value="No guides listed"' in page
    assert "Discard what I typed" in page
    # saving makes it the result and drops the draft
    client.post(url, {"outcome": "failed", "comment": "Help is empty.", "note_2": "No guides listed", "stay": "1"})
    assert Result.objects.get().comment == "Help is empty." and not Draft.objects.exists()
    # typing over a saved result leaves the result as it is until saved; typing it back drops the draft
    client.post(f"{url}draft", {"outcome": "passed", "comment": ""})
    page = page_only(client.get(url).content.decode())
    assert "your saved result is still <strong>Failed</strong>" in page and Result.objects.get().outcome == "failed"
    r = client.post(f"{url}draft", {"outcome": "failed", "comment": "Help is empty.", "note_2": "No guides listed"})
    assert r.json()["draft"] is False and not Draft.objects.exists()
    # discarding goes back to the saved result
    client.post(f"{url}draft", {"outcome": "passed", "comment": "Changed my mind"})
    r = client.post(url, {"discard": "1"})
    assert r.status_code == 302 and not Draft.objects.exists() and Result.objects.get().outcome == "failed"
    assert "Changed my mind" not in page_only(client.get(url).content.decode())


def test_a_draft_is_kept_only_for_an_open_run(client, signed_in, cpuc_admin, campaign, assignment, tester, pge_admin):
    url = f"/testing/runs/{assignment.pk}/PUB-001/draft"
    assert client.post(url, {"comment": "x"}).status_code == 403  # no run open in this browser
    sign_in(client, pge_admin)
    assert client.post(url, {"comment": "x"}).status_code == 403
    sign_in(client, tester)
    assert client.get(url).status_code == 405
    assert client.post(f"/testing/runs/{assignment.pk}/NOPE-1/draft", {"comment": "x"}).status_code == 404
    assert client.post(url, {"comment": "x"}).json()["draft"] is True
    service.close_campaign(cpuc_admin, campaign)
    r = client.post(url, {"comment": "y"})
    assert r.status_code == 409 and "closed" in r.json()["why"] and Draft.objects.get().comment == "x"
    # a closed round shows the saved result, not what was typed
    assert "have not saved it" not in page_only(client.get(f"/testing/runs/{assignment.pk}/PUB-001/").content.decode())


def test_the_test_lead_reads_a_rounds_results_and_downloads_them(
    client, signed_in, cpuc_admin, pge_admin, campaign, assignment, tester, make_user
):
    from openpyxl import load_workbook

    other = make_user("sam@example.org", "Sam Example")
    second = service.assign(cpuc_admin, campaign, assignment.script, other)
    gone = service.assign(cpuc_admin, campaign, campaign.version.scripts.get(code="PAF"), other)
    tests = {t.test_id: t for t in assignment.script.tests.all()}
    service.record(assignment, tests["PUB-001"], outcome="passed")
    service.record(assignment, tests["PUB-002"], outcome="failed", comment="Help is empty.", step_notes=["", "No guides listed"])
    service.record(second, tests["PUB-001"], outcome="blocked", comment="The site was down.")
    service.record(gone, gone.script.tests.first(), outcome="failed", comment="Withdrawn later.")
    service.withdraw(cpuc_admin, gone, "Left the team")
    service.save_draft(second, tests["PUB-002"], outcome="failed", comment="Only typed")

    s = service.summary(campaign)
    pub = next(r for r in s["rows"] if r["script"].code == "PUB")
    assert (pub["testers"], pub["total"], pub["done"], pub["left"], pub["passed"], pub["failed"], pub["blocked"]) == (2, 4, 3, 1, 1, 1, 1)
    assert s["whole"]["testers"] == 2 and s["whole"]["total"] == 4 and s["whole"]["percent"] == 75

    url = f"/admin/testing/campaigns/{campaign.pk}/results/"
    signed_in(pge_admin)
    assert client.get(url).status_code == 404 and client.get(f"{url[:-1]}.xlsx").status_code == 404
    signed_in(cpuc_admin)
    assert "Results of this round" in page_only(client.get(f"/admin/testing/campaigns/{campaign.pk}/").content.decode())
    page = page_only(client.get(url).content.decode())
    assert "3 of 4 tests have a result (75%)" in page and "2 results: failed and blocked." in page
    assert "Help is empty." in page and "Step 2: No guides listed" in page and "The site was down." in page
    assert "Withdrawn later." not in page and "Only typed" not in page
    page = page_only(client.get(url, {"show": "passed", "script": assignment.script.pk}).content.decode())
    assert "1 result: passed, in script" in page and "Help is empty." not in page
    assert "0 results" in page_only(client.get(url, {"show": "na", "script": "x"}).content.decode())

    r = client.get(f"{url[:-1]}.xlsx")
    assert r.status_code == 200 and "testing-results-release-acceptance.xlsx" in r["Content-Disposition"]
    book = load_workbook(io.BytesIO(r.content))
    assert book.sheetnames == ["Round", "By script", "Results"]
    rows = [
        dict(zip([c.value for c in book["Results"][1]], [c.value for c in row], strict=True))
        for row in book["Results"].iter_rows(min_row=2)
    ]
    assert len(rows) == 2 * 2 + gone.script.tests.count()  # every test of every tester, the withdrawn assignment's too
    mine = {r["Test"]: r for r in rows if r["Tester's email"] == "pat@example.org"}
    assert mine["PUB-002"]["Result"] == "Failed" and mine["PUB-002"]["Notes on steps"] == "Step 2: No guides listed"
    theirs = {r["Test"]: r for r in rows if r["Tester's email"] == "sam@example.org" and r["Assignment"] == "Assigned"}
    assert (
        theirs["PUB-001"]["Result"] == "Blocked"
        and theirs["PUB-002"]["Result"] == "No result yet"
        and not theirs["PUB-002"]["What happened"]
    )
    assert [r["What happened"] for r in rows if r["Assignment"] == "Withdrawn" and r["Result"] == "Failed"] == ["Withdrawn later."]
    event = AuditEvent.objects.get(action="testing.results_downloaded")
    assert event.actor == cpuc_admin and event.after["rows"] == len(rows)


def test_a_failed_or_blocked_result_is_a_finding_in_the_requests_inbox(
    client, signed_in, cpuc_admin, assignment, tester, settings, django_capture_on_commit_callbacks
):
    from cedars.services.feedback import service as requests
    from cedars.services.feedback.models import Feedback
    from cedars.services.testing.models import Finding
    from cedars.services.work import service as work

    settings.CEDARS_FEEDBACK_PER_HOUR = 1  # the form's hourly limit neither counts findings nor stops them
    case, other = Case.objects.get(test_id="PUB-001"), Case.objects.get(test_id="PUB-002")
    service.record(assignment, other, outcome="passed")
    assert not Feedback.objects.exists()
    with django_capture_on_commit_callbacks(execute=True):
        result = service.record(assignment, case, outcome="failed", comment="The page shows 2027.", step_notes=["Here"])
    fb = service.finding_of(result)
    assert fb.kind == "finding" and fb.user == tester and fb.status == "new" and fb.organisation is None
    assert fb.page == f"/testing/runs/{assignment.pk}/PUB-001/"
    for words in ("Test PUB-001: ", "Round of testing: ", "Result: Failed", "The page shows 2027.", "Step 1: Here", "Expected:"):
        assert words in fb.message
    assert AuditEvent.objects.filter(action="testing.finding_sent").latest("pk").after == {"test": "PUB-001", "request": fb.pk}
    assert Notification.objects.filter(user=cpuc_admin, title__startswith="A test finding from").count() == 1
    service.record(assignment, other, outcome="blocked", comment="No file.")  # a second within the hour
    assert Feedback.objects.filter(kind="finding").count() == 2
    requests.submit(page="/budgets/", message="One from the form", user=tester, kind="help")  # still takes the person's own
    # it cannot be sent from the form
    with pytest.raises(requests.FeedbackRefused):
        requests.submit(page="/", message="x", user=tester, kind="nonsense")
    sign_in(client, tester)
    r = client.post("/feedback/", {"page": "/", "kind": "finding", "message": "Not a finding"})
    assert r.status_code == 200 and not Feedback.objects.filter(message="Not a finding").exists()

    # saved again unchanged: nothing more; with other words: added as the tester's reply
    service.record(assignment, case, outcome="failed", comment="The page shows 2027.", step_notes=["Here"])
    assert fb.replies.count() == 0
    service.record(assignment, case, outcome="blocked", comment="Now it does not open.")
    fb.refresh_from_db()
    assert fb.replies.count() == 1 and "Result: Blocked" in fb.replies.get().body and Finding.objects.count() == 2
    # the tester follows it; the lead sees it on the results page; it is joined to a work item like any request
    page = page_only(client.get(f"/testing/runs/{assignment.pk}/PUB-001/").content.decode())
    assert "as a finding" in page and f'href="/feedback/{fb.pk}/"' in page
    assert "A test finding" in page_only(client.get("/feedback/mine/").content.decode())
    signed_in(cpuc_admin)
    page = page_only(client.get(f"/admin/testing/campaigns/{assignment.campaign_id}/results/").content.decode())
    assert f'href="/admin/feedback/{fb.pk}/"' in page and "open the finding from PUB-001" in page
    assert "A test finding" in page_only(client.get("/admin/feedback/?kind=finding").content.decode())
    item = work.propose(cpuc_admin, request=fb, title="The year in the heading is wrong", kind="problem")
    assert work.item_of(fb) == item and work.evidence(item)["requests"] == 1

    # passing closes it; failing again afterwards is a new finding
    service.record(assignment, case, outcome="passed")
    fb.refresh_from_db()
    assert fb.status == "closed" and "the result is now: Passed" in fb.replies.order_by("pk").last().body
    again = service.record(assignment, case, outcome="failed", comment="Back to 2027.")
    new = service.finding_of(again)
    assert new.pk != fb.pk and new.is_open and Finding.objects.count() == 2
